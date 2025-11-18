# 使用 Token 推送代码到 GitHub
# 使用方法: .\push_to_github.ps1

# 从环境变量获取 token，如果没有则提示输入
$token = $env:GITHUB_TOKEN
if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "请设置环境变量 GITHUB_TOKEN 或在此脚本中设置 token" -ForegroundColor Yellow
    Write-Host "例如: `$env:GITHUB_TOKEN = 'your_token_here'" -ForegroundColor Yellow
    exit 1
}
$username = "Yem53"
$repo = "Ye_system"

Write-Host "准备推送到 GitHub..." -ForegroundColor Green
Write-Host "仓库: https://github.com/$username/$repo" -ForegroundColor Cyan
Write-Host ""

# 显示待推送的提交
Write-Host "待推送的提交:" -ForegroundColor Yellow
git log --oneline -5
Write-Host ""

# 使用 token 推送
$pushUrl = "https://${username}:${token}@github.com/${username}/${repo}.git"

Write-Host "正在推送..." -ForegroundColor Cyan
$result = git push $pushUrl main 2>&1

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✅ 推送成功！" -ForegroundColor Green
    Write-Host "查看仓库: https://github.com/$username/$repo" -ForegroundColor Cyan
} else {
    Write-Host ""
    Write-Host "❌ 推送失败" -ForegroundColor Red
    Write-Host $result -ForegroundColor Red
    Write-Host ""
    Write-Host "可能的原因:" -ForegroundColor Yellow
    Write-Host "1. GitHub 服务器临时问题（500/503 错误）- 请稍后重试" -ForegroundColor Yellow
    Write-Host "2. Token 权限不足 - 检查 token 是否有 'repo' 权限" -ForegroundColor Yellow
    Write-Host "3. 网络连接问题 - 检查网络连接" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "可以尝试:" -ForegroundColor Cyan
    Write-Host "1. 等待几分钟后重试" -ForegroundColor Cyan
    Write-Host "2. 检查 GitHub 状态: https://www.githubstatus.com/" -ForegroundColor Cyan
    Write-Host "3. 使用浏览器访问仓库确认仓库存在" -ForegroundColor Cyan
}

