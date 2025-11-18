import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class TradeAnalysis(Base):
    __tablename__ = "trade_analysis_codex"

    id = Column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    trade_plan_id = Column(UUID(as_uuid=False), ForeignKey("trade_plans_codex.id", ondelete="CASCADE"), nullable=False)
    entry_price = Column(Numeric(32, 12), nullable=True)
    exit_price = Column(Numeric(32, 12), nullable=True)
    highest_price = Column(Numeric(32, 12), nullable=True)
    lowest_price = Column(Numeric(32, 12), nullable=True)
    pnl_percent = Column(Numeric(18, 8), nullable=True)
    data_points = Column(String(32), nullable=True)
    window_seconds = Column(Numeric(18, 2), nullable=False, default=900)
    computed_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    trade_plan = relationship("TradePlan", backref="analysis")
