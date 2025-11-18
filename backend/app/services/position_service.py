"""实时持仓监控和退出策略执行服务"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.enums import PositionStatus
from app.models.position import Position
from app.services.binance_service import BinanceFuturesClient
from app.services.execution_service import ExecutionService


class PositionService:
    """实时监控持仓并执行退出策略"""

    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.client = BinanceFuturesClient(self.settings)
        self.executor = ExecutionService(db, settings)

    def monitor_positions(self, sync_from_binance: bool = True) -> None:
        """
        监控所有活跃持仓，检查是否需要退出
        
        Args:
            sync_from_binance: 是否在监控前同步币安持仓（默认True，确保监控所有持仓包括非系统下单的）
        """
        # 定期同步币安持仓（确保监控所有持仓，包括非系统下单的）
        if sync_from_binance:
            try:
                self.sync_positions_from_binance()
            except Exception as exc:
                logger.warning("同步币安持仓时出错（继续监控）: {}", exc)
        
        # 监控所有活跃持仓
        stmt = select(Position).where(Position.status == PositionStatus.ACTIVE)
        positions = list(self.db.scalars(stmt))
        
        if not positions:
            return
        
        logger.debug("监控 %s 个活跃持仓", len(positions))
        
        for position in positions:
            try:
                logger.debug("开始检查持仓 %s (%s %s), 入场价: %s, 止损百分比: %s%%, 滑动退出百分比: %s%%", 
                           position.id, position.symbol, position.side, 
                           position.entry_price, 
                           float(position.stop_loss_pct) * 100,
                           float(position.trailing_exit_pct) * 100)
                self._check_position(position)
            except Exception as exc:
                logger.error("监控持仓 %s 时出错: %s", position.id, exc, exc_info=True)

    def _check_position(self, position: Position) -> None:
        """检查单个持仓，执行退出策略"""
        # 获取当前价格
        current_price = self.client.get_mark_price(position.symbol)
        if not current_price:
            logger.warning("无法获取 %s 的标记价格", position.symbol)
            return
        
        current_price = Decimal(str(current_price))
        now = datetime.now(timezone.utc)
        
        # 重要：在更新最高价/最低价之前，先保存用于滑动退出计算的基准价格
        # 这样可以确保滑动退出检查使用的是更新前的历史最高/最低价，避免逻辑错误
        highest_for_trailing = position.highest_price
        lowest_for_trailing = position.lowest_price
        
        # 更新最高价和最低价（持续追踪，用于滑动退出）
        # 重要：这些值会持续更新，即使trailing_exit_pct被修改，也会继续使用历史最高/最低价
        price_changed = False
        if position.highest_price is None or current_price > position.highest_price:
            old_highest = position.highest_price
            position.highest_price = current_price
            price_changed = True
            if old_highest is not None:
                logger.debug("持仓 %s (%s) 更新历史最高价: %s -> %s (用于滑动退出计算)", 
                           position.id, position.symbol, old_highest, current_price)
        if position.lowest_price is None or current_price < position.lowest_price:
            old_lowest = position.lowest_price
            position.lowest_price = current_price
            price_changed = True
            if old_lowest is not None:
                logger.debug("持仓 %s (%s) 更新历史最低价: %s -> %s (用于滑动退出计算)", 
                           position.id, position.symbol, old_lowest, current_price)
        
        position.last_check_time = now
        
        # 计算当前盈亏（用于日志和监控）
        if position.side == "BUY":
            pnl_pct = float((current_price - position.entry_price) / position.entry_price * 100)
            position_value = float(position.entry_quantity) * float(current_price)
            pnl_value = position_value * pnl_pct / 100
        else:
            pnl_pct = float((position.entry_price - current_price) / position.entry_price * 100)
            position_value = float(position.entry_quantity) * float(current_price)
            pnl_value = position_value * pnl_pct / 100
        
        # 检查止损
        if position.side == "BUY":
            # 做多：价格下跌触发止损
            stop_loss_price = position.entry_price * (Decimal("1") - Decimal(str(position.stop_loss_pct)))
            if current_price <= stop_loss_price:
                logger.info("持仓 %s (%s) 触发止损: 当前价 %s <= 止损价 %s (止损百分比: %s%%, 当前盈亏: %.2f%%, %.2f USDT)", 
                          position.id, position.symbol, current_price, stop_loss_price, 
                          float(position.stop_loss_pct) * 100, pnl_pct, pnl_value)
                self._close_position(position, current_price, "stop_loss")
                return
            else:
                # 增强日志：记录当前状态（每10次检查记录一次，避免日志过多）
                logger.debug("持仓 %s (%s) 监控中: 当前价=%s, 止损价=%s, 当前盈亏=%.2f%% (%.2f USDT), 止损百分比=%s%%", 
                           position.id, position.symbol, current_price, stop_loss_price, 
                           pnl_pct, pnl_value, float(position.stop_loss_pct) * 100)
        else:
            # 做空：价格上涨触发止损
            stop_loss_price = position.entry_price * (Decimal("1") + Decimal(str(position.stop_loss_pct)))
            if current_price >= stop_loss_price:
                logger.info("持仓 %s (%s) 触发止损: 当前价 %s >= 止损价 %s (止损百分比: %s%%, 当前盈亏: %.2f%%, %.2f USDT)", 
                          position.id, position.symbol, current_price, stop_loss_price,
                          float(position.stop_loss_pct) * 100, pnl_pct, pnl_value)
                self._close_position(position, current_price, "stop_loss")
                return
            else:
                # 增强日志：记录当前状态
                logger.debug("持仓 %s (%s) 监控中: 当前价=%s, 止损价=%s, 当前盈亏=%.2f%% (%.2f USDT), 止损百分比=%s%%", 
                           position.id, position.symbol, current_price, stop_loss_price,
                           pnl_pct, pnl_value, float(position.stop_loss_pct) * 100)
        
        # 检查滑动退出（仅对做多有效）
        # 重要：使用更新前的历史最高价计算滑动止损价，避免在本次检查中更新最高价后立即触发
        if position.side == "BUY" and highest_for_trailing:
            # 基于历史最高价和当前滑动退出百分比计算退出价格
            trailing_stop_price = highest_for_trailing * (Decimal("1") - Decimal(str(position.trailing_exit_pct)))
            # 重要：只有当当前价格严格小于等于滑动止损价时才触发（避免浮点数精度问题）
            if current_price <= trailing_stop_price:
                logger.info("持仓 %s (%s) 触发滑动退出: 当前价 %s <= 滑动止损价 %s (历史最高价: %s, 滑动退出百分比: %s%%, 当前盈亏: %.2f%%, %.2f USDT)", 
                          position.id, position.symbol, current_price, trailing_stop_price, highest_for_trailing,
                          float(position.trailing_exit_pct) * 100, pnl_pct, pnl_value)
                try:
                    self._close_position(position, current_price, "trailing_stop")  # 使用当前价格而不是计算出的止损价
                    return
                except Exception as exc:
                    logger.error("滑动退出执行失败: 持仓 %s (%s), 错误: %s", position.id, position.symbol, exc, exc_info=True)
                    # 不返回，继续监控，等待下次检查时重试
                    self.db.rollback()
                    return
            else:
                # 调试日志：显示滑动退出状态（每10次检查记录一次，避免日志过多）
                logger.debug("持仓 %s (%s) 滑动退出监控: 当前价=%s, 历史最高价=%s, 滑动止损价=%s, 滑动退出百分比=%s%%, 当前盈亏=%.2f%% (%.2f USDT)", 
                           position.id, position.symbol, current_price, highest_for_trailing, trailing_stop_price,
                           float(position.trailing_exit_pct) * 100, pnl_pct, pnl_value)
        
        # 对做空的处理（反转逻辑）
        # 重要：使用更新前的历史最低价计算滑动止损价，避免在本次检查中更新最低价后立即触发
        if position.side == "SELL" and lowest_for_trailing:
            # 基于历史最低价和当前滑动退出百分比计算退出价格
            trailing_stop_price = lowest_for_trailing * (Decimal("1") + Decimal(str(position.trailing_exit_pct)))
            # 重要：只有当当前价格严格大于等于滑动止损价时才触发（避免浮点数精度问题）
            if current_price >= trailing_stop_price:
                logger.info("持仓 %s (%s) 触发滑动退出: 当前价 %s >= 滑动止损价 %s (历史最低价: %s, 滑动退出百分比: %s%%, 当前盈亏: %.2f%%, %.2f USDT)", 
                          position.id, position.symbol, current_price, trailing_stop_price, lowest_for_trailing,
                          float(position.trailing_exit_pct) * 100, pnl_pct, pnl_value)
                try:
                    self._close_position(position, current_price, "trailing_stop")  # 使用当前价格而不是计算出的止损价
                    return
                except Exception as exc:
                    logger.error("滑动退出执行失败: 持仓 %s (%s), 错误: %s", position.id, position.symbol, exc, exc_info=True)
                    # 不返回，继续监控，等待下次检查时重试
                    self.db.rollback()
                    return
            else:
                # 调试日志：显示滑动退出状态
                logger.debug("持仓 %s (%s) 滑动退出监控: 当前价=%s, 历史最低价=%s, 滑动止损价=%s, 滑动退出百分比=%s%%, 当前盈亏=%.2f%% (%.2f USDT)", 
                           position.id, position.symbol, current_price, lowest_for_trailing, trailing_stop_price,
                           float(position.trailing_exit_pct) * 100, pnl_pct, pnl_value)
        
        self.db.commit()

    def _close_position(self, position: Position, exit_price: Decimal, reason: str) -> None:
        """关闭持仓"""
        try:
            # 平仓（反向操作）
            close_side = "SELL" if position.side == "BUY" else "BUY"
            
            # 重要：从币安获取实际持仓数量，而不是使用数据库中的entry_quantity
            # 因为实际持仓可能已经变化（部分平仓、加仓等）
            actual_quantity = None
            position_found_on_binance = False
            try:
                binance_positions = self.client.get_positions_from_binance()
                for binance_pos in binance_positions:
                    if (binance_pos["symbol"] == position.symbol and 
                        binance_pos["side"] == position.side):
                        actual_quantity = Decimal(str(binance_pos["position_amt"]))
                        position_found_on_binance = True
                        logger.info("从币安获取实际持仓数量: %s %s = %s (数据库数量: %s)", 
                                   position.symbol, position.side, actual_quantity, position.entry_quantity)
                        break
            except Exception as exc:
                logger.warning("获取币安实际持仓数量失败: %s，使用数据库数量", exc)
            
            # 如果币安上已经没有这个持仓了（可能已被手动平仓），直接标记为已关闭
            if not position_found_on_binance:
                logger.warning("币安上已无持仓 %s %s，可能已被手动平仓，直接标记为已关闭", 
                             position.symbol, position.side)
                position.status = PositionStatus.CLOSED
                position.exit_price = exit_price
                position.exit_quantity = Decimal("0")  # 已无持仓，数量为0
                position.exit_time = datetime.now(timezone.utc)
                position.exit_reason = f"{reason}_already_closed"  # 标记为已关闭
                self.db.commit()
                logger.info("持仓 %s 已标记为已关闭（币安上已无持仓）", position.id)
                return
            
            # 如果无法获取实际数量，使用数据库中的数量
            if actual_quantity is None or actual_quantity <= 0:
                actual_quantity = position.entry_quantity
                logger.warning("使用数据库中的持仓数量: %s (可能不准确)", actual_quantity)
            
            # 验证数量是否有效
            if actual_quantity <= 0:
                error_msg = f"持仓数量无效: {actual_quantity}，无法平仓"
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            # 确保数量精度正确（获取交易对的stepSize并调整）
            try:
                symbol_info = self.client.get_symbol_info(position.symbol)
                step_size = symbol_info.get("stepSize", Decimal("0.1"))
                from decimal import ROUND_DOWN
                # 根据stepSize调整数量精度
                if step_size < 1:
                    actual_quantity = (actual_quantity / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
                else:
                    actual_quantity = (actual_quantity / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
                
                # 再次验证调整后的数量
                if actual_quantity <= 0:
                    error_msg = f"调整精度后持仓数量无效: {actual_quantity}，无法平仓"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                logger.info("数量精度已调整: 原始=%s, 调整后=%s, stepSize=%s", 
                           position.entry_quantity, actual_quantity, step_size)
            except Exception as exc:
                logger.warning("调整数量精度失败: %s，使用原始数量", exc)
            
            logger.info("开始平仓持仓 %s (%s %s): 实际数量=%s, 方向=%s, 原因=%s", 
                       position.id, position.symbol, position.side, 
                       actual_quantity, close_side, reason)
            
            # 使用实际持仓数量平仓，添加 reduceOnly=true 确保这是平仓而不是开新仓
            # 这样可以避免需要额外的保证金（特别是做空持仓平仓时需要买入的情况）
            result = self.client.place_market_order(
                position.symbol,
                close_side,
                actual_quantity,
                reduce_only=True  # 平仓时使用 reduceOnly，避免需要额外保证金
            )
            
            # 记录订单ID
            order_id = result.get("orderId") or result.get("order_id") or str(result.get("clientOrderId", ""))
            order_status = result.get("status", "UNKNOWN")
            
            logger.info("平仓订单已提交: 订单ID=%s, 状态=%s, 结果=%s", order_id, order_status, result)
            
            # 重要：等待订单成交（市价单通常立即成交，但需要确认）
            # 市价单可能初始返回NEW状态，需要等待并查询
            import time
            max_retries = 15  # 增加重试次数（15次 * 0.5秒 = 7.5秒）
            retry_count = 0
            order_filled = False
            
            # 如果初始状态已经是FILLED，直接处理
            if order_status in ["FILLED", "COMPLETED"]:
                order_filled = True
                logger.info("订单立即成交: 订单ID=%s", order_id)
            else:
                # 等待订单成交（市价单通常很快成交）
                # 先等待0.2秒，让订单有时间成交
                time.sleep(0.2)
                
                while retry_count < max_retries:
                    try:
                        order_info = self.client.get_order_status(position.symbol, order_id)
                        order_status = order_info.get("status", order_status)
                        logger.debug("查询订单状态: 订单ID=%s, 状态=%s (重试 %d/%d)", 
                                   order_id, order_status, retry_count + 1, max_retries)
                        
                        if order_status in ["FILLED", "COMPLETED"]:
                            # 订单已成交，更新实际成交价格和数量
                            actual_price = order_info.get("avgPrice") or order_info.get("price") or exit_price
                            actual_quantity = order_info.get("executedQty") or order_info.get("quantity") or position.entry_quantity
                            exit_price = Decimal(str(actual_price))
                            position.exit_quantity = Decimal(str(actual_quantity))
                            logger.info("订单已成交: 订单ID=%s, 成交价=%s, 成交数量=%s", 
                                       order_id, exit_price, position.exit_quantity)
                            order_filled = True
                            break
                        elif order_status in ["CANCELED", "REJECTED", "EXPIRED"]:
                            error_msg = f"订单被取消或拒绝: 状态={order_status}, 订单ID={order_id}"
                            logger.error(error_msg)
                            raise ValueError(error_msg)
                        elif order_status == "NEW":
                            # 订单还是新状态，继续等待
                            logger.debug("订单仍为新状态，继续等待: 订单ID=%s", order_id)
                        else:
                            # 其他状态（如PARTIALLY_FILLED），继续等待
                            logger.debug("订单状态: %s, 继续等待: 订单ID=%s", order_status, order_id)
                            
                    except Exception as exc:
                        logger.warning("查询订单状态失败: %s (重试 %d/%d)", exc, retry_count + 1, max_retries)
                        # 查询失败时，如果是最后一次重试，尝试从原始结果获取信息
                        if retry_count == max_retries - 1:
                            # 最后一次重试失败，检查原始结果中是否有成交信息
                            if result.get("executedQty") and float(result.get("executedQty", 0)) > 0:
                                # 原始结果中有成交数量，说明订单可能已成交
                                logger.warning("订单状态查询失败，但原始结果显示有成交: 订单ID=%s, 成交数量=%s", 
                                             order_id, result.get("executedQty"))
                                # 尝试使用原始结果
                                actual_price = result.get("avgPrice") or result.get("price") or exit_price
                                actual_quantity = result.get("executedQty") or position.entry_quantity
                                if actual_price and actual_quantity:
                                    exit_price = Decimal(str(actual_price))
                                    position.exit_quantity = Decimal(str(actual_quantity))
                                    order_filled = True
                                    logger.info("使用原始结果: 订单ID=%s, 成交价=%s, 成交数量=%s", 
                                             order_id, exit_price, position.exit_quantity)
                                    break
                    
                    retry_count += 1
                    if retry_count < max_retries:
                        time.sleep(0.5)  # 等待0.5秒后重试
            
            # 检查订单是否成交
            if not order_filled:
                # 如果订单未成交，尝试从原始结果获取信息
                if result.get("executedQty") and float(result.get("executedQty", 0)) > 0:
                    # 原始结果中有成交数量，使用原始结果
                    actual_price = result.get("avgPrice") or result.get("price") or exit_price
                    actual_quantity = result.get("executedQty") or position.entry_quantity
                    exit_price = Decimal(str(actual_price))
                    position.exit_quantity = Decimal(str(actual_quantity))
                    logger.info("使用原始结果（订单可能已成交）: 订单ID=%s, 成交价=%s, 成交数量=%s", 
                               order_id, exit_price, position.exit_quantity)
                    order_filled = True
                else:
                    # 订单确实未成交，抛出异常
                    error_msg = f"平仓订单未成交: 订单ID={order_id}, 状态={order_status}, 原始结果={result}"
                    logger.error(error_msg)
                    raise ValueError(error_msg)
            
            # 如果还没有设置exit_quantity，使用entry_quantity
            if not hasattr(position, 'exit_quantity') or not position.exit_quantity:
                position.exit_quantity = position.entry_quantity
            
            # 只有在订单成功成交后才更新持仓状态
            position.status = PositionStatus.CLOSED
            position.exit_price = exit_price
            # exit_quantity 已经在上面设置好了
            position.exit_time = datetime.now(timezone.utc)
            position.exit_reason = reason
            
            # 记录执行日志
            from app.models.execution_log import ExecutionLog
            log = ExecutionLog(
                position_id=position.id,
                trade_plan_id=position.trade_plan_id,
                manual_plan_id=position.manual_plan_id,
                event_type="position_closed",
                order_id=order_id,
                symbol=position.symbol,
                side=close_side,
                price=exit_price,
                quantity=position.entry_quantity,
                status="FILLED",
                payload={
                    "reason": reason,
                    "entry_price": float(position.entry_price),
                    "pnl": float((exit_price - position.entry_price) / position.entry_price * 100) if position.side == "BUY" else float((position.entry_price - exit_price) / position.entry_price * 100),
                }
            )
            self.db.add(log)
            
            # 更新关联的计划状态
            if position.trade_plan_id:
                from app.models.trade_plan import TradePlan
                from app.models.enums import TradePlanStatus
                plan = self.db.get(TradePlan, position.trade_plan_id)
                if plan:
                    plan.status = TradePlanStatus.EXITED
                    plan.exit_time = position.exit_time
            
            self.db.commit()
            logger.info("持仓 %s 已关闭，原因: %s", position.id, reason)
            
            # 持仓关闭后，取消WebSocket订阅（如果该交易对没有其他活跃持仓）
            if self.settings.websocket_price_enabled:
                try:
                    # 检查是否还有其他活跃持仓使用该交易对
                    other_positions = list(self.db.scalars(
                        select(Position)
                        .where(Position.status == PositionStatus.ACTIVE)
                        .where(Position.symbol == position.symbol)
                        .where(Position.id != position.id)
                    ))
                    
                    # 如果没有其他活跃持仓，取消订阅
                    if not other_positions:
                        from app.services.binance_websocket_service import get_websocket_price_service
                        ws_service = get_websocket_price_service()
                        ws_service.unsubscribe_symbol(position.symbol)
                        logger.info("持仓关闭，已取消WebSocket订阅: {}", position.symbol)
                except Exception as exc:
                    logger.debug("取消WebSocket订阅失败 ({}): {}", position.symbol, exc)
            
        except Exception as exc:
            logger.error("关闭持仓 %s 失败: %s", position.id, exc)
            self.db.rollback()
            raise

    def get_active_positions(self) -> list[Position]:
        """获取所有活跃持仓"""
        stmt = select(Position).where(Position.status == PositionStatus.ACTIVE).order_by(Position.entry_time.desc())
        return list(self.db.scalars(stmt))

    def get_all_positions(self, limit: int = 100) -> list[Position]:
        """获取所有持仓（包括已关闭）"""
        stmt = select(Position).order_by(Position.created_at.desc()).limit(limit)
        return list(self.db.scalars(stmt))
    
    def sync_positions_from_binance(self) -> dict[str, int]:
        """
        从币安API同步所有实际持仓到数据库
        返回: {"created": 创建数量, "updated": 更新数量, "closed": 关闭数量}
        """
        try:
            # 从币安获取所有实际持仓
            binance_positions = self.client.get_positions_from_binance()
            
            # 如果币安API返回None，可能是API错误，不要关闭所有持仓
            # 只有在明确知道币安上没有持仓时才关闭
            if binance_positions is None:
                logger.warning("币安API返回None，跳过同步以避免误关闭持仓")
                return {"created": 0, "updated": 0, "closed": 0}
            
            # 获取数据库中所有活跃持仓（包括系统和非系统的）
            all_active_positions = self.get_active_positions()
            
            # 检查是否有重复的 (symbol, side) 持仓
            # 如果有重复，需要合并或关闭多余的持仓
            position_groups = {}
            for pos in all_active_positions:
                key = (pos.symbol, pos.side)
                if key not in position_groups:
                    position_groups[key] = []
                position_groups[key].append(pos)
            
            # 处理重复持仓：保留最新的或用户修改过的，关闭其他的
            for key, positions in position_groups.items():
                if len(positions) > 1:
                    logger.warning("检测到重复持仓: {} {} 有 {} 个活跃持仓，将合并为一个", 
                                 key[0], key[1], len(positions))
                    
                    # 选择要保留的持仓：
                    # 1. 优先保留有用户自定义退出参数的（trailing_exit_pct 或 stop_loss_pct 不等于默认值）
                    # 2. 如果没有，保留最新的（entry_time 最晚的）
                    settings = self.settings
                    default_trailing = Decimal(str(settings.trailing_exit_pct))
                    default_stop_loss = Decimal(str(settings.stop_loss_pct))
                    
                    # 找出有自定义参数的持仓
                    custom_positions = [p for p in positions 
                                      if p.trailing_exit_pct != default_trailing or 
                                         p.stop_loss_pct != default_stop_loss]
                    
                    if custom_positions:
                        # 保留有自定义参数的持仓（如果有多个，保留最新的）
                        keep_position = max(custom_positions, key=lambda p: p.entry_time)
                        logger.info("保留持仓 {} (有自定义退出参数: 滑动退出={}%%, 止损={}%%)", 
                                  keep_position.id,
                                  float(keep_position.trailing_exit_pct) * 100,
                                  float(keep_position.stop_loss_pct) * 100)
                    else:
                        # 保留最新的持仓
                        keep_position = max(positions, key=lambda p: p.entry_time)
                        logger.info("保留持仓 {} (最新创建)", keep_position.id)
                    
                    # 关闭其他重复的持仓
                    for pos in positions:
                        if pos.id != keep_position.id:
                            logger.info("关闭重复持仓 {} (与持仓 {} 重复)", pos.id, keep_position.id)
                            pos.status = PositionStatus.CLOSED
                            pos.exit_time = datetime.now(timezone.utc)
                            pos.exit_reason = "duplicate_merged"  # 标记为重复合并
            
            # 如果有重复持仓被关闭，先提交更改
            if any(len(positions) > 1 for positions in position_groups.values()):
                self.db.commit()
                logger.info("已关闭重复持仓，重新获取活跃持仓列表")
            
            # 重新获取活跃持仓（排除已关闭的重复持仓）
            db_positions = {
                (pos.symbol, pos.side): pos
                for pos in self.get_active_positions()
            }
            
            logger.debug("开始同步币安持仓: 数据库中有 %d 个活跃持仓（已处理重复），币安API返回 %d 个持仓", 
                        len(db_positions), len(binance_positions))
            
            # 币安实际持仓的键集合（用于检测已关闭的持仓）
            binance_keys = set()
            
            created_count = 0
            updated_count = 0
            
            for binance_pos in binance_positions:
                symbol = binance_pos["symbol"]
                side = binance_pos["side"]
                key = (symbol, side)
                binance_keys.add(key)
                
                entry_price = binance_pos["entry_price"]
                entry_quantity = binance_pos["position_amt"]
                leverage = binance_pos["leverage"]
                mark_price = binance_pos.get("mark_price", entry_price)  # 获取标记价格（当前价格）
                update_time = binance_pos.get("update_time", 0)
                
                # 将时间戳转换为datetime
                if update_time > 0:
                    entry_time = datetime.fromtimestamp(update_time / 1000, tz=timezone.utc)
                else:
                    entry_time = datetime.now(timezone.utc)
                
                # 检查数据库中是否已存在
                if key in db_positions:
                    # 更新现有持仓
                    position = db_positions[key]
                    
                    # 重要：保存用户自定义的退出参数，避免被同步覆盖
                    # 这些值可能已经被用户通过API修改过，必须保持不变
                    saved_trailing_exit_pct = position.trailing_exit_pct
                    saved_stop_loss_pct = position.stop_loss_pct
                    
                    # 如果数量或价格发生变化，更新（可能是部分平仓或加仓）
                    if position.entry_quantity != entry_quantity or position.entry_price != entry_price:
                        position.entry_quantity = entry_quantity
                        position.entry_price = entry_price
                        position.leverage = Decimal(str(leverage))
                        updated_count += 1
                        logger.debug("更新持仓: {} {} 数量={} 价格={}", symbol, side, entry_quantity, entry_price)
                    
                    # 重要：同步时使用当前标记价格更新最高价和最低价（如果当前价格更高/更低）
                    # 这确保即使是从外部同步的持仓，也能正确追踪历史最高/最低价
                    # 使用mark_price（标记价格）而不是entry_price，因为mark_price是当前市场价格
                    current_price = Decimal(str(mark_price))
                    if position.highest_price is None or current_price > position.highest_price:
                        old_highest = position.highest_price
                        position.highest_price = current_price
                        if old_highest is not None:
                            logger.debug("同步时更新持仓 %s (%s) 历史最高价: %s -> %s", 
                                       position.id, symbol, old_highest, current_price)
                    if position.lowest_price is None or current_price < position.lowest_price:
                        old_lowest = position.lowest_price
                        position.lowest_price = current_price
                        if old_lowest is not None:
                            logger.debug("同步时更新持仓 %s (%s) 历史最低价: %s -> %s", 
                                       position.id, symbol, old_lowest, current_price)
                    
                    # 重要：强制恢复用户自定义的退出参数（确保不会被覆盖）
                    # 这些值可能已经被用户通过API修改过，必须保持不变
                    # 无论什么原因导致这些值被改变，都要恢复它们
                    
                    # 检查退出参数是否被意外修改（在恢复之前检查）
                    old_trailing = position.trailing_exit_pct
                    old_stop_loss = position.stop_loss_pct
                    was_modified = (old_trailing != saved_trailing_exit_pct or 
                                   old_stop_loss != saved_stop_loss_pct)
                    
                    # 强制恢复用户自定义的值（无论是否被修改，都确保使用保存的值）
                    position.trailing_exit_pct = saved_trailing_exit_pct
                    position.stop_loss_pct = saved_stop_loss_pct
                    
                    # 如果检测到被修改，记录警告
                    if was_modified:
                        logger.warning("同步时检测到持仓 %s (%s) 的退出参数被意外修改，已恢复: 滑动退出 %.2f%% -> %.2f%%, 止损 %.2f%% -> %.2f%%", 
                                     position.id, symbol, 
                                     float(old_trailing) * 100,
                                     float(saved_trailing_exit_pct) * 100,
                                     float(old_stop_loss) * 100,
                                     float(saved_stop_loss_pct) * 100)
                    
                    position.last_check_time = datetime.now(timezone.utc)
                else:
                    # 创建新持仓（非系统下单的持仓）
                    # 使用系统默认的止损和滑动退出参数
                    # 注意：对于外部持仓，我们从当前标记价格开始追踪最高/最低价
                    # 虽然无法获取历史最高/最低价，但系统会从此刻开始正确追踪
                    entry_price_decimal = Decimal(str(entry_price))
                    mark_price_decimal = Decimal(str(mark_price))
                    
                    # 使用标记价格（当前价格）初始化最高/最低价，而不是入场价
                    # 这样可以更准确地反映当前市场状态
                    initial_high_low = mark_price_decimal
                    
                    position = Position(
                        symbol=symbol,
                        side=side,
                        status=PositionStatus.ACTIVE,
                        is_external=True,  # 标记为非系统下单的持仓
                        entry_price=entry_price_decimal,
                        entry_quantity=entry_quantity,
                        entry_time=entry_time,
                        leverage=Decimal(str(leverage)),
                        trailing_exit_pct=Decimal(str(self.settings.trailing_exit_pct)),
                        stop_loss_pct=Decimal(str(self.settings.stop_loss_pct)),
                        highest_price=initial_high_low,  # 初始最高价设为当前标记价格（从此刻开始追踪）
                        lowest_price=initial_high_low,  # 初始最低价设为当前标记价格（从此刻开始追踪）
                        last_check_time=datetime.now(timezone.utc),
                    )
                    self.db.add(position)
                    created_count += 1
                    logger.info("同步新持仓（非系统下单）: {} {} 数量={} 入场价={} 当前价={} 杠杆={} 止损={}% 滑动退出={}% (将从当前价格 %.2f 开始追踪最高/最低价)", 
                              symbol, side, entry_quantity, entry_price, mark_price, leverage,
                              float(self.settings.stop_loss_pct) * 100,
                              float(self.settings.trailing_exit_pct) * 100,
                              float(initial_high_low))
            
            # 检查币安上已关闭的持仓（数据库中有但币安上没有）
            # 需要更谨慎：只有在确认币安API调用成功且返回了完整数据时才关闭
            closed_count = 0
            for key, position in db_positions.items():
                if key not in binance_keys:
                    # 币安上可能已关闭，但需要二次确认以避免误关闭
                    if position.status == PositionStatus.ACTIVE:
                        # 添加日志，记录即将关闭的持仓信息
                        logger.info("检测到持仓可能在币安上已关闭: {} {} (持仓ID: {})，进行二次确认", 
                                  position.symbol, position.side, position.id)
                        
                        # 二次确认：再次查询币安API，确认该持仓确实不存在
                        # 如果确认不存在，才标记为关闭
                        try:
                            # 重新获取该交易对的持仓信息
                            all_positions = self.client.get_positions_from_binance()
                            if all_positions is not None:
                                # 检查该持仓是否真的不存在
                                found = False
                                for bp in all_positions:
                                    if bp["symbol"] == position.symbol and bp["side"] == position.side:
                                        found = True
                                        logger.debug("二次确认：持仓 {} {} 在币安上仍存在，保持ACTIVE状态", 
                                                   position.symbol, position.side)
                                        break
                                
                                if not found:
                                    # 确认不存在，标记为关闭
                                    position.status = PositionStatus.CLOSED
                                    position.exit_time = datetime.now(timezone.utc)
                                    position.exit_reason = "external_closed"  # 外部关闭（在币安手动平仓）
                                    closed_count += 1
                                    logger.info("确认持仓已关闭（币安二次确认）: {} {}", position.symbol, position.side)
                                else:
                                    logger.warning("持仓 {} {} 在二次确认时发现仍存在，保持ACTIVE状态（可能是API延迟）", 
                                                 position.symbol, position.side)
                            else:
                                # 二次确认时API返回None，可能是临时API问题，不关闭持仓
                                logger.warning("二次确认时币安API返回None，不关闭持仓 {} {} 以避免误操作", 
                                             position.symbol, position.side)
                        except Exception as exc:
                            # 如果二次确认失败，不关闭持仓，避免误操作
                            logger.error("二次确认持仓状态失败: {}，保持ACTIVE状态以避免误关闭持仓 {} {}", 
                                       exc, position.symbol, position.side, exc_info=True)
            
            self.db.commit()
            
            result = {
                "created": created_count,
                "updated": updated_count,
                "closed": closed_count,
            }
            
            if created_count > 0 or updated_count > 0 or closed_count > 0:
                logger.info("同步币安持仓完成: 创建={} 更新={} 关闭={}", created_count, updated_count, closed_count)
            
            return result
        except Exception as exc:
            logger.error("同步币安持仓失败: {}", exc, exc_info=True)
            self.db.rollback()
            return {"created": 0, "updated": 0, "closed": 0}

