from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings

settings = get_settings()


database_url = settings.database_url

if database_url.startswith("postgresql+asyncpg"):
    # 强制改用同步 psycopg 驱动，避免 MissingGreenlet 错误
    sync_url = database_url.replace("postgresql+asyncpg", "postgresql+psycopg", 1)
    engine = create_engine(sync_url, future=True)
else:
    engine = create_engine(database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
