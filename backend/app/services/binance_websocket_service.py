"""币安WebSocket价格订阅服务：实时订阅标记价格并维护缓存"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from threading import Lock
from typing import Set, Any

import websocket
from loguru import logger

from app.core.config import Settings, get_settings


class BinanceWebSocketPriceService:
    """币安WebSocket价格订阅服务
    
    功能：
    1. 订阅币安合约标记价格流
    2. 维护实时价格缓存
    3. 自动重连机制
    4. 线程安全的价格访问
    """
    
    # 类级别的价格缓存（所有实例共享）
    _price_cache: dict[str, tuple[Decimal, float]] = {}  # {symbol: (price, timestamp)}
    _cache_lock = Lock()
    _ws_connections: dict[str, websocket.WebSocketApp] = {}  # {symbol: ws_connection}
    _running = False
    _thread: threading.Thread | None = None
    
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._subscribed_symbols: Set[str] = set()
        self._reconnect_interval = 5  # 重连间隔（秒）
        self._max_reconnect_attempts = 10  # 最大重连次数
        
    def start(self, symbols: list[str] | None = None) -> None:
        """启动WebSocket价格订阅服务
        
        Args:
            symbols: 要订阅的交易对列表（如 ['BTCUSDT', 'ETHUSDT']），如果为None则不订阅任何交易对（按需订阅模式）
        """
        if self._running:
            logger.warning("WebSocket价格订阅服务已在运行")
            return
        
        # 如果提供了symbols，则订阅；否则不订阅任何交易对（按需订阅模式）
        if symbols is None:
            symbols = []  # 不订阅任何交易对，等待按需订阅
        else:
            # 转换为大写并去重
            symbols = list(set([s.upper() for s in symbols]))
        
        self._subscribed_symbols = set(symbols)
        
        self._running = True
        self._thread = threading.Thread(
            target=self._run_websocket_loop,
            daemon=True,
            name="BinanceWebSocketPriceService"
        )
        self._thread.start()
        logger.info("WebSocket价格订阅服务已启动，订阅 {} 个交易对", len(symbols))
    
    def stop(self) -> None:
        """停止WebSocket价格订阅服务"""
        self._running = False
        
        # 关闭所有WebSocket连接
        for symbol, ws in list(self._ws_connections.items()):
            try:
                ws.close()
            except Exception:
                pass
        self._ws_connections.clear()
        
        logger.info("WebSocket价格订阅服务已停止")
    
    def subscribe_symbol(self, symbol: str) -> None:
        """动态订阅新的交易对
        
        Args:
            symbol: 交易对（如 'BTCUSDT'）
        """
        symbol = symbol.upper()
        if symbol in self._subscribed_symbols:
            return
        
        self._subscribed_symbols.add(symbol)
        logger.info("动态订阅交易对: {}", symbol)
        
        # 如果服务正在运行，立即建立连接
        if self._running:
            self._connect_symbol(symbol)
    
    def unsubscribe_symbol(self, symbol: str) -> None:
        """取消订阅交易对（关闭WebSocket连接）
        
        Args:
            symbol: 交易对（如 'BTCUSDT'）
        """
        symbol = symbol.upper()
        if symbol not in self._subscribed_symbols:
            return
        
        # 关闭WebSocket连接
        if symbol in self._ws_connections:
            try:
                ws = self._ws_connections[symbol]
                ws.close()
            except Exception as exc:
                logger.debug("关闭WebSocket连接失败 ({}): {}", symbol, exc)
            del self._ws_connections[symbol]
        
        # 从订阅列表中移除
        self._subscribed_symbols.discard(symbol)
        
        # 清理价格缓存（可选，保留也可以）
        # with self._cache_lock:
        #     if symbol in self._price_cache:
        #         del self._price_cache[symbol]
        
        logger.info("已取消订阅交易对: {}", symbol)
    
    def get_price(self, symbol: str) -> Decimal | None:
        """从WebSocket缓存获取价格
        
        Args:
            symbol: 交易对（如 'BTCUSDT'）
            
        Returns:
            标记价格，如果未订阅或缓存中没有则返回None
        """
        symbol = symbol.upper()
        
        with self._cache_lock:
            if symbol in self._price_cache:
                price, timestamp = self._price_cache[symbol]
                # 如果缓存超过5秒，认为数据可能过期
                if time.time() - timestamp < 5.0:
                    return price
        
        # 如果未订阅，尝试订阅
        if symbol not in self._subscribed_symbols and self._running:
            self.subscribe_symbol(symbol)
        
        return None
    
    def get_all_prices(self) -> dict[str, Decimal]:
        """获取所有已订阅交易对的价格
        
        Returns:
            {symbol: price} 字典
        """
        result = {}
        with self._cache_lock:
            for symbol, (price, timestamp) in self._price_cache.items():
                # 只返回5秒内的有效价格
                if time.time() - timestamp < 5.0:
                    result[symbol] = price
        return result
    
    def get_status(self) -> dict[str, Any]:
        with self._cache_lock:
            cache_size = len(self._price_cache)
        return {
            "running": self._running,
            "subscribed_symbols": len(self._subscribed_symbols),
            "cached_symbols": cache_size,
        }
    
    def is_price_available(self, symbol: str) -> bool:
        """检查指定交易对的价格是否可用
        
        Args:
            symbol: 交易对
            
        Returns:
            如果价格可用且未过期则返回True
        """
        symbol = symbol.upper()
        with self._cache_lock:
            if symbol in self._price_cache:
                _, timestamp = self._price_cache[symbol]
                return time.time() - timestamp < 5.0
        return False
    
    def _run_websocket_loop(self) -> None:
        """WebSocket主循环（在后台线程中运行）"""
        while self._running:
            try:
                # 为每个交易对建立WebSocket连接
                for symbol in list(self._subscribed_symbols):
                    if not self._running:
                        break
                    
                    if symbol not in self._ws_connections:
                        self._connect_symbol(symbol)
                    
                    # 检查连接是否还活着（通过价格缓存时间判断）
                    # 如果价格缓存超过10秒未更新，认为连接可能断开
                    with self._cache_lock:
                        if symbol in self._price_cache:
                            _, timestamp = self._price_cache[symbol]
                            if time.time() - timestamp > 10.0:
                                logger.warning("交易对 {} 的价格缓存已过期，尝试重连", symbol)
                                if symbol in self._ws_connections:
                                    del self._ws_connections[symbol]
                                self._connect_symbol(symbol)
                
                # 每10秒检查一次连接状态
                time.sleep(10)
                
            except Exception as exc:
                logger.error("WebSocket主循环错误: {}", exc, exc_info=True)
                time.sleep(self._reconnect_interval)
    
    def _connect_symbol(self, symbol: str) -> None:
        """为指定交易对建立WebSocket连接
        
        Args:
            symbol: 交易对
        """
        if symbol in self._ws_connections:
            return
        
        # 币安WebSocket URL：单个标记价格流
        ws_url = f"wss://fstream.binance.com/ws/{symbol.lower()}@markPrice"
        
        def on_message(ws, message):
            """处理WebSocket消息"""
            try:
                data = json.loads(message)
                
                # 币安标记价格更新格式：
                # {"e":"markPriceUpdate","E":1234567890,"s":"BTCUSDT","p":"50000.00","r":"0.0001","T":1234567890}
                if data.get("e") == "markPriceUpdate":
                    msg_symbol = data.get("s", "").upper()
                    price_str = data.get("p", "0")
                    
                    if msg_symbol and price_str:
                        price = Decimal(price_str)
                        with self._cache_lock:
                            self._price_cache[msg_symbol] = (price, time.time())
                        
                        logger.debug("价格更新: {} = {}", msg_symbol, price)
                
            except Exception as exc:
                logger.error("处理WebSocket消息失败: {}", exc)
        
        def on_error(ws, error):
            """处理WebSocket错误"""
            logger.warning("WebSocket错误 ({}): {}", symbol, error)
        
        def on_close(ws, close_status_code, close_msg):
            """WebSocket连接关闭"""
            logger.info("WebSocket连接已关闭: {} (code: {}, msg: {})", symbol, close_status_code, close_msg)
            if symbol in self._ws_connections:
                del self._ws_connections[symbol]
            
            # 如果服务还在运行，尝试重连
            if self._running and symbol in self._subscribed_symbols:
                time.sleep(self._reconnect_interval)
                self._connect_symbol(symbol)
        
        def on_open(ws):
            """WebSocket连接打开"""
            logger.info("WebSocket连接已建立: {}", symbol)
        
        try:
            # 创建WebSocket连接
            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open
            )
            
            # 在后台线程中运行WebSocket
            ws_thread = threading.Thread(
                target=ws.run_forever,
                daemon=True,
                name=f"WS-{symbol}"
            )
            ws_thread.start()
            
            self._ws_connections[symbol] = ws
            
        except Exception as exc:
            logger.error("建立WebSocket连接失败 ({}): {}", symbol, exc, exc_info=True)


# 全局单例实例
_websocket_service: BinanceWebSocketPriceService | None = None


def get_websocket_price_service() -> BinanceWebSocketPriceService:
    """获取WebSocket价格订阅服务单例"""
    global _websocket_service
    if _websocket_service is None:
        _websocket_service = BinanceWebSocketPriceService()
    return _websocket_service

