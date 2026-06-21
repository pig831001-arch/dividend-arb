# 高股息個股期貨放空套利掃描器

自動每天抓最新台股除息資料、計算放空套利機會、寄信通知。

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `index.html` | 前端網頁（密碼 `0404` 登入） |
| `pipeline.py` | TWSE / TAIFEX / SITCA 資料抓取 |
| `update_db.py` | 每日跑：抓資料 → 算指標 → 寫 stocks.json |
| `notify.py` | 每日跑：讀 stocks.json → 寄 email |
| `daily-update.yml` | GitHub Actions：每天 09:30 跑 update_db |
| `daily-notify.yml` | GitHub Actions：每天 09:00 跑 notify |

## 部署架構

```
GitHub Repo
├── index.html                  ← 部署到 GitHub Pages
├── pipeline.py
├── update_db.py
├── notify.py
├── data/stocks.json            ← Actions 自動 commit
└── .github/workflows/
    ├── daily-update.yml        ← 09:30 排程
    └── daily-notify.yml        ← 09:00 排程
```

## 快速部署（手機 30 分鐘搞定）

### 1. 建 Repository
1. github.com 註冊/登入
2. 右上 `+` → New repository
3. 名稱：`dividend-arb`，Public，勾 Add README

### 2. 上傳檔案
在 repo 點 Add file → Upload files，選：
- `index.html`
- `pipeline.py`
- `update_db.py`
- `notify.py`

Commit changes。

### 3. 建 Actions 排程
重要：用 Create new file 輸入路徑會自動建資料夾。

- Add file → Create new file → 檔名 `.github/workflows/daily-update.yml`
- 貼上 daily-update.yml 內容 → Commit
- 重複上面，建 `.github/workflows/daily-notify.yml`

### 4. 開啟 GitHub Pages
- Settings → Pages
- Source: Deploy from a branch
- Branch: main / (root) → Save
- 1-2 分鐘後拿到網址：`https://<帳號>.github.io/dividend-arb/`

### 5. 設定 Secrets
Settings → Secrets and variables → Actions → New repository secret

| Name | 值 |
|---|---|
| `SUBSCRIBERS_JSON` | `[{"email":"你的email","preset":"xiaoge","min_fill_days_avg":20,"max_capital_per_stock":300000}]` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_USER` | 你的 Gmail |
| `SMTP_PASS` | Gmail 應用程式密碼 16 字元 |

**Gmail 應用程式密碼怎麼拿**：
1. `accounts.google.com/security`
2. 兩步驟驗證先開
3. 應用程式密碼 → 建立 → 複製 16 字元

### 6. 第一次手動觸發
- 進 Actions 分頁
- 左側「每日資料庫更新」
- Run workflow → Run workflow
- 等綠勾 (3-5 分鐘)

### 7. 驗證
打開你的 GitHub Pages 網址（密碼 `0404`），右上角應該顯示「後端 N 檔 · 更新 時間」。

## 之後完全自動

每天 09:30 自動更新資料 → 09:00 隔天寄信。你只要打開網頁、收 email 就好。

## 常見問題

**Q: Actions 紅叉**
看 log，最常見是 TWSE API 格式變了，要修 `pipeline.py`。

**Q: Email 收不到**
- 應用程式密碼有沒有對
- 檢查垃圾郵件夾
- 改用 SendGrid（`SENDGRID_API_KEY` 設一下即可自動切換）

**Q: 網頁顯示「未連接後端資料庫」**
`data/stocks.json` 還沒生成，手動跑一次 Actions 就解。

## 本機測試

```bash
pip install requests pandas beautifulsoup4 lxml openpyxl yfinance sendgrid

# 1. 抓資料
python update_db.py --days 35 --out data/stocks.json

# 2. 測試寄信（不會真的寄）
python notify.py --dry-run

# 3. 開網頁（雙擊 index.html 即可）
```

## 重要參數調整

`update_db.py` 內：
- `UNFILLED_PENALTY = 90`：未填息懲罰值
- 預期跌幅估算邏輯：`estimate_expected_decline()`

`notify.py` 內：
- `PRESETS` 字典：策略風格參數，需與前端 `PRESETS` 同步

前端 `index.html` 內：
- `PASSWORD = "0404"`：登入密碼
- `USER_EMAIL`：顯示用 email
- `DATA = [...]`：內建範例資料（後端 stocks.json 會覆蓋）

## 風險聲明

研究輔助工具，非投資建議。實際下單前確認最新報價、保證金、流動性。

預期跌幅是歷史推估，下一年市場結構（ETF 規模、利率環境）改變會偏離。
