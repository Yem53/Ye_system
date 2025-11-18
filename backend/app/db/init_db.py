from app.db import base  # noqa: F401
from app.db.session import engine
from app.models import (
    announcement,
    announcement_return,
    execution_log,
    manual_plan,
    position,
    trade_analysis,
    trade_plan,
)  # noqa: F401


def init_db() -> None:
    base.Base.metadata.create_all(bind=engine)
