param(
    [int]$Port = 8000
)

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
Set-Location $projectRoot

if (-not (Test-Path ".env")) {
    Write-Host "[run_local] 请先复制 .env.example 为 .env 并填写数据库/策略参数" -ForegroundColor Yellow
    exit 1
}

Get-Content .env | Where-Object {$_ -and ($_ -notmatch '^#')} | ForEach-Object {
    $pair = $_.Split('=',2)
    if ($pair.Count -eq 2) {
        $key = $pair[0].Trim()
        $value = $pair[1].Trim('", "')
        [System.Environment]::SetEnvironmentVariable($key, $value)
    }
}

if (-not (Get-Command uvicorn -ErrorAction SilentlyContinue)) {
    Write-Host "[run_local] 未检测到 uvicorn，请先在当前虚拟环境中安装依赖 (pip install -r requirements.txt)" -ForegroundColor Yellow
    exit 1
}

$env:PYTHONPATH = "$projectRoot/backend"
uvicorn app.main:app --host 127.0.0.1 --port $Port --reload --app-dir backend
