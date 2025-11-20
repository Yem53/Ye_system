"""实时持仓监控和退出策略执行服务"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
import time
from threading import Lock

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging_config import log_key_event
from app.models.enums import PositionStatus, ManualPlanStatus
from app.models.manual_plan import ManualPlan
from app.models.position import Position
from app.models.execution_log import ExecutionLog
from app.services.binance_service import BinanceFuturesClient
from app.services.execution_service import ExecutionService

_closing_positions: set[str] = set()
_closing_lock = Lock()


class PositionService:
    """实时监控持仓并执行退出策略"""

    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.client = BinanceFuturesClient(self.settings)
        self.executor = ExecutionService(db, settings)

    def _has_system_execution_record(self, position: Position) -> bool:
        """判断该持仓是否有系统成交记录（order_filled）或系统关闭记录（position_closed）"""
        # 检查是否有 order_filled 记录（系统下单成交）
        stmt = (
            select(ExecutionLog.id)
            .where(ExecutionLog.position_id == position.id)
            .where(ExecutionLog.event_type == "order_filled")
            .limit(1)
        )
        if self.db.scalar(stmt) is not None:
            return True
        
        # 检查是否有 position_closed 记录（系统关闭订单）
        stmt = (
            select(ExecutionLog.id)
            .where(ExecutionLog.position_id == position.id)
            .where(ExecutionLog.event_type == "position_closed")
            .limit(1)
        )
        return self.db.scalar(stmt) is not None

    def _finalize_missing_position(self, position: Position, exit_price: Decimal | None, default_reason: str = "external_closed") -> str:
        """当币安上找不到持仓时，更新本地持仓的退出信息"""
        reason = default_reason
        if default_reason == "external_closed" and not self._has_system_execution_record(position):
            reason = "not_executed"
        position.status = PositionStatus.CLOSED
        position.exit_price = exit_price or position.exit_price or position.entry_price
        position.exit_quantity = position.exit_quantity or Decimal("0")
        position.exit_time = datetime.now(timezone.utc)
        position.exit_reason = reason
        return reason

    def _confirm_position_absent_on_binance(self, symbol: str, side: str, attempts: int = 2, delay: float = 0.2) -> bool:
        """通过多次查询币安持仓确认该交易对确实不存在"""
        for attempt in range(attempts):
            binance_positions = self.client.get_positions_from_binance()
            if binance_positions is None:
                logger.warning("第%d次检查币安持仓失败，无法确认 %s %s 是否存在", attempt + 1, symbol, side)
                return False
            for bp in binance_positions:
                if bp["symbol"] == symbol and bp["side"] == side:
                    logger.debug("二次确认：持仓 %s %s 在币安仍存在", symbol, side)
                    return False
            if attempt < attempts - 1:
                time.sleep(delay)
        return True

    def _finalize_manual_plan_if_needed(self, manual_plan_id: str | None, closed_position_id: str | None = None) -> None:
        if not manual_plan_id:
            return
        plan = self.db.get(ManualPlan, manual_plan_id)
        if not plan:
            return
        if plan.status in {
            ManualPlanStatus.CANCELLED,
            ManualPlanStatus.FAILED,
            ManualPlanStatus.EXECUTED,
        }:
            return
        stmt = (
            select(Position.id)
            .where(Position.status == PositionStatus.ACTIVE)
            .where(Position.manual_plan_id == manual_plan_id)
            .limit(1)
        )
        if closed_position_id:
            stmt = stmt.where(Position.id != closed_position_id)
        has_other_active = self.db.scalar(stmt)
        if has_other_active:
            return
        plan.status = ManualPlanStatus.EXECUTED
        plan.updated_at = datetime.now(timezone.utc)
        logger.info("手动计划 %s 已全部执行完成，状态更新为 EXECUTED", plan.id)

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
        
        # 快速查询活跃持仓（只查询必要字段，提高性能）
        stmt = select(Position).where(Position.status == PositionStatus.ACTIVE)
        positions = list(self.db.scalars(stmt))
        
        if not positions:
            return
        
        # 批量获取所有持仓的价格（一次API调用，大幅提升性能）
        symbols = list(set(pos.symbol for pos in positions))
        prices = {}
        try:
            # 优先使用批量获取价格（如果支持）
            if hasattr(self.client, 'get_mark_prices_batch'):
                prices = self.client.get_mark_prices_batch(symbols)
            else:
                # 降级：逐个获取（但尽量减少调用）
                for symbol in symbols:
                    try:
                        price = self.client.get_mark_price(symbol)
                        if price:
                            prices[symbol] = price
                    except Exception:
                        pass  # 单个价格获取失败不影响其他
        except Exception as exc:
            logger.debug("批量获取价格失败: {}", exc)
            prices = {}
        
        # 并行处理持仓（充分利用多核CPU）
        CPU_COUNT = os.cpu_count() or 4
        PARALLEL_THRESHOLD = 2  # 持仓数>=2时使用并行处理
        
        positions_to_update = []  # 需要更新最高/最低价的持仓
        positions_to_close = []  # 需要关闭的持仓
        
        fallback_symbols: set[str] = set()
        
        def _resolve_price(position: Position) -> Decimal | None:
            current_price = prices.get(position.symbol)
            if current_price:
                return Decimal(str(current_price))
            cached_price = self.client.get_cached_price(position.symbol)
            if cached_price:
                fallback_symbols.add(position.symbol)
                return Decimal(str(cached_price))
            # 缺少实时价格时，使用入场价作为保守值
            fallback_symbols.add(position.symbol)
            return position.entry_price
        
        if len(positions) >= PARALLEL_THRESHOLD:
            # 多个持仓时并行处理（充分利用CPU资源）
            max_workers = min(len(positions), CPU_COUNT, 8)  # 最多8个并发，避免过多线程
            
            def _check_single_position(pos_data: tuple) -> tuple:
                """检查单个持仓（用于并行处理）"""
                position, current_price = pos_data
                try:
                    current_price_decimal = Decimal(str(current_price))
                    should_exit, exit_reason = self._should_exit_position(position, current_price_decimal)
                    
                    if should_exit:
                        return ("close", position, current_price_decimal, exit_reason, None)
                    elif self._should_update_high_low(position, current_price_decimal):
                        return ("update", position, current_price_decimal, None, None)
                    else:
                        return ("none", position, None, None, None)
                except Exception as exc:
                    logger.error("并行检查持仓 %s 时出错: %s", position.id, exc, exc_info=True)
                    return ("error", position, None, None, exc)
            
            # 准备数据
            position_data = []
            for position in positions:
                current_price = _resolve_price(position)
                if current_price:
                    position_data.append((position, current_price))
            
            # 并行处理
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_check_single_position, data): data[0] 
                          for data in position_data}
                
                for future in as_completed(futures):
                    position = futures[future]
                    try:
                        action, pos, price, reason, error = future.result()
                        if action == "close":
                            positions_to_close.append((pos, price, reason))
                        elif action == "update":
                            positions_to_update.append((pos, price))
                        elif action == "error":
                            logger.error("持仓 %s 检查失败", position.id)
                    except Exception as exc:
                        logger.error("获取持仓 %s 检查结果失败: %s", position.id, exc)
        else:
            # 单个持仓时串行处理（避免线程开销）
            for position in positions:
                try:
                    current_price_decimal = _resolve_price(position)
                    if current_price_decimal is None:
                        logger.debug("无法获取 %s 的标记价格，跳过本次检查", position.symbol)
                        continue
                    should_exit, exit_reason = self._should_exit_position(position, current_price_decimal)
                    
                    if should_exit:
                        positions_to_close.append((position, current_price_decimal, exit_reason))
                    elif self._should_update_high_low(position, current_price_decimal):
                        positions_to_update.append((position, current_price_decimal))
                except Exception as exc:
                    logger.error("监控持仓 %s 时出错: %s", position.id, exc, exc_info=True)
        
        if fallback_symbols:
            symbols_preview = ", ".join(sorted(fallback_symbols)[:5])
            if len(fallback_symbols) > 5:
                symbols_preview += ", ..."
            logger.warning(
                "暂时无法获取 %d 个交易对的实时价格，已使用缓存/入场价：%s",
                len(fallback_symbols),
                symbols_preview,
            )
        
        # 执行关闭操作（串行执行，避免并发问题）
        for position, current_price, exit_reason in positions_to_close:
            try:
                self._close_position(position, current_price, exit_reason)
            except Exception as exc:
                logger.error("关闭持仓 %s 失败: %s", position.id, exc, exc_info=True)
        
        # 批量更新最高/最低价（优化：使用SQL批量更新减少数据库往返）
        if positions_to_update:
            try:
                now = datetime.now(timezone.utc)
                INTERRUPT_THRESHOLD = 300  # 5分钟
                
                # 分离需要中断恢复的持仓和正常更新的持仓
                normal_updates = {}  # {position_id: (position, current_price, new_high, new_low)}
                interrupt_recovery_needed = []  # [(position, current_price)]
                
                for position, current_price in positions_to_update:
                    # 检测系统中断
                    last_check = position.last_check_time or position.entry_time or now
                    time_since_last_check = (now - last_check).total_seconds()
                    is_likely_interrupted = time_since_last_check > INTERRUPT_THRESHOLD
                    
                    # 需要中断恢复的持仓单独处理（需要查询K线数据）
                    if is_likely_interrupted and (position.highest_price is None or position.lowest_price is None):
                        interrupt_recovery_needed.append((position, current_price))
                    else:
                        # 正常更新：计算新的最高/最低价
                        new_high = max(position.highest_price or position.entry_price, current_price)
                        new_low = min(position.lowest_price or position.entry_price, current_price)
                        normal_updates[position.id] = (position, current_price, new_high, new_low)
                
                # 处理中断恢复（需要逐个处理，因为需要查询K线）
                for position, current_price in interrupt_recovery_needed:
                    try:
                        # 计算需要查询的时间范围
                        start_time = position.entry_time or position.last_check_time or (now - timedelta(hours=8))
                        start_time_ms = int(start_time.timestamp() * 1000)
                        end_time_ms = int(now.timestamp() * 1000)
                        
                        # 根据中断时间动态选择K线精度（精度很重要，滑动退出需要精确的最高/最低价）
                        time_range_hours = (end_time_ms - start_time_ms) / (1000 * 3600)
                        if time_range_hours <= 1:
                            # 1小时内：使用1分钟K线，最多1000条（约16.7小时）
                            interval = "1m"
                            limit = 1000
                        elif time_range_hours <= 8:
                            # 1-8小时：使用1分钟K线，最多500条（约8.3小时）
                            interval = "1m"
                            limit = 500
                        elif time_range_hours <= 24:
                            # 8-24小时：使用5分钟K线，最多500条（约41.7小时）
                            interval = "5m"
                            limit = 500
                        else:
                            # 超过24小时：使用15分钟K线，最多500条（约125小时）
                            interval = "15m"
                            limit = 500
                        
                        logger.info("从K线数据恢复历史价格: %s %s, 中断时间=%.1f小时, 使用K线间隔=%s, limit=%d", 
                                  position.id, position.symbol, time_range_hours, interval, limit)
                        
                        # 获取K线数据（使用动态选择的精度）
                        klines = self.client.get_klines(
                            symbol=position.symbol,
                            interval=interval,
                            limit=limit,
                            start_time=start_time_ms,
                            end_time=end_time_ms
                        )
                        
                        if klines:
                            kline_highs = [Decimal(str(k[2])) for k in klines]
                            kline_lows = [Decimal(str(k[3])) for k in klines]
                            
                            recovered_high = max(kline_highs) if kline_highs else None
                            recovered_low = min(kline_lows) if kline_lows else None
                            
                            # 使用恢复的数据更新最高/最低价
                            if recovered_high and (position.highest_price is None or recovered_high > position.highest_price):
                                position.highest_price = max(recovered_high, current_price)
                                logger.info("监控时恢复持仓 %s (%s) 历史最高价: %s (从K线数据)", 
                                          position.id, position.symbol, position.highest_price)
                            if recovered_low and (position.lowest_price is None or recovered_low < position.lowest_price):
                                position.lowest_price = min(recovered_low, current_price)
                                logger.info("监控时恢复持仓 %s (%s) 历史最低价: %s (从K线数据)", 
                                          position.id, position.symbol, position.lowest_price)
                    except Exception as exc:
                        logger.debug("监控时从K线数据恢复历史价格失败: {}", exc)
                        # 如果恢复失败，采用保守策略：使用入场价初始化
                        if position.highest_price is None:
                            position.highest_price = position.entry_price
                        if position.lowest_price is None:
                            position.lowest_price = position.entry_price
                    
                    # 中断恢复后，也需要正常更新
                    new_high = max(position.highest_price or position.entry_price, current_price)
                    new_low = min(position.lowest_price or position.entry_price, current_price)
                    normal_updates[position.id] = (position, current_price, new_high, new_low)
                
                # SQL批量更新（大幅减少数据库往返）
                if normal_updates:
                    # 使用SQLAlchemy的bulk_update_mappings进行批量更新
                    update_mappings = []
                    for pos_id, (position, current_price, new_high, new_low) in normal_updates.items():
                        update_mappings.append({
                            'id': pos_id,
                            'highest_price': new_high,
                            'lowest_price': new_low,
                            'last_check_time': now
                        })
                    
                    # 批量更新
                    self.db.bulk_update_mappings(Position, update_mappings)
                    self.db.commit()
                    
                    logger.debug("批量更新了 {} 个持仓的最高/最低价", len(update_mappings))
            except Exception as exc:
                logger.error("批量更新持仓最高/最低价失败: {}", exc, exc_info=True)
                self.db.rollback()
    
    def _should_exit_position(self, position: Position, current_price: Decimal) -> tuple[bool, str]:
        """快速检查持仓是否需要退出（不执行退出，只返回结果）
        
        Args:
            position: 持仓对象
            current_price: 当前价格
        
        Returns:
            (should_exit, exit_reason): (是否需要退出, 退出原因)
        """
        # 检查止损
        if position.side == "BUY":
            stop_loss_price = position.entry_price * (Decimal("1") - Decimal(str(position.stop_loss_pct)))
            if current_price <= stop_loss_price:
                return True, "stop_loss"
        else:
            stop_loss_price = position.entry_price * (Decimal("1") + Decimal(str(position.stop_loss_pct)))
            if current_price >= stop_loss_price:
                return True, "stop_loss"
        
        # 检查滑动退出（使用保守的默认值策略）
        # 改进1：如果 highest_price 为 None，使用 entry_price 而不是 current_price（更保守）
        # 这样可以避免从当前价格开始计算，保持保守策略
        if position.side == "BUY":
            # 做多：使用历史最高价，如果没有则使用入场价（保守策略）
            highest = position.highest_price if position.highest_price is not None else position.entry_price
            if highest:
                trailing_stop_price = highest * (Decimal("1") - Decimal(str(position.trailing_exit_pct)))
                if current_price <= trailing_stop_price:
                    return True, "trailing_stop"
        else:
            # 做空：使用历史最低价，如果没有则使用入场价（保守策略）
            lowest = position.lowest_price if position.lowest_price is not None else position.entry_price
            if lowest:
                trailing_stop_price = lowest * (Decimal("1") + Decimal(str(position.trailing_exit_pct)))
                if current_price >= trailing_stop_price:
                    return True, "trailing_stop"
        
        return False, ""
    
    def _should_update_high_low(self, position: Position, current_price: Decimal) -> bool:
        """检查是否需要更新最高/最低价"""
        if position.highest_price is None or current_price > position.highest_price:
            return True
        if position.lowest_price is None or current_price < position.lowest_price:
            return True
        return False

    def _check_position(self, position: Position, current_price: Decimal | None = None) -> None:
        """检查单个持仓，执行退出策略
        
        Args:
            position: 持仓对象
            current_price: 当前价格（可选，如果提供则跳过API调用，提高性能）
        """
        # 获取当前价格（如果未提供）
        if current_price is None:
            price_result = self.client.get_mark_price(position.symbol)
            if not price_result:
                logger.warning("无法获取 %s 的标记价格", position.symbol)
                return
            current_price = Decimal(str(price_result))
        else:
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
                log_key_event("INFO", "持仓 %s (%s) 触发止损: 当前价 %s <= 止损价 %s (止损百分比: %s%%, 当前盈亏: %.2f%%, %.2f USDT)", 
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
                log_key_event("INFO", "持仓 %s (%s) 触发止损: 当前价 %s >= 止损价 %s (止损百分比: %s%%, 当前盈亏: %.2f%%, %.2f USDT)", 
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
                log_key_event("INFO", "持仓 %s (%s) 触发滑动退出: 当前价 %s <= 滑动止损价 %s (历史最高价: %s, 滑动退出百分比: %s%%, 当前盈亏: %.2f%%, %.2f USDT)", 
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
                log_key_event("INFO", "持仓 %s (%s) 触发滑动退出: 当前价 %s >= 滑动止损价 %s (历史最低价: %s, 滑动退出百分比: %s%%, 当前盈亏: %.2f%%, %.2f USDT)", 
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
            # 重要：检查持仓状态，避免重复关闭（并行处理可能导致多个线程同时尝试关闭）
            if position.status == PositionStatus.CLOSED:
                logger.debug("持仓 %s 已经关闭，跳过重复关闭操作", position.id)
                return
            
            position_id = str(position.id)
            with _closing_lock:
                if position_id in _closing_positions:
                    logger.debug("持仓 %s 正在平仓，跳过重复请求", position_id)
                    return
                _closing_positions.add(position_id)
            try:
                # 平仓（反向操作）
                close_side = "SELL" if position.side == "BUY" else "BUY"
                
                # 重要：从币安获取实际持仓数量，而不是使用数据库中的entry_quantity
                # 因为实际持仓可能已经变化（部分平仓、加仓等）
                actual_quantity = None
                position_found_on_binance = False
                positions_fetch_failed = False
                binance_positions = self.client.get_positions_from_binance()
                if binance_positions is None:
                    positions_fetch_failed = True
                    binance_positions = []
                for binance_pos in binance_positions:
                    if (binance_pos["symbol"] == position.symbol and 
                        binance_pos["side"] == position.side):
                        actual_quantity = Decimal(str(binance_pos["position_amt"]))
                        position_found_on_binance = True
                        logger.info("从币安获取实际持仓数量: %s %s = %s (数据库数量: %s)", 
                                   position.symbol, position.side, actual_quantity, position.entry_quantity)
                        break
                
                # 如果币安上已经没有这个持仓了，需要判断是系统刚关闭还是外部关闭
                if not position_found_on_binance:
                    if positions_fetch_failed:
                        logger.warning("无法获取币安持仓状态，暂不标记 %s %s 为外部关闭，等待下次检查", 
                                     position.symbol, position.side)
                        return
                    # 再次检查持仓状态（可能在检查币安持仓时，其他线程已经关闭了）
                    self.db.refresh(position)
                    if position.status == PositionStatus.CLOSED:
                        logger.debug("持仓 %s 在检查币安持仓期间已被关闭，跳过重复关闭操作", position.id)
                        return
                    
                    # 检查是否有最近的系统关闭记录（5分钟内）
                    try:
                        from app.models.execution_log import ExecutionLog
                        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)
                        recent_close_log = self.db.scalar(
                            select(ExecutionLog)
                            .where(ExecutionLog.position_id == position.id)
                            .where(ExecutionLog.event_type == "position_closed")
                            .where(ExecutionLog.created_at >= recent_time)
                            .order_by(ExecutionLog.created_at.desc())
                            .limit(1)
                        )
                        if recent_close_log:
                            # 有系统关闭记录，说明是系统刚关闭的，使用系统设置的退出原因
                            payload = recent_close_log.payload or {}
                            system_reason = payload.get("reason", reason)
                            logger.info("币安上已无持仓 %s %s，但检测到系统关闭记录（原因: %s），使用系统退出原因", 
                                      position.symbol, position.side, system_reason)
                            # 再次检查状态（避免并发问题）
                            self.db.refresh(position)
                            if position.status == PositionStatus.CLOSED:
                                logger.debug("持仓 %s 在检查关闭记录期间已被关闭，跳过重复关闭操作", position.id)
                                return
                            reason_used = self._finalize_missing_position(
                                position,
                                exit_price if exit_price else (Decimal(str(recent_close_log.price)) if recent_close_log.price else position.entry_price),
                                default_reason=system_reason,
                            )
                            self.db.commit()
                            log_key_event("INFO", f"持仓 {position.id} 已标记为已关闭（系统关闭，原因: {reason_used}）")
                            return
                    except Exception as exc:
                        logger.debug("检查系统关闭记录失败: %s，继续处理", exc)
                    
                    # 没有系统关闭记录，可能是外部手动平仓
                    # 再次检查状态（避免并发问题）
                    self.db.refresh(position)
                    if position.status == PositionStatus.CLOSED:
                        logger.debug("持仓 %s 在检查期间已被关闭，跳过重复关闭操作", position.id)
                        return
                    if not self._confirm_position_absent_on_binance(position.symbol, position.side):
                        logger.info("再次检查后发现持仓 %s %s 仍存在或无法确认，保持 ACTIVE 状态", position.symbol, position.side)
                        return
                    reason_used = self._finalize_missing_position(position, exit_price or position.entry_price, default_reason="external_closed")
                    self.db.commit()
                    if reason_used == "external_closed":
                        log_key_event("INFO", f"持仓 {position.id} 已标记为已关闭（外部关闭）")
                    else:
                        log_key_event("INFO", f"持仓 {position.id} 已标记为未执行（未检测到系统成交记录）")
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
                
                log_key_event(
                    "INFO",
                    f"开始平仓持仓 {position.id} ({position.symbol} {position.side}): 实际数量={actual_quantity}, 方向={close_side}, 原因={reason}",
                )
                
                # 在下单前再次检查持仓状态（避免并发问题）
                self.db.refresh(position)
                if position.status == PositionStatus.CLOSED:
                    logger.debug("持仓 %s 在下单前已被关闭，跳过重复下单", position.id)
                    return
                
                # 使用实际持仓数量平仓，添加 reduceOnly=true 确保这是平仓而不是开新仓
                # 这样可以避免需要额外的保证金（特别是做空持仓平仓时需要买入的情况）
                position_side_for_exchange = "LONG" if position.side == "BUY" else "SHORT"
                result = self.client.place_market_order(
                    position.symbol,
                    close_side,
                    actual_quantity,
                    reduce_only=True,  # 平仓时使用 reduceOnly，避免需要额外保证金（单向模式）
                    position_side=position_side_for_exchange,
                )
            
            finally:
                with _closing_lock:
                    _closing_positions.discard(position_id)
            
            # 记录订单ID
            order_id = result.get("orderId") or result.get("order_id") or str(result.get("clientOrderId", ""))
            order_status = result.get("status", "UNKNOWN")
            
            log_key_event("INFO", "平仓订单已提交: 订单ID=%s, 状态=%s, 结果=%s", order_id, order_status, result)
            
            # 重要：等待订单成交（市价单通常立即成交，但需要确认）
            # 市价单可能初始返回NEW状态，需要等待并查询
            import time
            max_retries = 15  # 增加重试次数（15次 * 0.5秒 = 7.5秒）
            retry_count = 0
            order_filled = False
            
            # 如果初始状态已经是FILLED，直接处理
            if order_status in ["FILLED", "COMPLETED"]:
                order_filled = True
                log_key_event("INFO", "订单立即成交: 订单ID=%s", order_id)
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
                            log_key_event("INFO", "订单已成交: 订单ID=%s, 成交价=%s, 成交数量=%s", 
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
                                    log_key_event("INFO", "使用原始结果: 订单ID=%s, 成交价=%s, 成交数量=%s", 
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
                    log_key_event("INFO", "使用原始结果（订单可能已成交）: 订单ID=%s, 成交价=%s, 成交数量=%s", 
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
            # 更新手动计划状态（如果由手动计划触发）
            self._finalize_manual_plan_if_needed(position.manual_plan_id, position.id)
            
            self.db.commit()
            log_key_event("INFO", "持仓 %s 已关闭，原因: %s", position.id, reason)
            
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
            # 每次同步时重新加载系统配置，确保使用最新默认值
            current_settings = get_settings()
            self.settings = current_settings
            default_trailing_pct = Decimal(str(current_settings.trailing_exit_pct))
            default_stop_loss_pct = Decimal(str(current_settings.stop_loss_pct))
            
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
                    
                    # 辅助函数：比较两个Decimal是否相等（处理精度问题）
                    def is_decimal_equal(d1, d2, epsilon=Decimal("0.0001")):
                        return abs(d1 - d2) < epsilon

                    # 找出有自定义参数的持仓
                    # 使用宽松比较，防止精度问题导致误判
                    custom_positions = [p for p in positions 
                                      if not is_decimal_equal(p.trailing_exit_pct, default_trailing) or 
                                         not is_decimal_equal(p.stop_loss_pct, default_stop_loss)]
                    
                    if custom_positions:
                        # 保留有自定义参数的持仓（如果有多个，保留最新的）
                        keep_position = max(custom_positions, key=lambda p: p.entry_time)
                        logger.info("保留持仓 {} (有自定义退出参数: 滑动退出={}%, 止损={}%)", 
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
                    saved_max_slippage_pct = getattr(position, "max_slippage_pct", None)
                    
                    # 外部持仓：只在首次同步时使用系统默认值，之后保持用户手动设置的值
                    # 判断是否首次同步：如果参数等于系统默认值，可能是首次同步或用户恰好设置为默认值
                    # 为了区分，我们检查是否有用户手动修改的痕迹（通过检查参数是否与默认值不同）
                    # 如果用户手动修改过（不等于默认值），则保持用户设置的值
                    # 如果等于默认值，可能是首次同步，也可能是用户设置为默认值，这种情况下保持当前值即可
                    # 注意：不再强制同步到系统默认值，允许用户设置任意值（包括低于默认值）
                    
                    # 如果数量或价格发生变化，更新（可能是部分平仓或加仓）
                    if position.entry_quantity != entry_quantity or position.entry_price != entry_price:
                        position.entry_quantity = entry_quantity
                        position.entry_price = entry_price
                        position.leverage = Decimal(str(leverage))
                        updated_count += 1
                        logger.debug("更新持仓: {} {} 数量={} 价格={}", symbol, side, entry_quantity, entry_price)
                    
                    # 改进2：检测系统中断（检查 last_check_time）
                    current_price = Decimal(str(mark_price))
                    now = datetime.now(timezone.utc)
                    last_check = position.last_check_time or position.entry_time or now
                    time_since_last_check = (now - last_check).total_seconds()
                    INTERRUPT_THRESHOLD = 300  # 5分钟，超过此时间认为可能中断过
                    is_likely_interrupted = time_since_last_check > INTERRUPT_THRESHOLD
                    
                    # 改进3：如果检测到中断，尝试从K线数据恢复历史最高/最低价
                    recovered_high = None
                    recovered_low = None
                    if is_likely_interrupted and position.last_check_time:
                        try:
                            # 计算需要查询的时间范围（从上次检查到现在）
                            start_time_ms = int(position.last_check_time.timestamp() * 1000)
                            end_time_ms = int(now.timestamp() * 1000)
                            
                            # 根据中断时间动态选择K线精度（精度很重要，滑动退出需要精确的最高/最低价）
                            time_range_hours = (end_time_ms - start_time_ms) / (1000 * 3600)
                            if time_range_hours <= 1:
                                # 1小时内：使用1分钟K线，最多1000条（约16.7小时）
                                interval = "1m"
                                limit = 1000
                            elif time_range_hours <= 8:
                                # 1-8小时：使用1分钟K线，最多500条（约8.3小时）
                                interval = "1m"
                                limit = 500
                            elif time_range_hours <= 24:
                                # 8-24小时：使用5分钟K线，最多500条（约41.7小时）
                                interval = "5m"
                                limit = 500
                            else:
                                # 超过24小时：使用15分钟K线，最多500条（约125小时）
                                interval = "15m"
                                limit = 500
                            
                            logger.info("从K线数据恢复历史价格: %s %s, 中断时间=%.1f小时, 使用K线间隔=%s, limit=%d", 
                                      symbol, side, time_range_hours, interval, limit)
                            
                            # 获取K线数据（使用动态选择的精度）
                            klines = self.client.get_klines(
                                symbol=symbol,
                                interval=interval,
                                limit=limit,
                                start_time=start_time_ms,
                                end_time=end_time_ms
                            )
                            
                            if klines:
                                # K线格式：[开盘时间, 开盘价, 最高价, 最低价, 收盘价, ...]
                                # 提取所有K线的最高价和最低价
                                kline_highs = [Decimal(str(k[2])) for k in klines]  # 最高价
                                kline_lows = [Decimal(str(k[3])) for k in klines]   # 最低价
                                
                                recovered_high = max(kline_highs) if kline_highs else None
                                recovered_low = min(kline_lows) if kline_lows else None
                                
                                if recovered_high or recovered_low:
                                    logger.info("检测到系统中断（%.1f分钟），从K线数据恢复历史价格: %s %s 最高价=%s 最低价=%s", 
                                              time_since_last_check / 60, symbol, side,
                                              recovered_high, recovered_low)
                        except Exception as exc:
                            logger.debug("从K线数据恢复历史价格失败: {}", exc)
                    
                    # 更新最高价和最低价（优先使用恢复的数据）
                    price_updated = False
                    if recovered_high:
                        # 使用恢复的最高价和当前价格中的较大值
                        new_high = max(recovered_high, current_price)
                        if position.highest_price is None or new_high > position.highest_price:
                            old_highest = position.highest_price
                            position.highest_price = new_high
                            price_updated = True
                            logger.info("恢复持仓 %s (%s) 历史最高价: %s -> %s (从K线数据恢复)", 
                                      position.id, symbol, old_highest, new_high)
                    else:
                        # 正常更新（使用当前价格）
                        if position.highest_price is None or current_price > position.highest_price:
                            old_highest = position.highest_price
                            position.highest_price = current_price
                            price_updated = True
                            if old_highest is not None:
                                logger.debug("同步时更新持仓 %s (%s) 历史最高价: %s -> %s", 
                                           position.id, symbol, old_highest, current_price)
                    
                    if recovered_low:
                        # 使用恢复的最低价和当前价格中的较小值
                        new_low = min(recovered_low, current_price)
                        if position.lowest_price is None or new_low < position.lowest_price:
                            old_lowest = position.lowest_price
                            position.lowest_price = new_low
                            price_updated = True
                            logger.info("恢复持仓 %s (%s) 历史最低价: %s -> %s (从K线数据恢复)", 
                                      position.id, symbol, old_lowest, new_low)
                    else:
                        # 正常更新（使用当前价格）
                        if position.lowest_price is None or current_price < position.lowest_price:
                            old_lowest = position.lowest_price
                            position.lowest_price = current_price
                            price_updated = True
                            if old_lowest is not None:
                                logger.debug("同步时更新持仓 %s (%s) 历史最低价: %s -> %s", 
                                           position.id, symbol, old_lowest, current_price)
                    
                    # 如果检测到中断但无法恢复，记录警告并采用保守策略
                    if is_likely_interrupted and not (recovered_high or recovered_low):
                        logger.warning("检测到系统中断（%.1f分钟），但无法从K线数据恢复历史价格，将采用保守策略", 
                                     time_since_last_check / 60)
                        # 保守策略：如果最高/最低价为None，使用入场价初始化（而不是当前价格）
                        if position.highest_price is None:
                            position.highest_price = position.entry_price
                            logger.info("采用保守策略：持仓 %s (%s) 历史最高价初始化为入场价: %s", 
                                      position.id, symbol, position.entry_price)
                        if position.lowest_price is None:
                            position.lowest_price = position.entry_price
                            logger.info("采用保守策略：持仓 %s (%s) 历史最低价初始化为入场价: %s", 
                                      position.id, symbol, position.entry_price)
                    
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
                    if saved_max_slippage_pct is not None and hasattr(position, "max_slippage_pct"):
                        position.max_slippage_pct = saved_max_slippage_pct
                    
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
                        trailing_exit_pct=default_trailing_pct,
                        stop_loss_pct=default_stop_loss_pct,
                        max_slippage_pct=Decimal(str(current_settings.max_slippage_pct)),
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
                        # 重要：检查该持仓是否已经被系统关闭（通过检查执行日志）
                        # 如果系统刚刚自动平仓，可能在币安API同步时已经关闭，不应该误判为外部关闭
                        is_system_closed = False
                        try:
                            from app.models.execution_log import ExecutionLog
                            # 检查最近5分钟内是否有该持仓的系统关闭记录
                            recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)
                            recent_close_log = self.db.scalar(
                                select(ExecutionLog)
                                .where(ExecutionLog.position_id == position.id)
                                .where(ExecutionLog.event_type == "position_closed")
                                .where(ExecutionLog.created_at >= recent_time)
                                .order_by(ExecutionLog.created_at.desc())
                                .limit(1)
                            )
                            if recent_close_log:
                                # 有系统关闭记录，说明是系统关闭的，无论原因是什么
                                # 从执行日志的payload中获取退出原因
                                payload = recent_close_log.payload or {}
                                system_reason = payload.get("reason", "")
                                # 如果存在 position_closed 记录，说明是系统关闭的，无论原因是什么
                                is_system_closed = True
                                logger.info("检测到持仓 {} {} 已被系统关闭（原因: {}），同步时保持系统退出原因", 
                                          position.symbol, position.side, system_reason)
                                # 更新持仓状态，但保留系统设置的退出原因
                                position.status = PositionStatus.CLOSED
                                if not position.exit_time:
                                    position.exit_time = recent_close_log.created_at
                                if not position.exit_reason:
                                    position.exit_reason = system_reason
                                if not position.exit_price and recent_close_log.price:
                                    position.exit_price = Decimal(str(recent_close_log.price))
                                closed_count += 1
                        except Exception as exc:
                            logger.debug("检查系统关闭记录失败: {}，继续二次确认流程", exc)
                        
                        # 如果已经被系统关闭，跳过后续的外部关闭检查
                        if is_system_closed:
                            continue
                        
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
                                    # 确认不存在，标记为关闭（可能是外部关闭或从未真正成交）
                                    reason_used = self._finalize_missing_position(position, position.exit_price or position.entry_price, default_reason="external_closed")
                                    closed_count += 1
                                    logger.info("确认持仓已关闭（币安二次确认，原因: %s）: %s %s", 
                                                reason_used, position.symbol, position.side)
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

    def _calculate_realized_pnl(self, position: Position) -> Decimal:
        if not position.exit_price or position.exit_quantity is None:
            return Decimal("0")
        qty = position.exit_quantity or position.entry_quantity
        if not qty or qty <= 0:
            qty = position.entry_quantity
        if not qty or qty <= 0:
            return Decimal("0")
        qty_dec = Decimal(str(qty))
        entry_price = Decimal(str(position.entry_price))
        exit_price = Decimal(str(position.exit_price))
        if position.side == "BUY":
            pnl = (exit_price - entry_price) * qty_dec
        else:
            pnl = (entry_price - exit_price) * qty_dec
        return pnl

    def get_realized_pnl_summary(self, days: int = 30) -> dict:
        """返回最近 n 天的每日收益和累计收益"""
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=days)
        stmt = (
            select(Position)
            .where(Position.status == PositionStatus.CLOSED)
            .where(Position.exit_time.isnot(None))
            .where(Position.exit_time >= start_time)
            .order_by(Position.exit_time.desc())
        )
        positions = list(self.db.scalars(stmt))
        daily = defaultdict(Decimal)
        total = Decimal("0")
        for pos in positions:
            pnl = self._calculate_realized_pnl(pos)
            if pnl == 0:
                continue
            exit_time = pos.exit_time or end_time
            date_key = exit_time.astimezone(timezone.utc).date().isoformat()
            daily[date_key] += pnl
            total += pnl
        today_key = end_time.astimezone(timezone.utc).date().isoformat()
        daily_list = [
            {"date": date, "pnl": float(amount)}
            for date, amount in sorted(daily.items())
        ]
        return {
            "daily": daily_list,
            "total_pnl": float(total),
            "today_pnl": float(daily.get(today_key, Decimal("0"))),
            "days": days,
        }
