"""
每日資料庫更新
================
功能：
  1. 抓未來 30 日除息表
  2. 比對個股期貨清單，取有期貨的標的
  3. 為每檔抓：現股報價、期貨報價、保證金級距、ETF 持股、近 3 年填息表現
  4. 算出：預期跌幅、建議持有日、勝率、預期淨利
  5. 輸出 data/stocks.json 供前端讀

排程：每天 09:30 (Asia/Taipei) 跑一次 (盤中 15 分鐘後資料才齊)

執行：
    pip install requests pandas beautifulsoup4 lxml openpyxl yfinance
    python update_db.py --out data/stocks.json

前端讀取：
    fetch('./data/stocks.json').then(r => r.json()).then(data => {
      DATA = [...DATA, ...data.stocks];  // 合併內建與遠端
    });
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    from pipeline import (
        fetch_ex_dividend_calendar,
        fetch_stock_futures_list,
        fetch_futures_close,
        fetch_all_etf_holdings,
        HIGH_DIV_ETFS,
    )
except ImportError:
    print("[error] 找不到 pipeline.py")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
# 保證金級距 (從 TAIFEX 抓 → cache)
# ─────────────────────────────────────────────────────────────
MARGIN_TIER_CACHE_FILE = "data/margin_tiers.json"
MARGIN_RATES = {"A": 0.135, "B": 0.162, "C": 0.2025}


def fetch_margin_tiers() -> dict:
    """
    從 TAIFEX 抓「股票期貨保證金級距表」
    端點: https://www.taifex.com.tw/cht/5/stockMargining
    回傳: {ticker: "A" | "B" | "C"}
    """
    url = "https://www.taifex.com.tw/cht/5/stockMargining"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        tables = pd.read_html(r.text)
        # 通常是第一張表，欄位: 商品代碼, 標的證券, 級距, 風險價格係數
        df = tables[0]
        df.columns = [c.strip() for c in df.columns]

        result = {}
        for _, row in df.iterrows():
            ticker = str(row.get("標的證券代號", row.get("標的證券", ""))).strip()
            tier_raw = str(row.get("級距", row.get("風險價格係數", ""))).strip()
            if "20.25" in tier_raw or "20" in tier_raw:
                tier = "C"
            elif "16.2" in tier_raw or "16" in tier_raw:
                tier = "B"
            else:
                tier = "A"
            if ticker.isdigit():
                result[ticker] = tier
        return result
    except Exception as e:
        print(f"[warn] 保證金級距抓取失敗: {e}，使用 cache")
        # Fallback to cached version
        if Path(MARGIN_TIER_CACHE_FILE).exists():
            return json.loads(Path(MARGIN_TIER_CACHE_FILE).read_text())
        return {}


# ─────────────────────────────────────────────────────────────
# 填息歷史 (近 3 年)
# ─────────────────────────────────────────────────────────────
def fetch_fill_days_history(ticker: str, years: list[int]) -> list[Optional[int]]:
    """
    抓某檔股票最近 N 年的填息天數
    來源: 公開資訊觀測站 t05st09_ 或財報狗、Goodinfo

    示範用 Goodinfo CSV (需爬蟲)，實務建議：
    (a) 用財報狗 statementdog API (付費)
    (b) 自行從 TWSE 日 K 算 - 抓除息日當日 + 後 252 日，找第一日收盤 >= 除息前收盤
    """
    fill_days = []
    for y in years:
        try:
            # 簡化版：用 yfinance 抓歷史價，自己算填息日數
            import yfinance as yf
            tk = yf.Ticker(f"{ticker}.TW")

            # 1. 抓該年除息日 (從 dividends 屬性)
            divs = tk.dividends
            if divs.empty:
                fill_days.append(None)
                continue

            year_divs = divs[divs.index.year == y]
            if year_divs.empty:
                fill_days.append(None)
                continue

            ex_date = year_divs.index[0].date()
            div_amount = float(year_divs.iloc[0])

            # 2. 抓除息前收盤、後 252 日收盤
            hist = tk.history(start=ex_date - timedelta(days=5), end=ex_date + timedelta(days=300))
            if hist.empty:
                fill_days.append(None)
                continue

            # 除息前一日收盤
            pre_ex = hist[hist.index.date < ex_date]
            if pre_ex.empty:
                fill_days.append(None)
                continue
            pre_close = pre_ex.iloc[-1]["Close"]

            # 找第一日收盤 >= pre_close (填息)
            post_ex = hist[hist.index.date >= ex_date]
            fill_idx = None
            for i, (_, row) in enumerate(post_ex.iterrows()):
                if row["Close"] >= pre_close:
                    fill_idx = i
                    break

            fill_days.append(fill_idx if fill_idx is not None else None)
        except Exception as e:
            print(f"[warn] {ticker} {y} 年填息計算失敗: {e}")
            fill_days.append(None)
        time.sleep(0.2)

    return fill_days


def estimate_expected_decline(fill_days: list[Optional[int]]) -> tuple[float, int]:
    """
    從填息表現估算「除息後 N 日預期貼息跌幅」
    公式：填息越慢、未填次數越多 → 跌幅越大
    回傳: (預期跌幅%, 建議持有日數)
    """
    UNFILLED = 90
    vals = [d if d is not None else UNFILLED for d in fill_days]
    avg = sum(vals) / len(vals)
    unfilled = sum(1 for d in fill_days if d is None)

    # 分級估算
    if avg < 15:
        return (0.5 + unfilled * 0.3, 5)
    elif avg < 25:
        return (1.0 + unfilled * 0.5, 7)
    elif avg < 40:
        return (2.0 + unfilled * 1.0, 10)
    elif avg < 60:
        return (3.5 + unfilled * 1.0, 12)
    else:
        return (5.0 + unfilled * 1.5, 15)


# ─────────────────────────────────────────────────────────────
# 歷史回測 P&L (近 5 年)
# ─────────────────────────────────────────────────────────────
def backtest_history(ticker: str, years: list[int]) -> list[dict]:
    """
    對該股票回測過去 N 年除息後 10 日的放空 P&L
    P&L = (F0 - D - F_exit) * 2000 - 成本
    """
    results = []
    try:
        import yfinance as yf
        tk = yf.Ticker(f"{ticker}.TW")
        divs = tk.dividends

        for y in years:
            year_divs = divs[divs.index.year == y] if not divs.empty else pd.Series()
            if year_divs.empty:
                results.append({"year": y, "pnl": 0, "win": False})
                continue

            ex_date = year_divs.index[0].date()
            div_amount = float(year_divs.iloc[0])

            hist = tk.history(start=ex_date - timedelta(days=5), end=ex_date + timedelta(days=30))
            pre_ex = hist[hist.index.date < ex_date]
            post_ex = hist[hist.index.date >= ex_date]

            if pre_ex.empty or len(post_ex) < 10:
                results.append({"year": y, "pnl": 0, "win": False})
                continue

            f0 = pre_ex.iloc[-1]["Close"]
            f_exit = post_ex.iloc[9]["Close"]  # 第 10 個交易日
            adj_price = f0 - div_amount

            gross = (adj_price - f_exit) * 2000
            cost = (f0 + f_exit) * 2000 * 0.00002 + 60
            pnl = round(gross - cost)
            results.append({"year": y, "pnl": pnl, "win": pnl > 0})
    except Exception as e:
        print(f"[warn] {ticker} 回測失敗: {e}")
        for y in years:
            results.append({"year": y, "pnl": 0, "win": False})

    return results


# ─────────────────────────────────────────────────────────────
# ETF 反向查詢
# ─────────────────────────────────────────────────────────────
def find_etfs_holding(ticker: str, etf_holdings: pd.DataFrame) -> list[dict]:
    """從所有 ETF 持股表中找出哪幾檔有持有該股票"""
    if etf_holdings.empty:
        return []
    matches = etf_holdings[etf_holdings["ticker"] == ticker]
    return [
        {"id": row["etf_id"], "name": row["etf_name"], "weight": float(row["weight"])}
        for _, row in matches.iterrows()
    ]


# ─────────────────────────────────────────────────────────────
# 單檔股票完整資料組合
# ─────────────────────────────────────────────────────────────
def build_stock_record(
    ticker: str,
    name: str,
    sector: str,
    ex_date: date,
    cash_div: float,
    spot: float,
    futures: float,
    fut_volume: int,
    margin_tier: str,
    etf_holdings: pd.DataFrame,
) -> dict:
    """組裝單檔股票的完整紀錄"""

    print(f"  → {ticker} {name}: 抓填息歷史…")
    fill_days = fetch_fill_days_history(ticker, [2023, 2024, 2025])

    print(f"  → {ticker} {name}: 跑回測…")
    history = backtest_history(ticker, [2021, 2022, 2023, 2024, 2025])

    decline_pct, hold_days = estimate_expected_decline(fill_days)
    etfs = find_etfs_holding(ticker, etf_holdings)

    return {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "spotPrice": round(spot, 2),
        "futuresPrice": round(futures, 2),
        "cashDividend": round(cash_div, 4),
        "exDate": ex_date.isoformat(),
        "futuresVolume": int(fut_volume),
        "marginTier": margin_tier,
        "expectedDeclinePct": round(decline_pct, 1),
        "targetHoldDays": hold_days,
        "etfs": etfs,
        "history": history,
        "fillDays": fill_days,
    }


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def build_database(
    days_ahead: int = 30,
    custom_tickers: list = None,
    out_path: str = "data/stocks.json",
):
    tz = ZoneInfo("Asia/Taipei")
    today = datetime.now(tz).date()

    print(f"=== 資料庫更新 {today} ===\n")

    # 1. 除息日曆
    print("[1/5] 抓除息日曆…")
    div_df = fetch_ex_dividend_calendar(today, today + timedelta(days=days_ahead))
    print(f"      → {len(div_df)} 檔")

    # 2. 個股期貨清單
    print("[2/5] 抓個股期貨清單…")
    fut_list = fetch_stock_futures_list()
    fut_close = fetch_futures_close(today - timedelta(days=1))
    print(f"      → {len(fut_list)} 檔有期貨")

    # 3. ETF 持股
    print("[3/5] 抓高股息 ETF 持股…")
    etf_holdings = fetch_all_etf_holdings()
    print(f"      → {etf_holdings['etf_id'].nunique() if len(etf_holdings) else 0} 檔 ETF")

    # 4. 保證金級距
    print("[4/5] 抓保證金級距…")
    margin_tiers = fetch_margin_tiers()
    Path("data").mkdir(exist_ok=True)
    Path(MARGIN_TIER_CACHE_FILE).write_text(json.dumps(margin_tiers, ensure_ascii=False))

    # 5. 主流程：合併除息 ∩ 期貨 ∩ ETF
    print("[5/5] 組合資料…")
    merged = div_df.merge(fut_list, on="ticker", how="inner")
    merged = merged.merge(fut_close, on="futures_code", how="left")

    # 如果有自訂清單，加進來掃描即使沒被內建篩入
    if custom_tickers:
        for t in custom_tickers:
            if t in fut_list["ticker"].values and t not in merged["ticker"].values:
                # 從 fut_list 補上基本資料，除息資料為 None
                fut_row = fut_list[fut_list["ticker"] == t].iloc[0]
                merged = pd.concat([merged, pd.DataFrame([{
                    "ticker": t, "name": fut_row["name"], "ex_date": None,
                    "cash_div": 0, "futures_code": fut_row["futures_code"],
                    "close": None, "volume": 0,
                }])], ignore_index=True)

    stocks = []
    for i, row in merged.iterrows():
        ticker = str(row["ticker"])
        print(f"\n[{i+1}/{len(merged)}] 處理 {ticker}…")
        try:
            stock = build_stock_record(
                ticker=ticker,
                name=row["name"],
                sector="-",  # TODO: 從另一個 API 補
                ex_date=row["ex_date"] if pd.notna(row.get("ex_date")) else today + timedelta(days=365),
                cash_div=float(row.get("cash_div", 0) or 0),
                spot=float(row.get("close", 0) or 0) * 1.005,  # 用期貨價 +0.5% 當現股價估計
                futures=float(row.get("close", 0) or 0),
                fut_volume=int(row.get("volume", 0) or 0),
                margin_tier=margin_tiers.get(ticker, "A"),
                etf_holdings=etf_holdings,
            )
            stocks.append(stock)
        except Exception as e:
            print(f"  ✗ {ticker} 失敗: {e}")
        time.sleep(0.3)

    # 輸出 JSON
    payload = {
        "updated_at": datetime.now(tz).isoformat(),
        "today": today.isoformat(),
        "count": len(stocks),
        "stocks": stocks,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    print(f"\n✓ 完成，共 {len(stocks)} 檔，已寫入 {out_path}")
    return stocks


# ─────────────────────────────────────────────────────────────
# 訂閱者自訂清單聚合
# ─────────────────────────────────────────────────────────────
def collect_custom_tickers() -> list[str]:
    """從訂閱者設定收集所有自訂股票代號，確保它們也被掃描"""
    raw = os.environ.get("SUBSCRIBERS_JSON")
    if not raw:
        return []
    try:
        subs = json.loads(raw)
        tickers = set()
        for s in subs:
            tickers.update(s.get("custom_list", []))
        return sorted(tickers)
    except Exception:
        return []


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--out", default="data/stocks.json")
    parser.add_argument("--custom", nargs="*", default=None, help="額外要強制納入的股票代號")
    args = parser.parse_args()

    # 自訂清單來自訂閱者設定 + CLI 參數
    customs = collect_custom_tickers()
    if args.custom:
        customs.extend(args.custom)
    customs = sorted(set(customs))
    print(f"自訂清單: {customs}")

    build_database(days_ahead=args.days, custom_tickers=customs, out_path=args.out)
