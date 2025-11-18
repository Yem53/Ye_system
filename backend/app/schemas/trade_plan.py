from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import TradePlanStatus


class TradePlanBase(BaseModel):
    announcement_id: str
    leverage: float
    position_pct: float
    min_allocation: float | None = None
    max_allocation: float | None = None
    trailing_exit_pct: float
    stop_loss_pct: float
    planned_entry_time: datetime | None = None
    extra: dict[str, Any] | None = None


class TradePlanCreate(TradePlanBase):
    pass


class TradePlanRead(TradePlanBase):
    id: str
    status: TradePlanStatus
    actual_entry_time: datetime | None = None
    exit_time: datetime | None = None
    model_config = ConfigDict(from_attributes=True)
