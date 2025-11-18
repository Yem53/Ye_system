# 双向同步脚本 - 先拉取远程更新，再推送本地更改
# 使用方法: .\sync\sync_bidirectional.ps1 [commit_message]

param(
    [string]$CommitMessage = $null
)

Write-Host "开始双向同步..." -ForegroundColor Green

# 第一步：拉取远程更新
Write-Host "`n=== 第一步：从 GitHub 拉取最新代码 ===" -ForegroundColor Cyan
git fetch origin

$localCommit = git rev-parse HEAD
$remoteCommit = git rev-parse origin/main

if ($localCommit -ne $remoteCommit) {
    Write-Host "`n检测到远程更新，正在拉取..." -ForegroundColor Yellow
    
    # 检查本地是否有未提交的更改
    $status = git status --porcelain
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Write-Host "`n⚠️  警告: 本地有未提交的更改，先暂存..." -ForegroundColor Yellow
        git stash
        $stashed = $true
    } else {
        $stashed = $false
    }
    
    git pull origin main
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n❌ 拉取失败，请解决冲突后重试" -ForegroundColor Red
        if ($stashed) {
            Write-Host "恢复暂存的更改..." -ForegroundColor Yellow
            git stash pop
        }
        exit 1
    }
    
    if ($stashed) {
        Write-Host "恢复暂存的更改..." -ForegroundColor Yellow
        git stash pop
    }
    
    Write-Host "✅ 拉取成功" -ForegroundColor Green
} else {
    Write-Host "本地代码已是最新" -ForegroundColor Green
}

# 第二步：检查并推送本地更改
Write-Host "`n=== 第二步：推送本地更改到 GitHub ===" -ForegroundColor Cyan
$status = git status --porcelain

if ([string]::IsNullOrWhiteSpace($status)) {
    Write-Host "没有需要提交的更改" -ForegroundColor Yellow
} else {
    Write-Host "`n检测到本地更改:" -ForegroundColor Yellow
    git status --short
    
    if ([string]::IsNullOrWhiteSpace($CommitMessage)) {
        $CommitMessage = Read-Host "`n请输入提交信息（直接回车使用默认信息）"
        if ([string]::IsNullOrWhiteSpace($CommitMessage)) {
            $CommitMessage = "Auto sync: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        }
    }
    
    Write-Host "`n添加并提交更改..." -ForegroundColor Cyan
    git add -A
    git commit -m $CommitMessage
    
    Write-Host "`n推送到 GitHub..." -ForegroundColor Cyan
    git push origin main
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "`n✅ 推送成功！" -ForegroundColor Green
    } else {
        Write-Host "`n❌ 推送失败" -ForegroundColor Red
    }
}

Write-Host "`n✅ 双向同步完成！" -ForegroundColor Green

