from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response, WebSocket, WebSocketDisconnect, status
from loguru import logger
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.enums import ManualPlanStatus, PositionStatus
from app.models.execution_log import ExecutionLog
from app.models.manual_plan import ManualPlan
from app.models.position import Position
from app.schemas.manual_plan import ManualPlanCreate, ManualPlanRead
from app.services.binance_service import BinanceFuturesClient
from app.services.manual_plan_service import ManualPlanService
from app.services.position_service import PositionService
from app.services.websocket_service import websocket_manager
from app.core.config import get_settings

router = APIRouter(prefix="/api", tags=["Dashboard / 仪表盘"])


@router.get("/manual-plans", response_model=list[ManualPlanRead])
def list_manual_plans(db: Session = Depends(get_db)):
    """获取手动计划列表 / Get Manual Plans List"""
    service = ManualPlanService(db)
    return service.list_all()


@router.post("/manual-plans", response_model=ManualPlanRead)
def create_manual_plan(
    payload: ManualPlanCreate = Body(..., description="手动计划数据 / Manual Plan Data"),
    db: Session = Depends(get_db)
):
    """创建手动计划 / Create Manual Plan"""
    service = ManualPlanService(db)
    plan = service.create(payload.model_dump())
    return plan


@router.post("/manual-plans/{plan_id}/cancel", response_model=ManualPlanRead)
def cancel_manual_plan(
    plan_id: str = Path(..., description="计划ID / Plan ID"),
    db: Session = Depends(get_db)
):
    """取消手动计划 / Cancel Manual Plan"""
    plan = db.get(ManualPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在 / Plan not found")
    plan.status = ManualPlanStatus.CANCELLED
    db.commit()
    db.refresh(plan)
    return plan


@router.get("/realtime/account")
def get_account_info(db: Session = Depends(get_db)):
    """获取实时账户信息 / Get Real-time Account Info"""
    settings = get_settings()
    client = BinanceFuturesClient(settings)
    
    try:
        balance = client.get_account_balance()
        return {
            "balance": float(balance),
            "currency": "USDT",
        }
    except Exception as exc:
        return {"error": str(exc), "balance": 0, "currency": "USDT"}


@router.get("/realtime/positions")
def get_realtime_positions(db: Session = Depends(get_db)):
    """获取实时持仓信息 / Get Real-time Positions"""
    service = PositionService(db)
    positions = service.get_active_positions()
    
    settings = get_settings()
    client = BinanceFuturesClient(settings)
    
    result = []
    for pos in positions:
        # 获取当前价格
        current_price = client.get_mark_price(pos.symbol)
        if current_price:
            current_price = float(current_price)
            # 计算盈亏
            if pos.side == "BUY":
                pnl_pct = float((Decimal(str(current_price)) - pos.entry_price) / pos.entry_price * 100)
            else:
                pnl_pct = float((pos.entry_price - Decimal(str(current_price))) / pos.entry_price * 100)
        else:
            current_price = float(pos.entry_price)
            pnl_pct = 0.0
        
        # 计算盈亏金额
        position_value = float(pos.entry_quantity) * current_price
        pnl_value = position_value * pnl_pct / 100
        
        result.append({
            "id": pos.id,
            "symbol": pos.symbol,
            "side": pos.side,
            "entry_price": float(pos.entry_price),
            "current_price": current_price,
            "quantity": float(pos.entry_quantity),
            "leverage": float(pos.leverage),
            "pnl_pct": pnl_pct,
            "pnl_value": pnl_value,
            "highest_price": float(pos.highest_price) if pos.highest_price else None,
            "lowest_price": float(pos.lowest_price) if pos.lowest_price else None,
            "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
            "last_check_time": pos.last_check_time.isoformat() if pos.last_check_time else None,
            "is_external": pos.is_external,  # 是否为非系统下单的持仓
            "stop_loss_pct": float(pos.stop_loss_pct),  # 止损百分比
            "trailing_exit_pct": float(pos.trailing_exit_pct),  # 滑动退出百分比
        })
    
    return result


@router.get("/pnl/summary")
def get_pnl_summary(
    days: int = Query(30, ge=1, le=365, description="统计最近多少天的收益 / Number of days to include"),
    db: Session = Depends(get_db)
):
    """获取每日收益与累计收益 / Get daily and cumulative PnL summary"""
    service = PositionService(db)
    return service.get_realized_pnl_summary(days=days)


@router.get("/realtime/prices")
def get_realtime_prices(
    symbols: str = Query("", description="交易对列表，用逗号分隔，如 'BTCUSDT,ETHUSDT'。如果为空，则返回所有活跃持仓的交易对 / Symbol list, comma-separated. If empty, returns prices for all active positions"),
    db: Session = Depends(get_db)
):
    """获取实时价格（多个交易对） / Get Real-time Prices (Multiple Symbols)"""
    settings = get_settings()
    client = BinanceFuturesClient(settings)
    
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else []
    
    # 如果没有指定，获取所有活跃持仓的交易对
    if not symbol_list:
        service = PositionService(db)
        positions = service.get_active_positions()
        symbol_list = list(set([pos.symbol for pos in positions]))
    
    result = {}
    for symbol in symbol_list:
        try:
            price = client.get_mark_price(symbol)
            if price:
                result[symbol] = float(price)
        except Exception:
            pass
    
    return result


@router.get("/realtime/dashboard")
def get_realtime_dashboard(
    db: Session = Depends(get_db),
    response: Response = Response(),
    t: str | None = Query(None, description="时间戳参数，用于防止缓存 / Timestamp parameter to prevent caching")
):
    """获取完整的实时仪表盘数据 / Get Complete Real-time Dashboard Data"""
    # 设置响应头，防止浏览器缓存
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Last-Modified"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    
    settings = get_settings()
    client = BinanceFuturesClient(settings)
    position_service = PositionService(db)
    
    # 账户余额（优雅处理错误，不影响其他数据）
    spot_balance = 0.0  # 现货账户余额
    wallet_balance = 0.0  # 资金账户余额（钱包总资产，即资产总额）
    margin_balance = 0.0  # 杠杆账户余额（不显示，仅用于内部计算）
    futures_balance = 0.0  # 合约账户余额
    balance_error = None
    
    # 并行获取所有余额（提高响应速度）
    def fetch_spot_balance():
        try:
            return float(client.get_spot_balance())
        except Exception:
            return 0.0
    
    def fetch_wallet_balance():
        try:
            return float(client.get_wallet_balance())
        except Exception:
            return 0.0
    
    def fetch_margin_balance():
        try:
            return float(client.get_margin_balance())
        except Exception:
            return 0.0
    
    def fetch_futures_balance():
        try:
            return float(client.get_futures_balance())
        except Exception as exc:
            nonlocal balance_error
            balance_error = str(exc)
            return 0.0
    
    # 使用线程池并行执行所有余额查询
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_spot_balance): "spot",
            executor.submit(fetch_wallet_balance): "wallet",
            executor.submit(fetch_margin_balance): "margin",
            executor.submit(fetch_futures_balance): "futures",
        }
        
        for future in as_completed(futures):
            balance_type = futures[future]
            try:
                value = future.result()
                if balance_type == "spot":
                    spot_balance = value
                elif balance_type == "wallet":
                    wallet_balance = value
                elif balance_type == "margin":
                    margin_balance = value
                elif balance_type == "futures":
                    futures_balance = value
            except Exception as exc:
                logger.debug("获取{}余额失败: {}", balance_type, exc)
    
    # 注意：资金账户（wallet_balance）就是资产总额
    # sapi/v3/asset/getUserAsset 返回的就是所有账户的总和（资产总额）
    # 不需要再计算total_balance
    
    # 检查是否有待执行的手动计划在1分钟内
    manual_plan_service = ManualPlanService(db)
    pending_plans = manual_plan_service.get_pending_plans()
    now = datetime.now(timezone.utc)
    has_upcoming_trade = False
    
    for plan in pending_plans:
        if plan.listing_time > now:
            time_diff = (plan.listing_time - now).total_seconds()
            if 0 < time_diff <= 60.0:  # 1分钟内
                has_upcoming_trade = True
                break
    
    # 持仓信息（使用批量获取价格，提高响应速度）
    positions = position_service.get_active_positions()
    
    # 后端去重：确保每个持仓ID只出现一次
    seen_position_ids = set()
    unique_positions = []
    for pos in positions:
        if pos.id not in seen_position_ids:
            seen_position_ids.add(pos.id)
            unique_positions.append(pos)
    
    position_data = []
    total_pnl = 0.0  # 所有持仓的总盈亏（USDT）
    
    if unique_positions:
        # 批量获取所有持仓的价格（一次API调用）
        symbols = [pos.symbol for pos in unique_positions]
        prices = client.get_mark_prices_batch(symbols)
        
        for pos in unique_positions:
            current_price = prices.get(pos.symbol)
            if current_price:
                current_price = float(current_price)
                # 计算盈亏百分比
                if pos.side == "BUY":
                    pnl_pct = float((Decimal(str(current_price)) - pos.entry_price) / pos.entry_price * 100)
                else:
                    pnl_pct = float((pos.entry_price - Decimal(str(current_price))) / pos.entry_price * 100)
                
                # 计算持仓价值和盈亏金额
                position_value = float(pos.entry_quantity) * current_price  # 当前持仓价值
                pnl_value = position_value * pnl_pct / 100  # 盈亏金额（USDT）
                total_pnl += pnl_value  # 累加到总盈亏（所有持仓的总和）
            else:
                current_price = float(pos.entry_price)
                pnl_pct = 0.0
                pnl_value = 0.0
            
            # 计算本金（实际投入的保证金）
            principal = float(pos.entry_price * pos.entry_quantity / pos.leverage)
            
            # 计算滑动退出触发价
            trailing_stop_price = None
            trailing_stop_distance = None
            if pos.side == "BUY" and pos.highest_price:
                # 做多：从最高价回撤trailing_exit_pct时触发
                trailing_stop_price = float(pos.highest_price * (Decimal("1") - pos.trailing_exit_pct))
                trailing_stop_distance = current_price - trailing_stop_price  # 正数表示还有距离，负数表示已触发
            elif pos.side == "SELL" and pos.lowest_price:
                # 做空：从最低价上涨trailing_exit_pct时触发
                trailing_stop_price = float(pos.lowest_price * (Decimal("1") + pos.trailing_exit_pct))
                trailing_stop_distance = trailing_stop_price - current_price  # 正数表示还有距离，负数表示已触发
            
            position_data.append({
                "id": pos.id,
                "symbol": pos.symbol,
                "side": pos.side,
                "entry_price": float(pos.entry_price),
                "current_price": current_price,
                "quantity": float(pos.entry_quantity),
                "leverage": float(pos.leverage),
                "principal": principal,  # 本金（实际投入的保证金）
                "pnl_pct": pnl_pct,
                "pnl_value": pnl_value,
                "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
                "stop_loss_pct": float(pos.stop_loss_pct),  # 止损百分比
                "trailing_exit_pct": float(pos.trailing_exit_pct),  # 滑动退出百分比
                "highest_price": float(pos.highest_price) if pos.highest_price else None,  # 历史最高价（用于滑动退出）
                "lowest_price": float(pos.lowest_price) if pos.lowest_price else None,  # 历史最低价（用于滑动退出）
                "trailing_stop_price": trailing_stop_price,  # 滑动退出触发价
                "trailing_stop_distance": trailing_stop_distance,  # 当前价格距离触发价的距离（正数=还有距离，负数=已触发）
            })
    
    return {
        "account": {
            "spot_balance": spot_balance,  # 现货账户余额 / Spot account balance
            "futures_balance": futures_balance,  # 合约账户余额 / Futures account balance
            "wallet_balance": wallet_balance,  # 资金账户余额（资产总额）/ Wallet balance (Total Assets)
            "balance": futures_balance,  # 保持向后兼容 / Backward compatibility
            "currency": "USDT",
            "total_pnl": total_pnl,  # 总盈亏 / Total P&L
            "error": balance_error,  # 错误信息 / Error message
        },
        "positions": position_data,  # 持仓列表 / Positions list
        "position_count": len(position_data),  # 持仓数量 / Position count
        "has_upcoming_trade": has_upcoming_trade,  # 是否有待执行交易在1分钟内 / Has upcoming trade within 1 minute
    }


@router.get("/history")
def get_trading_history(
    db: Session = Depends(get_db),
    limit: int = Query(100, description="返回记录数量 / Number of records to return"),
    offset: int = Query(0, description="偏移量 / Offset"),
    include_logs: bool = Query(False, description="是否包含详细执行日志，默认仅返回已完成持仓"),
):
    """
    获取交易历史记录 / Get Trading History
    包括所有买入、退出记录及退出条件等信息
    """
    from sqlalchemy import desc, select
    from app.models.enums import PositionStatus
    
    history: list[dict] = []
    allowed_log_events = {"order_filled", "position_closed"}

    logs: list[ExecutionLog] = []
    if include_logs:
        logs_stmt = (
            select(ExecutionLog)
            .where(ExecutionLog.event_type.in_(allowed_log_events))
            .order_by(desc(ExecutionLog.created_at))
            .limit(limit)
            .offset(offset)
        )
        logs = list(db.scalars(logs_stmt))
    
    # 获取所有已关闭的持仓（包含完整的退出信息）
    positions_stmt = (
        select(Position)
        .where(Position.status == PositionStatus.CLOSED)
        .order_by(desc(Position.exit_time))
        .limit(limit)
        .offset(offset)
    )
    positions = list(db.scalars(positions_stmt))
    
    # 构建历史记录
    # 处理执行日志
    for log in logs:
        history.append(
            {
                "type": "execution_log",
                "id": log.id,
                "event_type": log.event_type,
                "symbol": log.symbol,
                "side": log.side,
                "price": float(log.price) if log.price else None,
                "quantity": float(log.quantity) if log.quantity else None,
                "order_id": log.order_id,
                "status": log.status,
                "timestamp": log.created_at.isoformat() if log.created_at else None,
                "payload": log.payload,
                "manual_plan_id": log.manual_plan_id,
                "trade_plan_id": log.trade_plan_id,
                "position_id": log.position_id,
            }
        )
    
    # 处理持仓记录（包含完整的退出信息）
    for pos in positions:
        # 计算盈亏
        pnl_pct = None
        if pos.exit_price and pos.entry_price:
            if pos.side == "BUY":
                pnl_pct = float((pos.exit_price - pos.entry_price) / pos.entry_price * 100)
            else:
                pnl_pct = float((pos.entry_price - pos.exit_price) / pos.entry_price * 100)
        
        # 确定时间戳（优先使用退出时间，否则使用入场时间）
        timestamp = pos.exit_time.isoformat() if pos.exit_time else (pos.entry_time.isoformat() if pos.entry_time else None)
        
        history.append({
            "type": "position",
            "id": pos.id,
            "symbol": pos.symbol,
            "side": pos.side,
            "entry_price": float(pos.entry_price) if pos.entry_price else None,
            "exit_price": float(pos.exit_price) if pos.exit_price else None,
            "entry_quantity": float(pos.entry_quantity) if pos.entry_quantity else None,
            "exit_quantity": float(pos.exit_quantity) if pos.exit_quantity else None,
            "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
            "exit_time": pos.exit_time.isoformat() if pos.exit_time else None,
            "timestamp": timestamp,  # 添加timestamp字段，用于排序
            "exit_reason": pos.exit_reason,  # 退出原因：stop_loss, trailing_stop, external_closed, manual
            "leverage": float(pos.leverage) if pos.leverage else None,
            "stop_loss_pct": float(pos.stop_loss_pct) if pos.stop_loss_pct else None,
            "trailing_exit_pct": float(pos.trailing_exit_pct) if pos.trailing_exit_pct else None,
            "highest_price": float(pos.highest_price) if pos.highest_price else None,
            "lowest_price": float(pos.lowest_price) if pos.lowest_price else None,
            "pnl_pct": pnl_pct,
            "is_external": pos.is_external,
            "manual_plan_id": str(pos.manual_plan_id) if pos.manual_plan_id else None,
            "trade_plan_id": str(pos.trade_plan_id) if pos.trade_plan_id else None,
            "order_id": None,  # 持仓记录没有order_id，使用None
        })
    
    # 按时间排序（最新的在前）
    history.sort(key=lambda x: x.get("timestamp") or x.get("exit_time") or x.get("entry_time") or "", reverse=True)
    
    return {
        "total": len(history),
        "history": history[:limit],  # 确保不超过limit
    }


@router.put("/positions/{position_id}/exit-params")
def update_position_exit_params(
    position_id: str,
    stop_loss_pct: float | None = Body(None, description="止损百分比 (0-1) / Stop loss percentage (0-1)"),
    trailing_exit_pct: float | None = Body(None, description="滑动退出百分比 (0-1) / Trailing exit percentage (0-1)"),
    db: Session = Depends(get_db),
):
    """
    更新持仓的退出参数 / Update Position Exit Parameters
    允许实时修改每个持仓的止损和滑动退出百分比
    """
    from app.models.position import Position
    
    position = db.get(Position, position_id)
    if not position:
        raise HTTPException(status_code=404, detail="持仓不存在 / Position not found")
    
    # 如果状态不是ACTIVE，先检查币安上是否真的存在该持仓
    # 这可以修复因同步逻辑误关闭导致的错误状态
    if position.status != PositionStatus.ACTIVE:
        logger.warning("持仓 %s (%s) 状态为 %s，尝试从币安验证实际状态", 
                     position_id, position.symbol, position.status)
        
        # 从币安API验证持仓是否真的存在
        try:
            settings = get_settings()
            client = BinanceFuturesClient(settings)
            binance_positions = client.get_positions_from_binance()
            
            if binance_positions:
                # 检查币安上是否有该持仓
                found = False
                for bp in binance_positions:
                    if bp["symbol"] == position.symbol and bp["side"] == position.side:
                        # 持仓在币安上存在，但数据库状态错误，修复状态
                        old_status = position.status
                        position.status = PositionStatus.ACTIVE
                        position.exit_time = None
                        position.exit_reason = None
                        db.commit()
                        logger.info("修复持仓状态: %s (%s) 从 %s 恢复为 ACTIVE（币安上确认存在）", 
                                  position_id, position.symbol, old_status)
                        found = True
                        break
                
                if not found:
                    # 持仓确实不存在，返回详细错误
                    status_map = {
                        PositionStatus.CLOSED: "已关闭 / Closed",
                        PositionStatus.LIQUIDATED: "已清算 / Liquidated",
                    }
                    status_text = status_map.get(position.status, str(position.status))
                    raise HTTPException(
                        status_code=400, 
                        detail=f"只能修改活跃持仓的退出参数。当前持仓状态: {status_text}。币安上已确认该持仓不存在。 / Can only update exit parameters for active positions. Current position status: {status_text}. Position confirmed not found on Binance."
                    )
            else:
                # 币安API调用失败或返回空列表，可能是临时API问题
                # 为了用户体验，允许修改（可能是临时API问题导致状态错误）
                logger.warning("币安API调用失败或返回空列表，无法验证持仓状态，但允许修改退出参数（可能是临时API问题）")
                # 临时恢复状态为ACTIVE，允许修改
                old_status = position.status
                position.status = PositionStatus.ACTIVE
                position.exit_time = None
                position.exit_reason = None
                db.commit()
                logger.info("临时修复持仓状态: %s (%s) 从 %s 恢复为 ACTIVE（币安API验证失败，允许修改）", 
                          position_id, position.symbol, old_status)
        except HTTPException:
            # 重新抛出HTTP异常
            raise
        except Exception as exc:
            # 如果验证失败，但用户明确要修改，我们允许修改（可能是临时API问题）
            logger.error("验证持仓状态时出错: %s，但允许继续修改（可能是临时API问题）", exc, exc_info=True)
            # 临时恢复状态为ACTIVE
            old_status = position.status
            position.status = PositionStatus.ACTIVE
            position.exit_time = None
            position.exit_reason = None
            db.commit()
            logger.info("临时修复持仓状态: %s (%s) 从 %s 恢复为 ACTIVE（验证出错，允许修改）", 
                      position_id, position.symbol, old_status)
    
    updates = []
    
    if stop_loss_pct is not None:
        if stop_loss_pct < 0 or stop_loss_pct > 1:
            raise HTTPException(status_code=400, detail="止损百分比必须在0-1之间 / Stop loss percentage must be between 0 and 1")
        position.stop_loss_pct = Decimal(str(stop_loss_pct))
        updates.append(f"止损百分比 / Stop loss: {stop_loss_pct * 100}%")
    
    if trailing_exit_pct is not None:
        if trailing_exit_pct < 0 or trailing_exit_pct > 1:
            raise HTTPException(status_code=400, detail="滑动退出百分比必须在0-1之间 / Trailing exit percentage must be between 0 and 1")
        position.trailing_exit_pct = Decimal(str(trailing_exit_pct))
        updates.append(f"滑动退出百分比 / Trailing exit: {trailing_exit_pct * 100}%")
        
        # 重要提示：修改滑动退出百分比后，系统会继续使用之前记录的highest_price/lowest_price
        # 这确保了即使修改了比率，也能基于历史最高/最低价准确退出
        highest_info = f"最高价: {float(position.highest_price)}" if position.highest_price else "最高价: 未记录"
        lowest_info = f"最低价: {float(position.lowest_price)}" if position.lowest_price else "最低价: 未记录"
        logger.info("持仓 %s (%s) 滑动退出百分比已更新为 %s%%，将继续使用历史 %s / %s 进行退出判断", 
                  position_id, position.symbol, trailing_exit_pct * 100, 
                  highest_info if position.side == "BUY" else lowest_info,
                  lowest_info if position.side == "BUY" else highest_info)
    
    if not updates:
        raise HTTPException(status_code=400, detail="至少需要提供一个参数 / At least one parameter is required")
    
    db.commit()
    db.refresh(position)
    
    logger.info("持仓 %s (%s) 的退出参数已更新: %s", position_id, position.symbol, ", ".join(updates))
    
    return {
        "success": True,
        "message": f"退出参数已更新 / Exit parameters updated: {', '.join(updates)}",
        "position": {
            "id": position.id,
            "symbol": position.symbol,
            "stop_loss_pct": float(position.stop_loss_pct),
            "trailing_exit_pct": float(position.trailing_exit_pct),
        }
    }


@router.websocket("/ws/realtime")
async def websocket_realtime(websocket: WebSocket):
    """WebSocket实时数据推送 / WebSocket Real-time Data Push"""
    await websocket_manager.connect(websocket)
    try:
        while True:
            # 保持连接活跃，等待客户端消息（如果需要）
            # 使用receive_text()保持连接，但不强制要求客户端发送消息
            try:
                # 设置超时，避免阻塞
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # 可以处理客户端发送的消息（如订阅特定交易对）
                logger.debug("收到WebSocket消息: {}", data)
            except asyncio.TimeoutError:
                # 超时是正常的，继续循环保持连接
                continue
    except WebSocketDisconnect:
        websocket_manager.disconnect(websocket)
    except Exception as exc:
        logger.error("WebSocket错误: {}", exc, exc_info=True)
        websocket_manager.disconnect(websocket)


@router.get("/settings")
def get_settings_api():
    """获取当前系统配置 / Get Current System Settings"""
    settings = get_settings()
    return {
        "order_type": settings.order_type,
        "max_slippage_pct": settings.max_slippage_pct,
        "limit_order_timeout_seconds": settings.limit_order_timeout_seconds,
        "max_order_amount": settings.max_order_amount,
        "leverage": settings.leverage,
        "position_pct": settings.position_pct,
        "trailing_exit_pct": settings.trailing_exit_pct,
        "stop_loss_pct": settings.stop_loss_pct,
    }


@router.put("/settings")
def update_settings_api(
    order_type: str | None = Body(None, description="订单类型：MARKET 或 LIMIT / Order type: MARKET or LIMIT"),
    max_slippage_pct: float | None = Body(None, description="最大滑点百分比 / Max slippage percentage"),
    limit_order_timeout_seconds: int | None = Body(None, description="限价单超时时间（秒）/ Limit order timeout (seconds)"),
    max_order_amount: float | None = Body(None, description="单笔订单最大购买金额（USDT）/ Max order amount (USDT)"),
    leverage: int | None = Body(None, description="默认杠杆倍数 / Default leverage"),
    position_pct: float | None = Body(None, description="默认使用可用保证金的比例 / Default position percentage"),
    trailing_exit_pct: float | None = Body(None, description="滑动退出百分比 / Trailing exit percentage"),
    stop_loss_pct: float | None = Body(None, description="止损百分比 / Stop loss percentage"),
):
    """更新系统配置（需要重启服务生效）/ Update System Settings (requires service restart)"""
    import os
    from pathlib import Path
    
    try:
        # 读取当前 .env 文件（从项目根目录）
        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        env_file = base_dir / ".env"
        if not env_file.exists():
            raise HTTPException(status_code=404, detail="未找到 .env 文件 / .env file not found")
        
        # 读取现有配置
        env_vars = {}
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()
        
        # 更新配置
        updates = []
        if order_type is not None:
            if order_type.upper() not in ["MARKET", "LIMIT"]:
                raise HTTPException(status_code=400, detail="订单类型必须是 MARKET 或 LIMIT / Order type must be MARKET or LIMIT")
            env_vars["ORDER_TYPE"] = order_type.upper()
            updates.append(f"订单类型 / Order type: {order_type.upper()}")
        
        if max_slippage_pct is not None:
            if max_slippage_pct < 0:
                raise HTTPException(status_code=400, detail="最大滑点不能为负数 / Max slippage cannot be negative")
            env_vars["MAX_SLIPPAGE_PCT"] = str(max_slippage_pct)
            updates.append(f"最大滑点 / Max slippage: {max_slippage_pct}%")
        
        if limit_order_timeout_seconds is not None:
            if limit_order_timeout_seconds < 0:
                raise HTTPException(status_code=400, detail="超时时间不能为负数 / Timeout cannot be negative")
            env_vars["LIMIT_ORDER_TIMEOUT_SECONDS"] = str(limit_order_timeout_seconds)
            updates.append(f"限价单超时 / Limit order timeout: {limit_order_timeout_seconds}s")
        
        if max_order_amount is not None:
            if max_order_amount < 0:
                raise HTTPException(status_code=400, detail="最大订单金额不能为负数 / Max order amount cannot be negative")
            if max_order_amount == 0:
                env_vars.pop("MAX_ORDER_AMOUNT", None)  # 删除配置表示不限制
            else:
                env_vars["MAX_ORDER_AMOUNT"] = str(max_order_amount)
            updates.append(f"最大订单金额 / Max order amount: {max_order_amount} USDT" if max_order_amount > 0 else "最大订单金额 / Max order amount: 无限制 / Unlimited")
        
        if leverage is not None:
            if leverage < 1:
                raise HTTPException(status_code=400, detail="杠杆倍数必须大于0 / Leverage must be greater than 0")
            env_vars["LEVERAGE"] = str(leverage)
            updates.append(f"杠杆倍数 / Leverage: {leverage}x")
        
        if position_pct is not None:
            if not 0 < position_pct <= 1:
                raise HTTPException(status_code=400, detail="仓位比例必须在0-1之间 / Position percentage must be between 0 and 1")
            env_vars["POSITION_PCT"] = str(position_pct)
            updates.append(f"仓位比例 / Position percentage: {position_pct * 100}%")
        
        if trailing_exit_pct is not None:
            if trailing_exit_pct < 0:
                raise HTTPException(status_code=400, detail="滑动退出百分比不能为负数 / Trailing exit percentage cannot be negative")
            env_vars["TRAILING_EXIT_PCT"] = str(trailing_exit_pct)
            updates.append(f"滑动退出百分比 / Trailing exit: {trailing_exit_pct * 100}%")
        
        if stop_loss_pct is not None:
            if stop_loss_pct < 0:
                raise HTTPException(status_code=400, detail="止损百分比不能为负数 / Stop loss percentage cannot be negative")
            env_vars["STOP_LOSS_PCT"] = str(stop_loss_pct)
            updates.append(f"止损百分比 / Stop loss: {stop_loss_pct * 100}%")
        
        if not updates:
            return {
                "success": False,
                "message": "没有提供要更新的配置项 / No configuration items provided"
            }
        
        # 读取原始文件内容（保留注释和格式）
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # 更新或添加配置项
        updated_lines = []
        updated_keys = set()
        
        # 需要更新的配置项（只更新用户提交的配置）
        keys_to_update = set()
        if order_type is not None:
            keys_to_update.add("ORDER_TYPE")
        if max_slippage_pct is not None:
            keys_to_update.add("MAX_SLIPPAGE_PCT")
        if limit_order_timeout_seconds is not None:
            keys_to_update.add("LIMIT_ORDER_TIMEOUT_SECONDS")
        if max_order_amount is not None:
            keys_to_update.add("MAX_ORDER_AMOUNT")
        if leverage is not None:
            keys_to_update.add("LEVERAGE")
        if position_pct is not None:
            keys_to_update.add("POSITION_PCT")
        if trailing_exit_pct is not None:
            keys_to_update.add("TRAILING_EXIT_PCT")
        if stop_loss_pct is not None:
            keys_to_update.add("STOP_LOSS_PCT")
        
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in keys_to_update:
                    # 更新这个配置项
                    updated_lines.append(f"{key}={env_vars[key]}\n")
                    updated_keys.add(key)
                    continue
            
            updated_lines.append(line)
        
        # 添加新的配置项（如果之前不存在）
        for key in keys_to_update:
            if key not in updated_keys:
                updated_lines.append(f"{key}={env_vars[key]}\n")
        
        # 写入文件
        try:
            with open(env_file, "w", encoding="utf-8") as f:
                f.writelines(updated_lines)
            logger.info("配置已写入 .env 文件: {}", env_file)
        except Exception as exc:
            logger.error("写入 .env 文件失败: {}", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=f"写入配置文件失败 / Failed to write config file: {str(exc)}")
        
        # 同时更新 os.environ，确保 pydantic-settings 能读取到新值
        # pydantic-settings 的 BaseSettings 优先使用 os.environ，然后才读取 .env 文件
        for key, value in env_vars.items():
            if key in keys_to_update:
                os.environ[key] = value
                logger.debug("已更新 os.environ[{}] = {}", key, value)
        
        # 清除缓存，使新配置生效
        from app.core.config import get_settings
        get_settings.cache_clear()
        
        # 强制重新加载配置以验证
        try:
            # 重新创建 Settings 实例（因为缓存已清除）
            new_settings = get_settings()
            logger.info("配置已更新: {}", ", ".join(updates))
            logger.debug("更新的配置项: {}", keys_to_update)
            logger.debug("写入的配置值: {}", {k: env_vars.get(k) for k in keys_to_update})
            logger.debug("重新加载后的配置值: leverage={}, position_pct={}, trailing_exit_pct={}, stop_loss_pct={}", 
                        new_settings.leverage, new_settings.position_pct, 
                        new_settings.trailing_exit_pct, new_settings.stop_loss_pct)
            
            # 验证配置是否正确加载
            if leverage is not None and new_settings.leverage != leverage:
                logger.warning("配置值不匹配: 期望 leverage={}, 实际={}", leverage, new_settings.leverage)
            if position_pct is not None and abs(new_settings.position_pct - position_pct) > 0.0001:
                logger.warning("配置值不匹配: 期望 position_pct={}, 实际={}", position_pct, new_settings.position_pct)
            if trailing_exit_pct is not None and abs(new_settings.trailing_exit_pct - trailing_exit_pct) > 0.0001:
                logger.warning("配置值不匹配: 期望 trailing_exit_pct={}, 实际={}", trailing_exit_pct, new_settings.trailing_exit_pct)
            if stop_loss_pct is not None and abs(new_settings.stop_loss_pct - stop_loss_pct) > 0.0001:
                logger.warning("配置值不匹配: 期望 stop_loss_pct={}, 实际={}", stop_loss_pct, new_settings.stop_loss_pct)
        except Exception as exc:
            logger.warning("重新加载配置失败（可能需要重启服务）: {}", exc)
        
        return {
            "success": True,
            "message": f"配置已更新，请重启服务使更改生效 / Settings updated, please restart service for changes to take effect",
            "updates": updates,
            "warning": "需要重启服务才能完全生效 / Service restart required for changes to take full effect"
        }
        
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("更新配置失败: {}", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"更新配置失败 / Failed to update settings: {str(exc)}")
