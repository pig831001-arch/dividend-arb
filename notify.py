"""
每日除息掃描通知
================
每天 09:00 (Asia/Taipei) 跑
  1. 從 data/stocks.json 讀取後端最新掃描結果
  2. 套用訂閱者的策略條件篩選
  3. 寄一封「今日掃描摘要」email

部署：透過 GitHub Actions 每天自動跑 (見 daily-notify.yml)

環境變數：
  SUBSCRIBERS_JSON       訂閱者清單 (JSON 字串)
  SMTP_HOST / SMTP_USER / SMTP_PASS    Gmail SMTP 設定
  SENDGRID_API_KEY       選用，若設定則優先用 SendGrid

執行：
  pip install requests
  python notify.py [--dry-run]
"""

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass
class Subscriber:
    email: str
    custom_list: list = field(default_factory=list)
    min_fill_days_avg: int = 20
    max_capital_per_stock: int = 300_000
    preset: str = "xiaoge"


def load_subscribers() -> list:
    raw = os.environ.get("SUBSCRIBERS_JSON")
    if raw:
        data = json.loads(raw)
    else:
        data = [{
            "email": "850404gary@gmail.com",
            "min_fill_days_avg": 20,
            "max_capital_per_stock": 300_000,
            "preset": "xiaoge",
        }]
    return [Subscriber(**d) for d in data]


# 策略風格 (與前端 PRESETS 同步)
PRESETS = {
    "xiaoge":     {"name": "高勝率型", "min_fill_days": 30, "min_unfilled": 1, "min_win_rate": 60, "hold_days": 5,  "max_stocks": 5},
    "aggressive": {"name": "進取",     "min_fill_days": 20, "min_unfilled": 0, "min_win_rate": 50, "hold_days": 10, "max_stocks": 8},
    "custom":     {"name": "客製",     "min_fill_days": 20, "min_unfilled": 0, "min_win_rate": 0,  "hold_days": 5,  "max_stocks": 5},
}


UNFILLED_PENALTY = 90


def avg_fill(fill_days):
    vals = [d if d is not None else UNFILLED_PENALTY for d in fill_days]
    return round(sum(vals) / len(vals)) if vals else 0


def unfilled_count(fill_days):
    return sum(1 for d in fill_days if d is None)


def win_rate(history):
    if not history: return 0
    return round(sum(1 for h in history if h.get("win")) / len(history) * 100)


def filter_stocks(stocks, sub, today):
    preset = PRESETS.get(sub.preset, PRESETS["xiaoge"])
    result = []
    for s in stocks:
        try:
            ex_date = datetime.fromisoformat(s["exDate"]).date()
        except Exception:
            continue
        days_to_ex = (ex_date - today).days
        if days_to_ex < 0 or days_to_ex > 35: continue
        if avg_fill(s.get("fillDays", [])) <= preset["min_fill_days"]: continue
        if unfilled_count(s.get("fillDays", [])) < preset["min_unfilled"]: continue
        if win_rate(s.get("history", [])) < preset["min_win_rate"]: continue
        s["_daysToEx"] = days_to_ex
        s["_avgFillDays"] = avg_fill(s.get("fillDays", []))
        result.append(s)
    result.sort(key=lambda x: x["_daysToEx"])
    return result[:preset["max_stocks"]]


def build_email_html(stocks, sub, today):
    preset = PRESETS.get(sub.preset, PRESETS["xiaoge"])

    if not stocks:
        rows_html = '<tr><td colspan="6" style="padding:20px; text-align:center; color:#6B6B6B;">今日無符合條件的標的</td></tr>'
    else:
        rows_html = "\n".join(
            f'''
            <tr style="border-bottom:1px solid #E0DDD4;">
              <td style="padding:10px 8px; font-family:monospace; font-weight:600;">{s["ticker"]}</td>
              <td style="padding:10px 8px;">{s["name"]}</td>
              <td style="padding:10px 8px; font-family:monospace;">{s["exDate"][5:]}</td>
              <td style="padding:10px 8px; font-family:monospace; text-align:right; color:#B45309; font-weight:600;">T−{s["_daysToEx"]}</td>
              <td style="padding:10px 8px; font-family:monospace; text-align:right;">{s.get("cashDividend", 0):.2f}</td>
              <td style="padding:10px 8px; font-family:monospace; text-align:right;">{s["_avgFillDays"]}日</td>
            </tr>
            ''' for s in stocks
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:'Helvetica Neue', sans-serif; background:#F5F4EF; padding:20px; margin:0;">
  <div style="max-width:640px; margin:0 auto; background:#FFF; border:1px solid #E0DDD4; padding:32px;">
    <div style="font-family:monospace; font-size:11px; color:#6B6B6B; letter-spacing:0.15em; text-transform:uppercase;">
      Taiwan · Dividend Arbitrage Desk
    </div>
    <h1 style="font-size:24px; margin:8px 0 4px; color:#1A1A1A;">
      每日掃描摘要 <span style="color:#B45309;">{today.strftime("%Y/%m/%d")}</span>
    </h1>
    <div style="font-size:13px; color:#6B6B6B; margin-bottom:24px;">
      策略：<strong>{preset["name"]}</strong> · 篩選後 {len(stocks)} 檔候選
    </div>
    <table style="width:100%; border-collapse:collapse;">
      <thead>
        <tr style="border-bottom:2px solid #1A1A1A;">
          <th style="padding:8px; text-align:left; font-size:10px; color:#6B6B6B; text-transform:uppercase;">代號</th>
          <th style="padding:8px; text-align:left; font-size:10px; color:#6B6B6B; text-transform:uppercase;">名稱</th>
          <th style="padding:8px; text-align:left; font-size:10px; color:#6B6B6B; text-transform:uppercase;">除息日</th>
          <th style="padding:8px; text-align:right; font-size:10px; color:#6B6B6B; text-transform:uppercase;">距今</th>
          <th style="padding:8px; text-align:right; font-size:10px; color:#6B6B6B; text-transform:uppercase;">股利</th>
          <th style="padding:8px; text-align:right; font-size:10px; color:#6B6B6B; text-transform:uppercase;">填息</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    <div style="margin-top:24px; padding:12px; background:#FAFAF7; border-left:3px solid #B45309; font-size:12px; line-height:1.6; color:#6B6B6B;">
      <strong style="color:#1A1A1A;">使用方式：</strong>進入完整掃描頁，看每檔的進場價、保證金、預期跌幅、年化 ROI。
    </div>
    <hr style="border:none; border-top:1px solid #E0DDD4; margin:20px 0;">
    <p style="color:#B45309; font-size:11px;">⚠ 研究輔助通知，非投資建議。實際下單請自行確認最新報價。</p>
  </div>
</body></html>"""


def send_email(to_email, subject, html_body, dry_run=False):
    if dry_run:
        print(f"[dry-run] → {to_email}: {subject}")
        return True
    if os.environ.get("SENDGRID_API_KEY"):
        return send_via_sendgrid(to_email, subject, html_body)
    return send_via_smtp(to_email, subject, html_body)


def send_via_sendgrid(to_email, subject, html_body):
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, From, To, Subject, HtmlContent
        message = Mail(
            from_email=From(os.environ.get("FROM_EMAIL", "notify@arb-scanner.app"), "套利掃描器"),
            to_emails=To(to_email),
            subject=Subject(subject),
            html_content=HtmlContent(html_body),
        )
        sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        response = sg.send(message)
        if response.status_code in (200, 202):
            print(f"[sent via SendGrid] → {to_email}")
            return True
        print(f"[fail] SendGrid HTTP {response.status_code}")
        return False
    except Exception as e:
        print(f"[error] SendGrid: {e}")
        return False


def send_via_smtp(to_email, subject, html_body):
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    if not (smtp_user and smtp_pass):
        print(f"[skip] SMTP 憑證未設定，跳過 {to_email}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                          int(os.environ.get("SMTP_PORT", "587"))) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        print(f"[sent via SMTP] → {to_email}")
        return True
    except Exception as e:
        print(f"[error] SMTP: {e}")
        return False


def load_stocks():
    path = Path("data/stocks.json")
    if not path.exists():
        print(f"[error] {path} 不存在，請先跑 update_db.py")
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("stocks", [])
    except Exception as e:
        print(f"[error] 讀取失敗: {e}")
        return []


def main(dry_run=False):
    tz = ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    today = now.date()
    print(f"=== 每日掃描通知 {now.isoformat()} ===\n")

    print("[1/3] 讀取 stocks.json…")
    stocks = load_stocks()
    print(f"      → {len(stocks)} 檔資料")
    if not stocks:
        print("[abort] 無資料，跳出")
        return

    print("[2/3] 讀取訂閱者…")
    subscribers = load_subscribers()
    print(f"      → {len(subscribers)} 位訂閱者")

    print("[3/3] 寄送…")
    sent = 0
    for sub in subscribers:
        filtered = filter_stocks(stocks, sub, today)
        subject = f"[掃描摘要] {today.strftime('%m/%d')} {len(filtered)} 檔候選"
        html = build_email_html(filtered, sub, today)
        if send_email(sub.email, subject, html, dry_run):
            sent += 1

    print(f"\n✓ 完成，共發送 {sent} 封")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
