import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base
from app.models.enums import PositionStatus


class Position(Base):
    """持仓记录，跟踪实际在币安的持仓"""

    __tablename__ = "positions_codex"

    id = Column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    trade_plan_id = Column(UUID(as_uuid=False), ForeignKey("trade_plans_codex.id", ondelete="CASCADE"), nullable=True)
    manual_plan_id = Column(UUID(as_uuid=False), ForeignKey("manual_plans_codex.id", ondelete="CASCADE"), nullable=True)
    symbol = Column(String(50), nullable=False)  # 如 BTCUSDT
    side = Column(String(4), nullable=False)  # BUY or SELL
    status = Column(Enum(PositionStatus), default=PositionStatus.ACTIVE, nullable=False)
    is_external = Column(Boolean, default=False, nullable=False)  # 是否为非系统下单的持仓（手动在币安下单的）
    
    # 订单信息
    order_id = Column(String(100), nullable=True)  # 币安订单ID
    entry_price = Column(Numeric(32, 8), nullable=False)  # 入场价格
    entry_quantity = Column(Numeric(32, 8), nullable=False)  # 入场数量
    entry_time = Column(DateTime(timezone=True), nullable=False)  # 入场时间
    
    # 退出信息
    exit_price = Column(Numeric(32, 8), nullable=True)  # 退出价格
    exit_quantity = Column(Numeric(32, 8), nullable=True)  # 退出数量
    exit_time = Column(DateTime(timezone=True), nullable=True)  # 退出时间
    exit_reason = Column(String(100), nullable=True)  # 退出原因：trailing_stop, stop_loss, manual
    
    # 策略参数
    leverage = Column(Numeric(10, 2), nullable=False)
    trailing_exit_pct = Column(Numeric(5, 4), nullable=False)
    stop_loss_pct = Column(Numeric(5, 4), nullable=False)
    
    # 跟踪信息
    highest_price = Column(Numeric(32, 8), nullable=True)  # 持仓期间最高价
    lowest_price = Column(Numeric(32, 8), nullable=True)  # 持仓期间最低价
    last_check_time = Column(DateTime(timezone=True), nullable=True)  # 最后检查时间
    
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    trade_plan = relationship("TradePlan", backref="positions")
    manual_plan = relationship("ManualPlan", backref="positions")

