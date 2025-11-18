"""WebSocket服务：实时推送价格和持仓数据"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Set

from fastapi import WebSocket
from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.services.binance_service import BinanceFuturesClient
from app.services.position_service import PositionService


class WebSocketManager:
    """管理WebSocket连接和实时数据推送"""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.settings = get_settings()
        self._running = False
        self._task = None
        self._last_positions_data = None  # 缓存上一次的持仓数据，用于比较
        self._last_pnl = None  # 缓存上一次的PnL
    
    async def connect(self, websocket: WebSocket):
        """接受新的WebSocket连接"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info("WebSocket连接已建立，当前连接数: {}", len(self.active_connections))
        
        # 如果还没有运行推送任务，启动它
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._broadcast_loop())
    
    def disconnect(self, websocket: WebSocket):
        """断开WebSocket连接"""
        self.active_connections.discard(websocket)
        logger.info("WebSocket连接已断开，当前连接数: {}", len(self.active_connections))
        
        # 如果没有连接了，停止推送任务
        if not self.active_connections and self._running:
            self._running = False
            if self._task:
                self._task.cancel()
    
    async def _broadcast_loop(self):
        """广播循环：定期推送实时数据"""
        client = BinanceFuturesClient(self.settings)
        
        while self._running:
            try:
                if not self.active_connections:
                    await asyncio.sleep(0.5)
                    continue
                
                # 获取持仓数据
                positions = []
                try:
                    with SessionLocal() as db:
                        position_service = PositionService(db)
                        positions = position_service.get_active_positions()
                except Exception as exc:
                    logger.debug("获取持仓数据失败: {}", exc)
                
                if positions:
                    # 后端去重：确保每个持仓ID只出现一次
                    seen_position_ids = set()
                    unique_positions = []
                    for pos in positions:
                        if pos.id not in seen_position_ids:
                            seen_position_ids.add(pos.id)
                            unique_positions.append(pos)
                    
                    # 批量获取所有持仓的价格
                    symbols = [pos.symbol for pos in unique_positions]
                    prices = client.get_mark_prices_batch(symbols)
                    
                    # 构建持仓数据
                    position_data = []
                    total_pnl = 0.0
                    
                    for pos in unique_positions:
                        current_price = prices.get(pos.symbol)
                        if current_price:
                            current_price = float(current_price)
                            if pos.side == "BUY":
                                pnl_pct = float((Decimal(str(current_price)) - pos.entry_price) / pos.entry_price * 100)
                            else:
                                pnl_pct = float((pos.entry_price - Decimal(str(current_price))) / pos.entry_price * 100)
                            
                            position_value = float(pos.entry_quantity) * current_price
                            pnl_value = position_value * pnl_pct / 100
                            total_pnl += pnl_value
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
                            "id": str(pos.id),
                            "symbol": pos.symbol,
                            "side": pos.side,
                            "entry_price": float(pos.entry_price),
                            "current_price": current_price,
                            "quantity": float(pos.entry_quantity),
                            "leverage": float(pos.leverage),
                            "principal": principal,  # 本金（实际投入的保证金）
                            "pnl_pct": pnl_pct,
                            "pnl_value": pnl_value,
                            "stop_loss_pct": float(pos.stop_loss_pct),  # 止损百分比
                            "trailing_exit_pct": float(pos.trailing_exit_pct),  # 滑动退出百分比
                            "highest_price": float(pos.highest_price) if pos.highest_price else None,  # 历史最高价（用于滑动退出）
                            "lowest_price": float(pos.lowest_price) if pos.lowest_price else None,  # 历史最低价（用于滑动退出）
                            "trailing_stop_price": trailing_stop_price,  # 滑动退出触发价
                            "trailing_stop_distance": trailing_stop_distance,  # 当前价格距离触发价的距离（正数=还有距离，负数=已触发）
                        })
                    
                    # 生成数据唯一标识（用于比较，避免推送未变化的数据）
                    # 币安风格：提高精度（价格0.01，PnL 0.1%），但不过度过滤
                    current_data_key = json.dumps({
                        "positions": sorted([(p["id"], round(p["current_price"], 2), round(p["pnl_pct"] * 1000) / 1000, round(p["pnl_value"], 2)) for p in position_data]),
                        "total_pnl": round(total_pnl, 2),
                        "count": len(position_data)
                    }, sort_keys=True)
                    
                    # 如果数据没有变化，跳过推送（避免不必要的DOM更新）
                    if current_data_key == self._last_positions_data:
                        await asyncio.sleep(0.05 if positions else 0.5)  # 币安风格：有持仓时50ms推送一次
                        continue
                    
                    self._last_positions_data = current_data_key
                    
                    # 构建消息
                    message = {
                        "type": "positions_update",
                        "data": {
                            "positions": position_data,
                            "total_pnl": total_pnl,
                            "position_count": len(position_data),
                            "timestamp": time.time(),
                        }
                    }
                else:
                    # 无持仓
                    # 如果之前也没有持仓，跳过推送
                    if self._last_positions_data == "[]":
                        await asyncio.sleep(1.0)
                        continue
                    
                    self._last_positions_data = "[]"
                    
                    message = {
                        "type": "positions_update",
                        "data": {
                            "positions": [],
                            "total_pnl": 0.0,
                            "position_count": 0,
                            "timestamp": time.time(),
                        }
                    }
                
                # 广播给所有连接的客户端
                disconnected = set()
                for connection in list(self.active_connections):  # 创建副本避免迭代时修改
                    try:
                        await connection.send_json(message)
                    except Exception as exc:
                        logger.debug("发送WebSocket消息失败: {}", exc)
                        disconnected.add(connection)
                
                # 清理断开的连接
                for conn in disconnected:
                    self.disconnect(conn)
                
                # 根据是否有持仓调整推送频率
                # 币安风格：有持仓时50ms推送一次（更实时），无持仓：1秒
                await asyncio.sleep(0.05 if positions else 1.0)
                
            except Exception as exc:
                logger.error("WebSocket广播循环错误: {}", exc, exc_info=True)
                await asyncio.sleep(1.0)
    
    async def send_personal_message(self, message: dict, websocket: WebSocket):
        """发送个人消息"""
        try:
            await websocket.send_json(message)
        except Exception as exc:
            logger.debug("发送个人消息失败: {}", exc)
            self.disconnect(websocket)


# 全局WebSocket管理器
websocket_manager = WebSocketManager()

