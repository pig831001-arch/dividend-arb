"""
fetch_exdates.py — 每日自動更新 data/stocks.json
=================================================
- 從 wantgoo.com 抓各股除息日、現金股利
- 從 TWSE MIS API 抓即時/前收股價
- 合併寫出 data/stocks.json 供前端讀取

執行：
    python fetch_exdates.py

GitHub Actions 每天 09:30 台北時間自動跑 (見 .github/workflows/daily-update.yml)
"""

import json
import time
import re
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Taipei")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ── 要追蹤的股票清單 (上市用 tse, 上櫃用 otc) ──────────────────
STOCKS = [
    {"ticker": "2882", "name": "國泰金",   "sector": "金融",       "exchange": "tse"},
    {"ticker": "2317", "name": "鴻海",     "sector": "電子代工",   "exchange": "tse"},
    {"ticker": "2357", "name": "華碩",     "sector": "電腦週邊",   "exchange": "tse"},
    {"ticker": "3264", "name": "欣銓",     "sector": "半導體封測", "exchange": "tse"},
    {"ticker": "5347", "name": "世界",     "sector": "半導體晶圓代工", "exchange": "otc"},
    {"ticker": "3231", "name": "緯創",     "sector": "電子代工",   "exchange": "tse"},
    {"ticker": "2603", "name": "長榮",     "sector": "航運",       "exchange": "tse"},
    {"ticker": "2308", "name": "台達電",   "sector": "電子零組件", "exchange": "tse"},
    {"ticker": "1319", "name": "東陽",     "sector": "汽車零組件", "exchange": "tse"},
    {"ticker": "2891", "name": "中信金",   "sector": "金融",       "exchange": "tse"},
    {"ticker": "2412", "name": "中華電",   "sector": "電信",       "exchange": "tse"},
    {"ticker": "1101", "name": "台泥",     "sector": "水泥",       "exchange": "tse"},
]


# ── 1. 抓即時/前收股價 (TWSE MIS API) ─────────────────────────
def fetch_spot_prices(stocks: list) -> dict:
    """
    從 TWSE MIS 批次抓股價。
    回傳 {ticker: price}
    盤中為即時價，盤後/假日為前收價。
    """
    ex_ch = "|".join(f"{s['exchange']}_{s['ticker']}.tw" for s in stocks)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={requests.utils.quote(ex_ch)}&_={int(datetime.now().timestamp()*1000)}"

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()

        prices = {}
        for item in data.get("msgArray", []):
            ticker = item.get("c", "")
            raw = item.get("z", "-")
            if raw == "-" or not raw:
                raw = item.get("y", "0")  # 用前收
            try:
                price = float(raw)
                if price > 0:
                    prices[ticker] = price
            except ValueError:
                pass

        print(f"[股價] 取得 {len(prices)}/{len(stocks)} 檔")
        return prices
    except Exception as e:
        print(f"[warn] 股價抓取失敗: {e}")
        return {}


# ── 2. 從 wantgoo 抓單檔除息資料 ──────────────────────────────
def fetch_wantgoo_exdate(ticker: str) -> dict | None:
    """
    從 wantgoo.com 抓除息日與現金股利。
    回傳 {"exDate": "2026-06-30", "cashDividend": 3.5} 或 None
    """
    url = f"https://www.wantgoo.com/stock/{ticker}/dividend-policy/ex-dividend"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        html = r.text

        # 找 2026 那一行 (或當年度)
        current_year = datetime.now(TZ).year

        # wantgoo 表格格式：<td>2026</td> ... <td>M/D</td> ... <td>X.XX</td>
        # 用 regex 抓年份對應行
        # 抓整段表格
        table_match = re.search(r'<table[^>]*>.*?</table>', html, re.DOTALL)
        if not table_match:
            return None

        table_html = table_match.group(0)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)

        for row in rows:
            cells = re.findall(r'<td[^>]*>\s*(.*?)\s*</td>', row, re.DOTALL)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

            # 找包含當年度的行
            if not cells or str(current_year) not in cells[0]:
                continue

            # 嘗試解析除息日 (格式: M/D 或 YYYY-MM-DD 或 --/-- 表示未公告)
            ex_date_str = None
            cash_div = None

            for i, cell in enumerate(cells):
                # 找日期 (M/D 格式)
                date_match = re.match(r'^(\d{1,2})/(\d{1,2})$', cell)
                if date_match and ex_date_str is None:
                    m, d = date_match.groups()
                    ex_date_str = f"{current_year}-{int(m):02d}-{int(d):02d}"
                    continue

                # 找現金股利 (數字)
                if re.match(r'^\d+\.?\d*$', cell) and cash_div is None and float(cell) > 0:
                    cash_div = float(cell)

            if ex_date_str and cash_div:
                return {"exDate": ex_date_str, "cashDividend": cash_div, "estimated": False}
            elif ex_date_str is None:
                # 日期未公告，標記為 estimated
                return {"exDate": None, "cashDividend": cash_div, "estimated": True}

        return None
    except Exception as e:
        print(f"  [warn] {ticker} wantgoo 抓取失敗: {e}")
        return None


# ── 3. 從 TWSE 除息日曆批次抓 (補充 wantgoo 的缺漏) ──────────
def fetch_twse_exdiv_calendar() -> dict:
    """
    從 TWSE TWT49U 抓未來 90 天除息日曆。
    回傳 {ticker: {"exDate": "...", "cashDividend": X}}
    """
    today = date.today()
    from datetime import timedelta
    end = today + timedelta(days=90)

    url = "https://www.twse.com.tw/exchangeReport/TWT49U"
    params = {
        "response": "json",
        "strDate": today.strftime("%Y%m%d"),
        "endDate": end.strftime("%Y%m%d"),
    }

    result = {}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()

        rows = data.get("data", [])
        # TWSE JSON 格式：[股票代號, 名稱, 除息日(民國), 現金股利, ...]
        for row in rows:
            if len(row) < 4:
                continue
            ticker = str(row[0]).strip()
            roc_date = str(row[2]).strip()  # 民國日期 e.g. "115/07/01"
            cash_div_str = str(row[3]).strip()

            # 民國轉西元
            try:
                parts = roc_date.split("/")
                ad_year = int(parts[0]) + 1911
                ex_date = f"{ad_year}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            except Exception:
                continue

            try:
                cash_div = float(cash_div_str)
                if cash_div > 0:
                    result[ticker] = {"exDate": ex_date, "cashDividend": cash_div, "estimated": False}
            except ValueError:
                pass

        print(f"[TWSE日曆] 取得 {len(result)} 筆除息資料")
    except Exception as e:
        print(f"[warn] TWSE 除息日曆抓取失敗: {e}")

    return result


# ── 主流程 ─────────────────────────────────────────────────────
def main():
    print(f"=== fetch_exdates.py  {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ===\n")

    # 1. 股價
    print("[1/3] 抓即時股價 (TWSE MIS)…")
    prices = fetch_spot_prices(STOCKS)

    # 2. TWSE 批次除息日曆
    print("\n[2/3] 抓 TWSE 除息日曆…")
    twse_exdiv = fetch_twse_exdiv_calendar()

    # 3. 逐股補充 (wantgoo 抓 TWSE 沒有的)
    print("\n[3/3] 補充個股除息資料 (wantgoo)…")
    wantgoo_exdiv = {}
    for s in STOCKS:
        ticker = s["ticker"]
        if ticker in twse_exdiv:
            print(f"  {ticker} {s['name']}: TWSE 已有資料 → {twse_exdiv[ticker]['exDate']}")
            continue
        print(f"  {ticker} {s['name']}: 向 wantgoo 查詢…", end=" ")
        info = fetch_wantgoo_exdate(ticker)
        if info:
            wantgoo_exdiv[ticker] = info
            print(f"→ {info.get('exDate', '未公告')}")
        else:
            print("→ 未取得")
        time.sleep(1.0)  # 避免過快請求

    # 4. 合併現有 stocks.json (保留前端的 ETF/history 等欄位)
    existing = {}
    stocks_json_path = Path("data/stocks.json")
    if stocks_json_path.exists():
        try:
            old = json.loads(stocks_json_path.read_text())
            existing = {s["ticker"]: s for s in old.get("stocks", [])}
        except Exception:
            pass

    # 5. 組合輸出
    output_stocks = []
    for s in STOCKS:
        ticker = s["ticker"]

        # 基礎資料來自已有 stocks.json (保留 ETF/history 等)
        record = dict(existing.get(ticker, {}))

        # 必填欄位
        record["ticker"] = ticker
        record["name"] = s["name"]
        record["sector"] = s["sector"]

        # 股價更新
        if ticker in prices:
            record["spotPrice"] = prices[ticker]
            record["futuresPrice"] = round(prices[ticker] * 0.995, 2)  # 期貨估算 (近似)
        elif "spotPrice" not in record:
            record["spotPrice"] = 0

        # 除息日更新 (優先 TWSE > wantgoo > 保留舊資料)
        if ticker in twse_exdiv:
            record.update(twse_exdiv[ticker])
        elif ticker in wantgoo_exdiv:
            record.update(wantgoo_exdiv[ticker])
        # else: 保留既有的 exDate/cashDividend

        # 預設值 (若沒有)
        record.setdefault("cashDividend", 0)
        record.setdefault("exDate", "")
        record.setdefault("estimated", True)
        record.setdefault("futuresVolume", 0)
        record.setdefault("marginTier", "A")
        record.setdefault("expectedDeclinePct", 2.0)
        record.setdefault("targetHoldDays", 7)
        record.setdefault("etfs", [])
        record.setdefault("history", [])
        record.setdefault("fillDays", [None, None, None])

        output_stocks.append(record)

    payload = {
        "updated_at": datetime.now(TZ).isoformat(),
        "today": date.today().isoformat(),
        "count": len(output_stocks),
        "stocks": output_stocks,
    }

    Path("data").mkdir(exist_ok=True)
    stocks_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n✓ 完成：{len(output_stocks)} 檔寫入 data/stocks.json")

    # 輸出摘要
    print("\n── 最新除息資料 ──")
    for s in output_stocks:
        status = "★" if not s.get("estimated") else "估"
        print(f"  [{status}] {s['ticker']} {s['name']:6s}  除息 {s.get('exDate','未知'):12s}  股利 {s.get('cashDividend', 0):.2f}  現股 {s.get('spotPrice', 0):.2f}")


if __name__ == "__main__":
    main()
