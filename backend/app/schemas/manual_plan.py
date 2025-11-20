from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ManualPlanStatus


class ManualPlanBase(BaseModel):
    symbol: str = Field(..., description="合约符号，例如 FOLKSUSDT / Contract symbol, e.g. FOLKSUSDT")
    side: str = Field("BUY", description="交易方向：BUY 或 SELL / Trading direction: BUY or SELL")
    listing_time: datetime = Field(..., description="上线时间（UTC） / Listing time (UTC)")
    leverage: float = Field(5, description="杠杆倍数（默认5） / Leverage multiplier (default: 5)")
    position_pct: float = Field(0.5, description="仓位比例，使用可用保证金的百分比（默认0.5即50%） / Position percentage, % of available margin (default: 0.5 = 50%)")
    trailing_exit_pct: float = Field(0.15, description="滑动退出百分比，从最高价回撤的百分比（默认0.15即15%） / Trailing exit %, pullback from high (default: 0.15 = 15%)")
    stop_loss_pct: float = Field(0.05, description="止损百分比，从入场价下跌的百分比（默认0.05即5%） / Stop loss %, drop from entry (default: 0.05 = 5%)")
    max_slippage_pct: float = Field(0.5, description="最大允许滑点百分比（默认0.5%%） / Max allowed slippage percentage (default 0.5%)")
    notes: str | None = Field(None, description="备注，可选 / Notes, optional")


class ManualPlanCreate(ManualPlanBase):
    pass


class ManualPlanRead(ManualPlanBase):
    id: str
    status: ManualPlanStatus
    created_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)
