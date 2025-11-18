#!/bin/bash
# 双向同步脚本 - 先拉取远程更新，再推送本地更改
# 使用方法: ./sync/sync_bidirectional.sh [commit_message]

COMMIT_MESSAGE="${1:-}"

echo "开始双向同步..."

# 第一步：拉取远程更新
echo ""
echo "=== 第一步：从 GitHub 拉取最新代码 ==="
git fetch origin

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo ""
    echo "检测到远程更新，正在拉取..."
    
    # 检查本地是否有未提交的更改
    if [ -n "$(git status --porcelain)" ]; then
        echo ""
        echo "⚠️  警告: 本地有未提交的更改，先暂存..."
        git stash
        STASHED=true
    else
        STASHED=false
    fi
    
    if git pull origin main; then
        if [ "$STASHED" = true ]; then
            echo "恢复暂存的更改..."
            git stash pop
        fi
        echo "✅ 拉取成功"
    else
        echo ""
        echo "❌ 拉取失败，请解决冲突后重试"
        if [ "$STASHED" = true ]; then
            echo "恢复暂存的更改..."
            git stash pop
        fi
        exit 1
    fi
else
    echo "本地代码已是最新"
fi

# 第二步：检查并推送本地更改
echo ""
echo "=== 第二步：推送本地更改到 GitHub ==="
if [ -z "$(git status --porcelain)" ]; then
    echo "没有需要提交的更改"
else
    echo ""
    echo "检测到本地更改:"
    git status --short
    
    if [ -z "$COMMIT_MESSAGE" ]; then
        read -p "请输入提交信息（直接回车使用默认信息）: " COMMIT_MESSAGE
        if [ -z "$COMMIT_MESSAGE" ]; then
            COMMIT_MESSAGE="Auto sync: $(date '+%Y-%m-%d %H:%M:%S')"
        fi
    fi
    
    echo ""
    echo "添加并提交更改..."
    git add -A
    git commit -m "$COMMIT_MESSAGE"
    
    echo ""
    echo "推送到 GitHub..."
    if git push origin main; then
        echo ""
        echo "✅ 推送成功！"
    else
        echo ""
        echo "❌ 推送失败"
    fi
fi

echo ""
echo "✅ 双向同步完成！"

