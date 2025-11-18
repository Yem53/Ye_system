#!/bin/bash
# 自动同步脚本 - 将本地更改推送到 GitHub
# 使用方法: ./scripts/auto_sync.sh [commit_message]

COMMIT_MESSAGE="${1:-Auto sync: $(date '+%Y-%m-%d %H:%M:%S')}"

echo "开始自动同步到 GitHub..."

# 检查是否有更改
if [ -z "$(git status --porcelain)" ]; then
    echo "没有需要提交的更改"
    exit 0
fi

# 显示更改
echo ""
echo "检测到以下更改:"
git status --short

# 添加所有更改
echo ""
echo "添加所有更改..."
git add -A

# 提交更改
echo "提交更改: $COMMIT_MESSAGE"
git commit -m "$COMMIT_MESSAGE"

# 推送到 GitHub
echo ""
echo "推送到 GitHub..."
if git push origin main; then
    echo ""
    echo "✅ 同步成功！"
else
    echo ""
    echo "❌ 推送失败，可能需要身份验证"
    echo ""
    echo "提示: 如果遇到身份验证问题，请使用以下方式之一:"
    echo "1. 使用 Personal Access Token (推荐)"
    echo "2. 配置 SSH 密钥"
    echo "3. 使用 GitHub CLI (gh auth login)"
fi

