# 从 GitHub 拉取最新代码
# 使用方法: .\sync\pull_from_github.ps1

Write-Host "从 GitHub 拉取最新代码..." -ForegroundColor Green

# 先获取远程更新
Write-Host "`n获取远程更新..." -ForegroundColor Cyan
git fetch origin

# 检查是否有远程更新
$localCommit = git rev-parse HEAD
$remoteCommit = git rev-parse origin/main

if ($localCommit -eq $remoteCommit) {
    Write-Host "`n✅ 本地代码已是最新，无需更新" -ForegroundColor Green
    exit 0
}

# 显示远程更新
Write-Host "`n远程有以下更新:" -ForegroundColor Yellow
git log HEAD..origin/main --oneline

# 检查本地是否有未提交的更改
$status = git status --porcelain
if (-not [string]::IsNullOrWhiteSpace($status)) {
    Write-Host "`n⚠️  警告: 本地有未提交的更改" -ForegroundColor Yellow
    Write-Host "请先提交或暂存本地更改，然后再拉取" -ForegroundColor Yellow
    Write-Host "`n本地更改:" -ForegroundColor Cyan
    git status --short
    Write-Host "`n选项:" -ForegroundColor Cyan
    Write-Host "1. 提交本地更改: git add . && git commit -m '你的提交信息'" -ForegroundColor Cyan
    Write-Host "2. 暂存本地更改: git stash" -ForegroundColor Cyan
    Write-Host "3. 强制拉取（会覆盖本地更改）: git pull --rebase origin main" -ForegroundColor Red
    exit 1
}

# 拉取并合并
Write-Host "`n拉取并合并远程更新..." -ForegroundColor Cyan
$pullResult = git pull origin main 2>&1

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n✅ 拉取成功！" -ForegroundColor Green
    Write-Host "`n最新提交:" -ForegroundColor Cyan
    git log --oneline -5
} else {
    Write-Host "`n❌ 拉取失败" -ForegroundColor Red
    Write-Host $pullResult -ForegroundColor Red
    Write-Host "`n可能需要解决冲突，请手动处理" -ForegroundColor Yellow
}

