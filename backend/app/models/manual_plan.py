import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.models.enums import ManualPlanStatus


class ManualPlan(Base):
    __tablename__ = "manual_plans_codex"

    id = Column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    symbol = Column(String(50), nullable=False)
    side = Column(String(4), default="BUY", nullable=False)
    listing_time = Column(DateTime(timezone=True), nullable=False)
    leverage = Column(Numeric(10, 2), nullable=False, default=5)
    position_pct = Column(Numeric(5, 4), nullable=False, default=0.5)
    trailing_exit_pct = Column(Numeric(5, 4), nullable=False, default=0.15)
    stop_loss_pct = Column(Numeric(5, 4), nullable=False, default=0.05)
    notes = Column(Text, nullable=True)
    status = Column(Enum(ManualPlanStatus), default=ManualPlanStatus.PENDING, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
