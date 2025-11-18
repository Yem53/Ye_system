# 推送本地更改到 GitHub
# 使用方法: .\sync\push_to_github.ps1 [commit_message]

param(
    [string]$CommitMessage = "Auto sync: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
)

Write-Host "推送本地更改到 GitHub..." -ForegroundColor Green

# 检查是否有更改
$status = git status --porcelain
if ([string]::IsNullOrWhiteSpace($status)) {
    Write-Host "没有需要提交的更改" -ForegroundColor Yellow
    exit 0
}

# 显示更改
Write-Host "`n检测到以下更改:" -ForegroundColor Cyan
git status --short

# 添加所有更改
Write-Host "`n添加所有更改..." -ForegroundColor Cyan
git add -A

# 提交更改
Write-Host "提交更改: $CommitMessage" -ForegroundColor Cyan
git commit -m $CommitMessage

# 推送到 GitHub
Write-Host "`n推送到 GitHub..." -ForegroundColor Cyan
$pushResult = git push origin main 2>&1

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n✅ 推送成功！" -ForegroundColor Green
} else {
    Write-Host "`n❌ 推送失败，可能需要身份验证" -ForegroundColor Red
    Write-Host $pushResult -ForegroundColor Red
    Write-Host "`n提示: 如果遇到身份验证问题，请使用以下方式之一:" -ForegroundColor Yellow
    Write-Host "1. 使用 Personal Access Token (推荐)" -ForegroundColor Yellow
    Write-Host "2. 配置 SSH 密钥" -ForegroundColor Yellow
    Write-Host "3. 使用 GitHub CLI (gh auth login)" -ForegroundColor Yellow
}

