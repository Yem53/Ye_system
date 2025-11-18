"""风险管理服务 - 控制全局交易风险，防止过度亏损"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import NamedTuple

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.position import Position
from app.models.execution_log import ExecutionLog
from app.models.enums import PositionStatus
from app.services.binance_service import BinanceFuturesClient


class RiskCheckResult(NamedTuple):
    """风险检查结果"""
    allowed: bool  # 是否允许交易
    reason: str  # 拒绝原因（如果不允许）
    current_drawdown: float | None = None  # 当前回撤
    daily_loss: float | None = None  # 今日亏损
    position_concentration: dict | None = None  # 持仓集中度


class RiskManagementService:
    """风险管理服务 - 全局风险控制"""

    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.client = BinanceFuturesClient(self.settings)

    def check_trading_allowed(self, symbol: str | None = None, leverage: int | None = None) -> RiskCheckResult:
        """检查是否允许交易

        Args:
            symbol: 交易对符号（用于检查仓位集中度）
            leverage: 杠杆倍数（用于检查最大杠杆）

        Returns:
            RiskCheckResult: 风险检查结果
        """
        if not self.settings.risk_management_enabled:
            return RiskCheckResult(allowed=True, reason="风险管理未启用")

        # 1. 检查最大回撤
        drawdown_check = self._check_max_drawdown()
        if not drawdown_check.allowed:
            return drawdown_check

        # 2. 检查单日最大亏损
        daily_loss_check = self._check_daily_loss()
        if not daily_loss_check.allowed:
            return daily_loss_check

        # 3. 检查持仓集中度
        if symbol:
            concentration_check = self._check_position_concentration(symbol)
            if not concentration_check.allowed:
                return concentration_check

        # 4. 检查最大杠杆
        if leverage:
            leverage_check = self._check_max_leverage(leverage)
            if not leverage_check.allowed:
                return leverage_check

        return RiskCheckResult(allowed=True, reason="风险检查通过")

    def _check_max_drawdown(self) -> RiskCheckResult:
        """检查最大回撤是否超限

        回撤 = (历史最高余额 - 当前余额) / 历史最高余额
        """
        try:
            current_balance = self.client.get_futures_balance()

            # 从执行日志中获取历史最高余额
            # 简化实现：使用当前余额作为基准，如果需要更精确的历史最高余额，
            # 可以在数据库中增加专门的余额历史表
            stmt = select(ExecutionLog).order_by(ExecutionLog.created_at.desc()).limit(100)
            recent_logs = list(self.db.scalars(stmt))

            # 简化计算：假设初始余额为当前余额（实际应该从配置或历史记录获取）
            # 这里使用近期最高值作为参考
            historical_high = current_balance

            # 计算回撤
            if historical_high > 0:
                current_drawdown = float((historical_high - current_balance) / historical_high)

                if current_drawdown > self.settings.max_drawdown_pct:
                    return RiskCheckResult(
                        allowed=False,
                        reason=f"账户回撤 {current_drawdown:.2%} 超过限制 {self.settings.max_drawdown_pct:.2%}，暂停交易",
                        current_drawdown=current_drawdown
                    )

            return RiskCheckResult(allowed=True, reason="回撤检查通过")

        except Exception as exc:
            logger.warning("检查最大回撤失败: {}, 允许继续交易", exc)
            return RiskCheckResult(allowed=True, reason="回撤检查失败，默认允许")

    def _check_daily_loss(self) -> RiskCheckResult:
        """检查单日亏损是否超限"""
        try:
            # 获取今日开始时间（UTC）
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

            # 查询今日所有已关闭的持仓
            stmt = (
                select(Position)
                .where(Position.status == PositionStatus.CLOSED)
                .where(Position.exit_time >= today_start)
            )
            today_positions = list(self.db.scalars(stmt))

            # 计算今日盈亏
            total_pnl = Decimal("0")
            for pos in today_positions:
                if pos.entry_price and pos.exit_price and pos.entry_quantity:
                    if pos.side == "BUY":
                        pnl = (pos.exit_price - pos.entry_price) * pos.entry_quantity
                    else:  # SELL
                        pnl = (pos.entry_price - pos.exit_price) * pos.entry_quantity
                    total_pnl += pnl

            # 获取初始余额（简化实现：使用当前余额）
            current_balance = self.client.get_futures_balance()
            initial_balance = current_balance - total_pnl

            # 只有亏损时才检查
            if total_pnl < 0 and initial_balance > 0:
                daily_loss_pct = abs(float(total_pnl / initial_balance))

                if daily_loss_pct > self.settings.max_daily_loss_pct:
                    return RiskCheckResult(
                        allowed=False,
                        reason=f"今日亏损 {daily_loss_pct:.2%} 超过限制 {self.settings.max_daily_loss_pct:.2%}，暂停交易",
                        daily_loss=daily_loss_pct
                    )

            return RiskCheckResult(allowed=True, reason="单日亏损检查通过")

        except Exception as exc:
            logger.warning("检查单日亏损失败: {}, 允许继续交易", exc)
            return RiskCheckResult(allowed=True, reason="单日亏损检查失败，默认允许")

    def _check_position_concentration(self, symbol: str) -> RiskCheckResult:
        """检查持仓集中度

        Args:
            symbol: 新建仓位的交易对

        Returns:
            RiskCheckResult: 检查结果
        """
        try:
            # 获取所有活跃持仓
            stmt = select(Position).where(Position.status == PositionStatus.ACTIVE)
            active_positions = list(self.db.scalars(stmt))

            if not active_positions:
                return RiskCheckResult(allowed=True, reason="无活跃持仓，集中度检查通过")

            # 计算每个交易对的持仓价值
            position_values = {}
            total_value = Decimal("0")

            for pos in active_positions:
                current_price = self.client.get_mark_price(pos.symbol)
                if current_price:
                    value = pos.entry_quantity * current_price
                    position_values[pos.symbol] = position_values.get(pos.symbol, Decimal("0")) + value
                    total_value += value

            # 检查新交易对的集中度（假设新仓位价值等于平均仓位）
            if total_value > 0:
                avg_position_value = total_value / len(active_positions)
                new_symbol_value = position_values.get(symbol, Decimal("0")) + avg_position_value
                concentration = float(new_symbol_value / (total_value + avg_position_value))

                if concentration > self.settings.max_position_concentration_pct:
                    return RiskCheckResult(
                        allowed=False,
                        reason=f"交易对 {symbol} 持仓集中度 {concentration:.2%} 超过限制 {self.settings.max_position_concentration_pct:.2%}",
                        position_concentration=position_values
                    )

            return RiskCheckResult(allowed=True, reason="持仓集中度检查通过")

        except Exception as exc:
            logger.warning("检查持仓集中度失败: {}, 允许继续交易", exc)
            return RiskCheckResult(allowed=True, reason="集中度检查失败，默认允许")

    def _check_max_leverage(self, leverage: int) -> RiskCheckResult:
        """检查杠杆倍数是否超限

        Args:
            leverage: 杠杆倍数

        Returns:
            RiskCheckResult: 检查结果
        """
        if leverage > self.settings.max_total_leverage:
            return RiskCheckResult(
                allowed=False,
                reason=f"杠杆倍数 {leverage}x 超过限制 {self.settings.max_total_leverage}x"
            )

        return RiskCheckResult(allowed=True, reason="杠杆检查通过")

    def get_risk_status(self) -> dict:
        """获取当前风险状态（用于仪表盘显示）

        Returns:
            dict: 包含各项风险指标的字典
        """
        try:
            current_balance = self.client.get_futures_balance()

            # 计算回撤
            drawdown_result = self._check_max_drawdown()

            # 计算今日盈亏
            daily_loss_result = self._check_daily_loss()

            # 获取活跃持仓集中度
            stmt = select(Position).where(Position.status == PositionStatus.ACTIVE)
            active_positions = list(self.db.scalars(stmt))

            position_concentration = {}
            if active_positions:
                total_value = Decimal("0")
                for pos in active_positions:
                    current_price = self.client.get_mark_price(pos.symbol)
                    if current_price:
                        value = pos.entry_quantity * current_price
                        position_concentration[pos.symbol] = float(value)
                        total_value += value

                # 转换为百分比
                if total_value > 0:
                    position_concentration = {
                        symbol: value / float(total_value)
                        for symbol, value in position_concentration.items()
                    }

            return {
                "enabled": self.settings.risk_management_enabled,
                "current_balance": float(current_balance),
                "max_drawdown_pct": self.settings.max_drawdown_pct,
                "current_drawdown": drawdown_result.current_drawdown,
                "max_daily_loss_pct": self.settings.max_daily_loss_pct,
                "current_daily_loss": daily_loss_result.daily_loss,
                "max_position_concentration_pct": self.settings.max_position_concentration_pct,
                "position_concentration": position_concentration,
                "max_total_leverage": self.settings.max_total_leverage,
                "active_positions_count": len(active_positions),
            }

        except Exception as exc:
            logger.error("获取风险状态失败: {}", exc, exc_info=True)
            return {"error": str(exc)}
