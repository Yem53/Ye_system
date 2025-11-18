#!/bin/bash
# 从 GitHub 拉取最新代码
# 使用方法: ./sync/pull_from_github.sh

echo "从 GitHub 拉取最新代码..."

# 先获取远程更新
echo ""
echo "获取远程更新..."
git fetch origin

# 检查是否有远程更新
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo ""
    echo "✅ 本地代码已是最新，无需更新"
    exit 0
fi

# 显示远程更新
echo ""
echo "远程有以下更新:"
git log HEAD..origin/main --oneline

# 检查本地是否有未提交的更改
if [ -n "$(git status --porcelain)" ]; then
    echo ""
    echo "⚠️  警告: 本地有未提交的更改"
    echo "请先提交或暂存本地更改，然后再拉取"
    echo ""
    echo "本地更改:"
    git status --short
    echo ""
    echo "选项:"
    echo "1. 提交本地更改: git add . && git commit -m '你的提交信息'"
    echo "2. 暂存本地更改: git stash"
    echo "3. 强制拉取（会覆盖本地更改）: git pull --rebase origin main"
    exit 1
fi

# 拉取并合并
echo ""
echo "拉取并合并远程更新..."
if git pull origin main; then
    echo ""
    echo "✅ 拉取成功！"
    echo ""
    echo "最新提交:"
    git log --oneline -5
else
    echo ""
    echo "❌ 拉取失败"
    echo "可能需要解决冲突，请手动处理"
fi

