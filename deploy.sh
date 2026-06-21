#!/usr/bin/env bash
# 一鍵部署到 GitHub
# 用法：
#   ./deploy.sh <github_username> [repo_name]
# 例：./deploy.sh gary850404 dividend-arb

set -e

USERNAME=${1:?"必須提供 GitHub 帳號：./deploy.sh <username> [repo_name]"}
REPO=${2:-dividend-arb}

echo "═══════════════════════════════════════════════"
echo "  部署 $REPO 到 github.com/$USERNAME/$REPO"
echo "═══════════════════════════════════════════════"
echo ""

# 1. 確認在正確目錄
if [ ! -f "index.html" ]; then
    echo "✗ 找不到 index.html，請在 dividend-arb 資料夾內執行"
    exit 1
fi

# 2. 初始化 git
if [ ! -d ".git" ]; then
    echo "[1/5] 初始化 git..."
    git init -q
    git add .
    git commit -q -m "init"
else
    echo "[1/5] git 已存在，提交更新..."
    git add .
    git diff --staged --quiet || git commit -q -m "update"
fi

# 3. 設定遠端
echo "[2/5] 設定遠端..."
if git remote | grep -q origin; then
    git remote set-url origin "https://github.com/$USERNAME/$REPO.git"
else
    git remote add origin "https://github.com/$USERNAME/$REPO.git"
fi

# 4. 改 main branch
echo "[3/5] 切到 main..."
git branch -M main

# 5. 推
echo "[4/5] 推到 GitHub..."
echo ""
echo "如果這是第一次推：請先到 github.com 建好 repo（不要 README）"
echo "如果已建：按 Enter 繼續"
read -p "Enter 繼續..."

git push -u origin main

# 6. 提示後續
echo ""
echo "═══════════════════════════════════════════════"
echo "  ✓ 推送完成"
echo "═══════════════════════════════════════════════"
echo ""
echo "接下來去 GitHub Web UI 做："
echo ""
echo "1. 開 GitHub Pages："
echo "   https://github.com/$USERNAME/$REPO/settings/pages"
echo "   Source: Deploy from a branch → main / (root) → Save"
echo ""
echo "2. 設定 Secrets："
echo "   https://github.com/$USERNAME/$REPO/settings/secrets/actions"
echo "   新增：SUBSCRIBERS_JSON, SMTP_HOST, SMTP_USER, SMTP_PASS"
echo ""
echo "3. 觸發第一次 Actions："
echo "   https://github.com/$USERNAME/$REPO/actions"
echo "   → 每日資料庫更新 → Run workflow"
echo ""
echo "4. 開網頁（1-2 分鐘後）："
echo "   https://$USERNAME.github.io/$REPO/"
echo "   密碼：0404"
echo ""
