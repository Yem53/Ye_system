import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base
from app.models.enums import TradePlanStatus


class TradePlan(Base):
    __tablename__ = "trade_plans_codex"

    id = Column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    announcement_id = Column(UUID(as_uuid=False), ForeignKey("announcements_codex.id", ondelete="CASCADE"), nullable=False)
    leverage = Column(Numeric(10, 2), nullable=False, default=5)
    position_pct = Column(Numeric(5, 4), nullable=False, default=0.5)
    min_allocation = Column(Numeric(32, 8), nullable=True)
    max_allocation = Column(Numeric(32, 8), nullable=True)
    trailing_exit_pct = Column(Numeric(5, 4), nullable=False, default=0.15)
    stop_loss_pct = Column(Numeric(5, 4), nullable=False, default=0.05)
    status = Column(Enum(TradePlanStatus), default=TradePlanStatus.DRAFT, nullable=False)
    planned_entry_time = Column(DateTime(timezone=True), nullable=True)
    actual_entry_time = Column(DateTime(timezone=True), nullable=True)
    exit_time = Column(DateTime(timezone=True), nullable=True)
    extra = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    announcement = relationship("Announcement", backref="trade_plans")
