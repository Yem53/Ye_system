from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import get_settings

settings = get_settings()


database_url = settings.database_url

# 优化数据库连接池配置（支持更多并发操作）
pool_config = {
    "poolclass": QueuePool,
    "pool_size": 20,  # 增加连接池大小（默认5）
    "max_overflow": 40,  # 增加溢出连接数（默认10）
    "pool_pre_ping": True,  # 连接前ping，确保连接有效
    "pool_recycle": 3600,  # 1小时后回收连接
}

if database_url.startswith("postgresql+asyncpg"):
    # 强制改用同步 psycopg 驱动，避免 MissingGreenlet 错误
    sync_url = database_url.replace("postgresql+asyncpg", "postgresql+psycopg", 1)
    engine = create_engine(sync_url, future=True, **pool_config)
else:
    engine = create_engine(database_url, future=True, **pool_config)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
