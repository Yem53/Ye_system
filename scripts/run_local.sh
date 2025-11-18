#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJECT_ROOT"

if [ ! -f .env ]; then
  echo "[run_local] 请先复制 .env.example 为 .env 并填写数据库/策略参数"
  exit 1
fi

# 加载 .env 中的变量
set -a
source .env
set +a

if ! command -v uvicorn >/dev/null 2>&1; then
  echo "[run_local] 未检测到 uvicorn，请先在当前虚拟环境中安装依赖 (pip install -r requirements.txt)"
  exit 1
fi

exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --app-dir backend
