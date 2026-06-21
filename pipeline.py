"""
台股高股息個股期貨套利掃描 ─ 資料抓取管線
================================================
依序串接：
  1. TWSE / 公開資訊觀測站  → 未來 30 日除息表
  2. TAIFEX               → 個股期貨清單與收盤價
  3. SITCA / 投信官網      → 高股息 ETF 持股權重
  4. 任一即時報價 API      → 現股報價（Shioaji / Fugle）

執行：
    pip install requests pandas beautifulsoup4 lxml openpyxl
    python pipeline.py --days 30 --min-etf 2

注意：
  - TWSE / TAIFEX 對頻繁請求會 429，請加 sleep 或 cache
  - ETF 持股資料每月 5 日更新，每日 PCF 可由各投信網站抓
  - 即時報價需自行申請券商 API token
"""

import argparse
import csv
import io
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ArbScanner/1.0)"}


# ─────────────────────────────────────────────────────────────
# 1. 除息資料
# ─────────────────────────────────────────────────────────────
def fetch_ex_dividend_calendar(start: date, end: date) -> pd.DataFrame:
    """
    從 TWSE 抓除息日曆。
    端點：https://www.twse.com.tw/exchangeReport/TWT49U
    參數：response=csv, strDate=YYYYMMDD, endDate=YYYYMMDD
    回傳欄位：股票代號, 名稱, 除息日, 現金股利, 股票股利
    """
    url = "https://www.twse.com.tw/exchangeReport/TWT49U"
    params = {
        "response": "csv",
        "strDate": start.strftime("%Y%m%d"),
        "endDate": end.strftime("%Y%m%d"),
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.encoding = "big5"

    # TWSE CSV 前後有雜訊，要找到表頭那行
    lines = [ln for ln in r.text.splitlines() if ln.count(",") >= 5]
    if not lines:
        return pd.DataFrame()

    df = pd.read_csv(io.StringIO("\n".join(lines)))
    df.columns = [c.strip().replace('"', "") for c in df.columns]

    df = df.rename(columns={
        "股票代號": "ticker",
        "名稱": "name",
        "除息交易日": "ex_date",
        "除權交易日": "ex_right_date",
        "現金股利": "cash_div",
    })

    # 民國轉西元
    def roc_to_ad(s):
        try:
            y, m, d = str(s).split("/")
            return date(int(y) + 1911, int(m), int(d))
        except Exception:
            return None

    df["ex_date"] = df["ex_date"].apply(roc_to_ad)
    df = df.dropna(subset=["ex_date"])
    df["cash_div"] = pd.to_numeric(df["cash_div"], errors="coerce").fillna(0)
    df = df[df["cash_div"] > 0]
    return df[["ticker", "name", "ex_date", "cash_div"]]


# ─────────────────────────────────────────────────────────────
# 2. 個股期貨清單與報價
# ─────────────────────────────────────────────────────────────
def fetch_stock_futures_list() -> pd.DataFrame:
    """
    從 TAIFEX 抓所有具個股期貨的標的。
    端點：https://www.taifex.com.tw/cht/2/stockLists
    """
    url = "https://www.taifex.com.tw/cht/2/stockLists"
    r = requests.get(url, headers=HEADERS, timeout=15)
    tables = pd.read_html(r.text)
    # 通常第一張就是清單
    df = tables[0]
    df.columns = [c.strip() for c in df.columns]

    # 欄位常見：商品代碼, 標的證券, 標的證券代號
    df = df.rename(columns={
        "標的證券代號": "ticker",
        "標的證券": "name",
        "商品代碼": "futures_code",
    })
    return df[["ticker", "name", "futures_code"]]


def fetch_futures_close(date_: date) -> pd.DataFrame:
    """
    抓某日個股期貨收盤價。
    端點：https://www.taifex.com.tw/cht/3/stkDataDown
    回傳欄位：futures_code, close, volume
    """
    url = "https://www.taifex.com.tw/cht/3/stkDataDown"
    params = {
        "down_type": 1,
        "queryStartDate": date_.strftime("%Y/%m/%d"),
        "queryEndDate": date_.strftime("%Y/%m/%d"),
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.encoding = "big5"

    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]

    df = df.rename(columns={
        "契約": "futures_code",
        "收盤價": "close",
        "成交量": "volume",
        "到期月份(週別)": "expiry",
    })

    # 只取最近月合約
    df = df.sort_values(["futures_code", "expiry"]).groupby("futures_code").first().reset_index()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df[["futures_code", "close", "volume"]]


# ─────────────────────────────────────────────────────────────
# 3. 高股息 ETF 持股
# ─────────────────────────────────────────────────────────────
HIGH_DIV_ETFS = {
    "0056":  {"name": "元大高股息",         "url": "https://www.yuantaetfs.com/RWD/ProductsInfo/Documents/0056"},
    "00878": {"name": "國泰永續高股息",     "url": "https://www.cathaysite.com.tw/ETF/00878"},
    "00919": {"name": "群益台灣精選高息",   "url": "https://www.capitalfund.com.tw/CFAWeb/ETF/00919"},
    "00929": {"name": "復華台灣科技優息",   "url": "https://www.fhtrust.com.tw/ETF/00929"},
    "00713": {"name": "元大台灣高息低波",   "url": "https://www.yuantaetfs.com/RWD/ProductsInfo/Documents/00713"},
    "00900": {"name": "富邦特選高股息30",   "url": "https://websys.fsit.com.tw/FubonETF/Trade/ETFInfo.aspx?Code=00900"},
    "00915": {"name": "凱基優選高股息30",   "url": "https://kgifund.com.tw/etf/00915"},
    "00936": {"name": "台新永續高息中小",   "url": "https://www.tsit.com.tw/ETF/00936"},
    "00940": {"name": "元大臺灣價值高息",   "url": "https://www.yuantaetfs.com/RWD/ProductsInfo/Documents/00940"},
    "00939": {"name": "統一台灣高息動能",   "url": "https://www.invest.uni-president.com.tw/etf/00939"},
}


def fetch_etf_holdings(etf_id: str) -> pd.DataFrame:
    """
    抓單一 ETF 成分股權重。
    實務上每家投信網頁結構不同，建議：
      (a) 用 SITCA 月報 CSV: https://www.sitca.org.tw/  (月初公告)
      (b) 用各投信「每日 PCF」(申購買回清單)，是最即時的
      (c) 用付費資料源 (TEJ, MoneyDJ, CMoney)

    這裡示範用 SITCA 月報 CSV 結構。
    """
    # 範例：SITCA ETF 持股月報 (假設端點)
    url = f"https://www.sitca.org.tw/etf/holdings.aspx?fund_id={etf_id}&format=csv"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = "utf-8"
        df = pd.read_csv(io.StringIO(r.text))
        df = df.rename(columns={"證券代號": "ticker", "證券名稱": "name", "持股比例": "weight"})
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
        df["etf_id"] = etf_id
        df["etf_name"] = HIGH_DIV_ETFS[etf_id]["name"]
        return df[["etf_id", "etf_name", "ticker", "weight"]]
    except Exception as e:
        print(f"[warn] {etf_id} 抓取失敗: {e}")
        return pd.DataFrame(columns=["etf_id", "etf_name", "ticker", "weight"])


def fetch_all_etf_holdings() -> pd.DataFrame:
    dfs = []
    for etf_id in HIGH_DIV_ETFS:
        df = fetch_etf_holdings(etf_id)
        dfs.append(df)
        time.sleep(0.5)  # 禮貌延遲
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


# ─────────────────────────────────────────────────────────────
# 4. 現股報價 (示範 Fugle Marketdata REST)
# ─────────────────────────────────────────────────────────────
def fetch_spot_price_fugle(ticker: str, api_key: str) -> Optional[float]:
    """
    Fugle Marketdata REST 即時報價。
    需到 https://developer.fugle.tw 申請 API key
    """
    url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{ticker}"
    r = requests.get(url, headers={"X-API-KEY": api_key}, timeout=10)
    if r.status_code != 200:
        return None
    data = r.json()
    return data.get("data", {}).get("quote", {}).get("price")


# ─────────────────────────────────────────────────────────────
# 5. 組合 ─ 主管線
# ─────────────────────────────────────────────────────────────
@dataclass
class ArbCandidate:
    ticker: str
    name: str
    ex_date: date
    cash_div: float
    spot: float
    futures: float
    futures_volume: int
    etfs: list = field(default_factory=list)

    @property
    def yield_pct(self) -> float:
        return self.cash_div / self.spot * 100 if self.spot else 0

    @property
    def basis(self) -> float:
        return self.futures - self.spot

    @property
    def naive_short_net(self) -> int:
        gross = self.cash_div * 2000
        cost = self.futures * 2000 * 0.00002 * 2 + 100
        return int(gross - cost)


def build_scan(days: int = 30, min_etf: int = 2, spot_api_key: str = "") -> pd.DataFrame:
    print("→ 抓除息日曆…")
    div = fetch_ex_dividend_calendar(date.today(), date.today() + timedelta(days=days))
    print(f"  {len(div)} 檔股票")

    print("→ 抓個股期貨清單…")
    fut_list = fetch_stock_futures_list()
    print(f"  {len(fut_list)} 檔有個股期")

    print("→ 抓昨日個股期收盤…")
    fut_close = fetch_futures_close(date.today() - timedelta(days=1))

    print("→ 抓高股息 ETF 持股…")
    etfs = fetch_all_etf_holdings()
    print(f"  {etfs['etf_id'].nunique() if len(etfs) else 0} 檔 ETF")

    # 合併：除息 ∩ 有期貨
    merged = div.merge(fut_list, on="ticker", how="inner", suffixes=("", "_f"))
    merged = merged.merge(fut_close, on="futures_code", how="left")

    # 統計每檔被多少 ETF 納入
    if len(etfs):
        etf_count = etfs.groupby("ticker")["etf_id"].count().rename("etf_count")
        merged = merged.merge(etf_count, on="ticker", how="left")
        merged["etf_count"] = merged["etf_count"].fillna(0).astype(int)
    else:
        merged["etf_count"] = 0

    merged = merged[merged["etf_count"] >= min_etf]

    # 加現股報價
    if spot_api_key:
        print("→ 抓現股報價…")
        merged["spot"] = merged["ticker"].apply(lambda t: fetch_spot_price_fugle(t, spot_api_key))
    else:
        merged["spot"] = merged["close"]  # 沒 key 就用期貨價當參考

    # 套利試算
    merged["yield_pct"] = merged["cash_div"] / merged["spot"] * 100
    merged["basis"] = merged["close"] - merged["spot"]
    merged["naive_net_per_contract"] = (
        merged["cash_div"] * 2000 - merged["close"] * 2000 * 0.00002 * 2 - 100
    ).round(0)

    return merged.sort_values("naive_net_per_contract", ascending=False)


# ─────────────────────────────────────────────────────────────
# 6. 歷史回測
# ─────────────────────────────────────────────────────────────
def backtest_one(ticker: str, years: list[int]) -> pd.DataFrame:
    """
    對單一標的，回測過去 N 年除息日的中性套利績效。
    需要：
      - 各年除息日當日的現股 / 期貨收盤
      - 除息前一日的現股 / 期貨收盤
    這裡寫骨架，實際請接 TWSE 歷史報價 API。
    """
    results = []
    for y in years:
        # TODO: 從 TWSE 抓該年該股票的除息日
        # TODO: 抓除息前 D-1 收盤 (現股、期貨) 與除息日 D 收盤
        # 試算：
        #   basis_open = F(D-1) - S(D-1)
        #   basis_close = F(D) - S(D)
        #   pnl = -basis_open * 2000 + basis_close * 2000  + div_net - costs
        results.append({
            "year": y,
            "basis_open": None,
            "basis_close": None,
            "pnl": None,
            "win": None,
        })
    return pd.DataFrame(results)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="未來幾日內除息")
    parser.add_argument("--min-etf", type=int, default=2, help="至少被幾檔 ETF 納入")
    parser.add_argument("--spot-key", default="", help="Fugle API key (選填)")
    parser.add_argument("--out", default="candidates.csv")
    args = parser.parse_args()

    df = build_scan(args.days, args.min_etf, args.spot_key)
    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n✓ 共 {len(df)} 檔候選，已輸出 {args.out}")
    print(df[["ticker", "name", "ex_date", "cash_div", "etf_count", "naive_net_per_contract"]].head(20).to_string(index=False))
