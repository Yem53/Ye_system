# GitHub Token 配置脚本
# 此脚本会配置 Git 使用 Personal Access Token

# 从环境变量获取 token，如果没有则提示输入
$token = $env:GITHUB_TOKEN
if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "请设置环境变量 GITHUB_TOKEN" -ForegroundColor Yellow
    Write-Host "例如: `$env:GITHUB_TOKEN = 'your_token_here'" -ForegroundColor Yellow
    exit 1
}
$username = "Yem53"
$repoUrl = "https://github.com/Yem53/Ye_system.git"

Write-Host "配置 GitHub 凭据..." -ForegroundColor Green

# 使用 Windows Credential Manager 存储凭据
$credential = "https://${username}:${token}@github.com"
git credential approve $credential

Write-Host "✅ 凭据已配置" -ForegroundColor Green
Write-Host ""
Write-Host "尝试推送代码..." -ForegroundColor Cyan

# 尝试推送
git push origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✅ 推送成功！" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "⚠️  推送失败，可能是 GitHub 服务器问题，请稍后重试" -ForegroundColor Yellow
    Write-Host "   可以运行: git push origin main" -ForegroundColor Yellow
}

