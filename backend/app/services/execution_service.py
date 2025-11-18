from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional
import time

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.enums import PositionStatus, TradePlanStatus
from app.models.execution_log import ExecutionLog
from app.models.manual_plan import ManualPlan
from app.models.position import Position
from app.models.trade_plan import TradePlan
from app.services.binance_service import BinanceFuturesClient


class ExecutionService:
    """封装与币安合约交互的关键步骤，下单逻辑集中在此。"""

    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.client = BinanceFuturesClient(self.settings)

    def calculate_order_size(
        self,
        symbol: str,
        symbol_price: Decimal,
        available_balance: Decimal,
        leverage: int | None = None,
        position_pct: float | None = None
    ) -> Decimal:
        """根据可用保证金 * 配置比例来计算下单张数，并应用最大购买金额限制。

        Args:
            symbol: 交易对符号（用于获取精度信息）
            symbol_price: 交易对价格
            available_balance: 可用保证金
            leverage: 杠杆倍数，如果为None则使用系统默认杠杆
            position_pct: 仓位比例，如果为None则使用系统默认配置
        """
        # 使用传入的仓位比例，如果没有则使用系统默认配置
        pct_to_use = position_pct if position_pct is not None else self.settings.position_pct
        allocation = available_balance * Decimal(str(pct_to_use))

        if allocation <= 0 or symbol_price <= 0:
            raise ValueError("无法计算下单数量")

        # 应用最大购买金额限制
        if self.settings.max_order_amount:
            max_amount = Decimal(str(self.settings.max_order_amount))
            if allocation > max_amount:
                logger.info("购买金额 %s 超过最大限制 %s，已限制为最大金额", allocation, max_amount)
                allocation = max_amount

        # 使用传入的杠杆，如果没有则使用系统默认杠杆
        leverage_to_use = Decimal(str(leverage)) if leverage is not None else Decimal(self.settings.leverage)
        quantity = allocation * leverage_to_use / symbol_price

        # 动态获取交易对精度（stepSize），避免硬编码
        from decimal import ROUND_DOWN
        try:
            symbol_info = self.client.get_symbol_info(symbol)
            step_size = symbol_info.get("stepSize", Decimal("0.001"))
            # 根据stepSize调整数量精度（向下取整，避免超额下单）
            if step_size > 0:
                quantity = (quantity / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
        except Exception as exc:
            logger.warning("获取交易对 {} 精度信息失败，使用默认精度0.001: {}", symbol, exc)
            quantity = quantity.quantize(Decimal("0.001"), rounding=ROUND_DOWN)

        return quantity

    def _check_slippage(self, expected_price: Decimal, actual_price: Decimal, side: str) -> tuple[bool, float]:
        """检查滑点是否在允许范围内
        
        Returns:
            (is_valid, slippage_pct): (是否在允许范围内, 滑点百分比)
        """
        if expected_price <= 0:
            return True, 0.0
        
        if side == "BUY":
            slippage_pct = float((actual_price - expected_price) / expected_price * 100)
        else:  # SELL
            slippage_pct = float((expected_price - actual_price) / expected_price * 100)
        
        max_slippage = self.settings.max_slippage_pct
        is_valid = slippage_pct <= max_slippage
        
        return is_valid, slippage_pct

    def _place_order_with_slippage_check(
        self, 
        symbol: str, 
        side: str, 
        quantity: Decimal, 
        expected_price: Decimal
    ) -> dict:
        """根据配置的订单类型下单，并检查滑点"""
        order_type = self.settings.order_type.upper()
        
        if order_type == "LIMIT":
            # 限价单：使用预期价格下单
            logger.info("使用限价单下单 {} {} @ {}", symbol, side, expected_price)
            order_result = self.client.place_limit_order(symbol, side, quantity, expected_price)
        else:  # MARKET
            # 市价单：直接下单
            logger.info("使用市价单下单 {} {}", symbol, side)
            order_result = self.client.place_market_order(symbol, side, quantity)
            
            # 对于市价单，如果状态是 NEW，等待一下然后查询订单状态
            order_status = order_result.get("status", "").upper()
            if order_status == "NEW":
                order_id = str(order_result.get("orderId", ""))
                if order_id:
                    logger.info("市价单已提交，等待成交，订单ID: {}", order_id)
                    # 等待最多3秒，每0.5秒检查一次
                    import time
                    for _ in range(6):
                        time.sleep(0.5)
                        try:
                            order_result = self.client.get_order_status(symbol, order_id)
                            order_status = order_result.get("status", "").upper()
                            if order_status in ["FILLED", "PARTIALLY_FILLED"]:
                                logger.info("市价单已成交，订单ID: {}", order_id)
                                break
                        except Exception as exc:
                            logger.debug("查询订单状态失败: {}", exc)
            
            # 检查滑点（如果订单已成交）
            if order_result.get("status", "").upper() in ["FILLED", "PARTIALLY_FILLED"]:
                actual_price = Decimal(str(order_result.get("avgPrice", expected_price))) if order_result.get("avgPrice") else expected_price
                is_valid, slippage_pct = self._check_slippage(expected_price, actual_price, side)

                if not is_valid:
                    error_msg = (
                        f"市价单滑点超过限制: 预期价格={expected_price}, 实际价格={actual_price}, "
                        f"滑点={slippage_pct:.2f}%, 最大允许={self.settings.max_slippage_pct:.2f}%"
                    )
                    logger.error(error_msg)
                    # 如果配置为拒绝订单，则抛出异常（默认行为）
                    if self.settings.slippage_reject_order:
                        raise ValueError(error_msg)
                    else:
                        logger.warning("滑点超限但配置为继续执行，请注意风险")
                else:
                    logger.debug("市价单滑点检查通过: 滑点={:.2f}%", slippage_pct)
        
        return order_result

    def _place_order_with_timeout(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        expected_price: Decimal
    ) -> dict:
        """下单，如果是限价单且超时未成交，则取消并转为市价单"""
        order_type = self.settings.order_type.upper()
        
        if order_type == "LIMIT":
            # 下限价单
            order_result = self.client.place_limit_order(symbol, side, quantity, expected_price)
            order_id = str(order_result.get("orderId", ""))
            
            if not order_id:
                logger.warning("限价单下单失败，未返回订单ID，转为市价单")
                return self._place_order_with_slippage_check(symbol, side, quantity, expected_price)
            
            # 等待订单成交
            timeout = self.settings.limit_order_timeout_seconds
            start_time = time.time()
            
            # 先检查初始订单状态
            initial_status = order_result.get("status", "").upper()
            if initial_status == "FILLED":
                logger.info("限价单立即成交: {}", order_id)
                return order_result
            elif initial_status in ["CANCELED", "REJECTED", "EXPIRED"]:
                logger.warning("限价单被拒绝/取消/过期: {}, 状态: {}", order_id, initial_status)
                # 立即转为市价单
                return self._place_order_with_slippage_check(symbol, side, quantity, expected_price)
            
            # 如果订单状态是 NEW 或 PARTIALLY_FILLED，等待成交
            while time.time() - start_time < timeout:
                try:
                    order_status = self.client.get_order_status(symbol, order_id)
                    status = order_status.get("status", "").upper()
                    
                    if status == "FILLED":
                        logger.info("限价单已成交: {}", order_id)
                        return order_status
                    elif status == "PARTIALLY_FILLED":
                        # 部分成交，继续等待
                        logger.debug("限价单部分成交: {}, 已成交: {}/{}", 
                                   order_id, 
                                   order_status.get("executedQty", "0"),
                                   order_status.get("origQty", "0"))
                    elif status in ["CANCELED", "REJECTED", "EXPIRED"]:
                        logger.warning("限价单被取消/拒绝/过期: {}, 状态: {}", order_id, status)
                        break
                except Exception as exc:
                    logger.debug("查询限价单状态失败: {}", exc)
                
                time.sleep(0.5)  # 每0.5秒检查一次
            
            # 超时或取消，根据配置决定是否转为市价单
            try:
                self.client.cancel_order(symbol, order_id)
                logger.info("限价单超时，已取消订单")
            except Exception as exc:
                logger.warning("取消限价单失败: {}", exc)

            # 根据配置决定是否转为市价单
            if self.settings.limit_order_auto_convert_to_market:
                logger.info("限价单未成交，根据配置转为市价单（可能有滑点风险）")
                return self._place_order_with_slippage_check(symbol, side, quantity, expected_price)
            else:
                error_msg = f"限价单超时未成交，且未配置自动转市价单，订单失败: {order_id}"
                logger.error(error_msg)
                raise ValueError(error_msg)
        else:
            return self._place_order_with_slippage_check(symbol, side, quantity, expected_price)

    def execute_plan(self, plan: TradePlan, side: str = "BUY", price_hint: Optional[Decimal] = None) -> None:
        """执行交易计划（市价单建仓），记录订单详情并创建持仓。"""

        if not plan.announcement or not plan.announcement.symbol:
            raise ValueError("交易计划缺少交易对信息")
        symbol = f"{plan.announcement.symbol}USDT"

        # 风险管理检查
        from app.services.risk_management_service import RiskManagementService
        risk_service = RiskManagementService(self.db, self.settings)
        risk_check = risk_service.check_trading_allowed(symbol=symbol, leverage=plan.leverage)
        if not risk_check.allowed:
            logger.error("风险管理拒绝交易: {}", risk_check.reason)
            raise ValueError(f"风险管理拒绝交易: {risk_check.reason}")
        
        # 确保WebSocket已订阅（如果启用）
        if self.settings.websocket_price_enabled:
            try:
                from app.services.binance_websocket_service import get_websocket_price_service
                ws_service = get_websocket_price_service()
                ws_service.subscribe_symbol(symbol)
            except Exception as exc:
                logger.debug("订阅WebSocket失败 ({}): {}", symbol, exc)
        
        self.client.set_leverage(symbol, int(plan.leverage))
        balance = self.client.get_account_balance()
        mark_price = price_hint or self.client.get_mark_price(symbol) or Decimal("1")
        # 使用计划中的杠杆计算订单数量
        quantity = self.calculate_order_size(symbol, mark_price, balance, leverage=plan.leverage)
        
        # 执行订单（根据配置选择市价单或限价单，并检查滑点）
        order_result = self._place_order_with_timeout(symbol, side, quantity, mark_price)
        order_id = str(order_result.get("orderId") or order_result.get("order_id") or order_result.get("clientOrderId", ""))
        
        # 检查订单状态，确保订单已成交
        order_status = order_result.get("status", "").upper()
        if order_status not in ["FILLED", "PARTIALLY_FILLED"]:
            # 如果订单未成交，抛出异常
            raise ValueError(f"订单未成交，状态: {order_status}, 订单ID: {order_id}")
        
        # 获取实际成交价格和数量
        # 对于市价单，使用 avgPrice 或 price；对于限价单，使用 avgPrice（平均成交价）
        actual_price = None
        if order_result.get("avgPrice"):
            actual_price = Decimal(str(order_result.get("avgPrice")))
        elif order_result.get("price"):
            actual_price = Decimal(str(order_result.get("price")))
        else:
            actual_price = mark_price
        
        # 获取实际成交数量
        actual_quantity = Decimal(str(order_result.get("executedQty", "0")))
        if actual_quantity <= 0:
            # 如果成交数量为0，使用原始数量（部分成交的情况）
            actual_quantity = Decimal(str(order_result.get("origQty", quantity)))
        
        if actual_quantity <= 0:
            raise ValueError(f"订单成交数量无效: {actual_quantity}, 订单ID: {order_id}")
        
        # 创建持仓记录
        position = Position(
            trade_plan_id=plan.id,
            symbol=symbol,
            side=side,
            status=PositionStatus.ACTIVE,
            order_id=order_id,
            entry_price=actual_price,
            entry_quantity=actual_quantity,
            entry_time=datetime.now(timezone.utc),
            leverage=plan.leverage,
            trailing_exit_pct=plan.trailing_exit_pct,
            stop_loss_pct=plan.stop_loss_pct,
            highest_price=actual_price,
            lowest_price=actual_price,
            last_check_time=datetime.now(timezone.utc),
        )
        self.db.add(position)
        
        # 记录执行日志
        log = ExecutionLog(
            trade_plan_id=plan.id,
            position_id=position.id,
            event_type="order_filled",
            order_id=order_id,
            symbol=symbol,
            side=side,
            price=actual_price,
            quantity=actual_quantity,
            status=order_result.get("status", "FILLED"),
            payload=order_result,
        )
        self.db.add(log)
        
        plan.status = TradePlanStatus.ACTIVE
        plan.actual_entry_time = datetime.now(timezone.utc)
        self.db.commit()
        logger.info("计划 %s 执行完成，订单ID: %s，持仓ID: %s", plan.id, order_id, position.id)

    def execute_manual_plan(self, plan: ManualPlan) -> None:
        # 确保symbol格式正确，如果没有USDT后缀则自动添加
        symbol = plan.symbol.upper()
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"

        # 风险管理检查
        from app.services.risk_management_service import RiskManagementService
        risk_service = RiskManagementService(self.db, self.settings)
        risk_check = risk_service.check_trading_allowed(symbol=symbol, leverage=plan.leverage)
        if not risk_check.allowed:
            logger.error("风险管理拒绝手动计划交易: {}", risk_check.reason)
            raise ValueError(f"风险管理拒绝交易: {risk_check.reason}")
        
        # 确保WebSocket已订阅（如果启用）
        if self.settings.websocket_price_enabled:
            try:
                from app.services.binance_websocket_service import get_websocket_price_service
                ws_service = get_websocket_price_service()
                ws_service.subscribe_symbol(symbol)
            except Exception as exc:
                logger.debug("订阅WebSocket失败 ({}): {}", symbol, exc)
        
        self.client.set_leverage(symbol, int(plan.leverage))
        # 清除余额缓存，确保获取最新的可用保证金
        # 防止因为缓存导致使用过期的余额信息
        from app.services.binance_service import BinanceFuturesClient
        BinanceFuturesClient.clear_balance_cache("futures")

        # 使用合约账户余额（可用保证金）
        balance = self.client.get_futures_balance()
        mark_price = self.client.get_mark_price(symbol) or Decimal("1")

        # 记录余额和价格信息
        logger.info("执行计划 {}: 可用保证金={} USDT, 标记价格={}, 杠杆={}x, 仓位比例={}",
                   plan.id, balance, mark_price, plan.leverage, plan.position_pct)

        # 直接传递计划中的仓位比例和杠杆，避免修改全局配置（并发安全）
        quantity = self.calculate_order_size(
            symbol,
            mark_price,
            balance,
            leverage=plan.leverage,
            position_pct=plan.position_pct
        )

        # 计算实际需要的保证金
        # 订单价值 = quantity * mark_price
        order_value = quantity * mark_price
        # 需要的保证金 = 订单价值 / 杠杆 = allocation（应该等于 balance * position_pct）
        required_margin = order_value / Decimal(str(plan.leverage))

        logger.info("计划 {}: 计算数量={}, 订单价值={} USDT, 需要保证金={} USDT, 可用保证金={} USDT",
                   plan.id, quantity, order_value, required_margin, balance)

        # 检查保证金是否足够（留一点余量，避免精度问题）
        if required_margin > balance * Decimal("0.99"):  # 留1%的余量
            error_msg = f"保证金不足: 需要 {required_margin} USDT, 可用 {balance} USDT"
            logger.error("计划 {}: {}", plan.id, error_msg)
            raise ValueError(error_msg)
        
        # 执行订单（根据配置选择市价单或限价单，并检查滑点）
        order_result = self._place_order_with_timeout(symbol, plan.side.upper(), quantity, mark_price)
        order_id = str(order_result.get("orderId") or order_result.get("order_id") or order_result.get("clientOrderId", ""))
        
        # 检查订单状态，确保订单已成交
        order_status = order_result.get("status", "").upper()
        if order_status not in ["FILLED", "PARTIALLY_FILLED"]:
            # 如果订单未成交，抛出异常
            raise ValueError(f"订单未成交，状态: {order_status}, 订单ID: {order_id}")
        
        # 获取实际成交价格和数量
        # 对于市价单，使用 avgPrice 或 price；对于限价单，使用 avgPrice（平均成交价）
        actual_price = None
        if order_result.get("avgPrice"):
            actual_price = Decimal(str(order_result.get("avgPrice")))
        elif order_result.get("price"):
            actual_price = Decimal(str(order_result.get("price")))
        else:
            actual_price = mark_price
        
        # 获取实际成交数量
        actual_quantity = Decimal(str(order_result.get("executedQty", "0")))
        if actual_quantity <= 0:
            # 如果成交数量为0，使用原始数量（部分成交的情况）
            actual_quantity = Decimal(str(order_result.get("origQty", quantity)))
        
        if actual_quantity <= 0:
            raise ValueError(f"订单成交数量无效: {actual_quantity}, 订单ID: {order_id}")
        
        # 创建持仓记录
        position = Position(
            manual_plan_id=plan.id,
            symbol=symbol,
            side=plan.side.upper(),
            status=PositionStatus.ACTIVE,
            order_id=order_id,
            entry_price=actual_price,
            entry_quantity=actual_quantity,
            entry_time=datetime.now(timezone.utc),
            leverage=plan.leverage,
            trailing_exit_pct=plan.trailing_exit_pct,
            stop_loss_pct=plan.stop_loss_pct,
            highest_price=actual_price,
            lowest_price=actual_price,
            last_check_time=datetime.now(timezone.utc),
        )
        self.db.add(position)
        
        # 记录执行日志
        log = ExecutionLog(
            manual_plan_id=plan.id,
            position_id=position.id,
            event_type="order_filled",
            order_id=order_id,
            symbol=symbol,
            side=plan.side.upper(),
            price=actual_price,
            quantity=actual_quantity,
            status=order_result.get("status", "FILLED"),
            payload=order_result,
        )
        self.db.add(log)
        
        self.db.commit()
        logger.info("手动计划 %s 执行完成，订单ID: %s，持仓ID: %s", plan.id, order_id, position.id)
