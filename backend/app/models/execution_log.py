import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class ExecutionLog(Base):
    __tablename__ = "execution_logs_codex"

    id = Column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    trade_plan_id = Column(UUID(as_uuid=False), ForeignKey("trade_plans_codex.id", ondelete="CASCADE"), nullable=True)
    manual_plan_id = Column(UUID(as_uuid=False), ForeignKey("manual_plans_codex.id", ondelete="CASCADE"), nullable=True)
    position_id = Column(UUID(as_uuid=False), ForeignKey("positions_codex.id", ondelete="CASCADE"), nullable=True)
    event_type = Column(String(100), nullable=False)  # order_placed, order_filled, position_closed, etc.
    payload = Column(JSON, nullable=True)
    
    # 订单详情
    order_id = Column(String(100), nullable=True)  # 币安订单ID
    symbol = Column(String(50), nullable=True)
    side = Column(String(4), nullable=True)
    price = Column(Numeric(32, 8), nullable=True)
    quantity = Column(Numeric(32, 8), nullable=True)
    status = Column(String(50), nullable=True)  # 订单状态
    
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
