"""本地启动入口：加载 .env 并启动 FastAPI。"""

import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
BACKEND_DIR = BASE_DIR / "backend"

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    raise SystemExit("未找到 .env，请先复制 .env.example 并填写配置")

# 将 backend 目录添加到 Python 路径，以便能够导入 app 模块
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if __name__ == "__main__":
    import logging
    
    # 配置 Uvicorn 日志：减少访问日志的噪音
    # 只记录 WARNING 级别以上的访问日志，减少 INFO 级别的访问日志输出
    uvicorn_logger = logging.getLogger("uvicorn.access")
    uvicorn_logger.setLevel(logging.WARNING)  # 只显示 WARNING 及以上级别的访问日志
    
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        app_dir=str(BACKEND_DIR),
        log_level="info",  # 应用日志级别保持 INFO
        access_log=False,  # 完全禁用访问日志（可选，如果觉得太吵）
    )
