from __future__ import annotations

import time
from collections import defaultdict
from decimal import Decimal
from threading import Lock

from binance.client import Client
from loguru import logger

from app.core.config import Settings, get_settings
from app.services.binance_websocket_service import get_websocket_price_service


class BinanceFuturesClient:
    # 类级别的缓存（所有实例共享）
    _price_cache: dict[str, tuple[Decimal, float]] = {}  # {symbol: (price, timestamp)}
    _balance_cache: dict[str, tuple[float, float]] = {}  # {balance_type: (value, timestamp)}
    _all_prices_cache: dict[str, tuple[dict[str, Decimal], float]] = {}  # {"all": ({symbol: price}, timestamp)}
    _symbol_info_cache: dict[str, dict] = {}  # {symbol: {stepSize, tickSize, ...}}
    _cache_lock = Lock()
    
    @classmethod
    def clear_balance_cache(cls, balance_type: str | None = None) -> None:
        """清除余额缓存（公共方法，线程安全）

        Args:
            balance_type: 要清除的缓存类型，可选值: "futures", "spot", "margin", "wallet"
                         如果为None则清除所有缓存
        """
        with cls._cache_lock:
            if balance_type is None:
                cls._balance_cache.clear()
                logger.debug("已清除所有余额缓存")
            elif balance_type in cls._balance_cache:
                del cls._balance_cache[balance_type]
                logger.debug("已清除 {} 余额缓存", balance_type)

    @classmethod
    def clear_price_cache(cls, symbol: str | None = None) -> None:
        """清除价格缓存（公共方法，线程安全）

        Args:
            symbol: 要清除的交易对，如果为None则清除所有价格缓存
        """
        with cls._cache_lock:
            if symbol is None:
                cls._price_cache.clear()
                cls._all_prices_cache.clear()
                logger.debug("已清除所有价格缓存")
            elif symbol in cls._price_cache:
                del cls._price_cache[symbol]
                logger.debug("已清除 {} 价格缓存", symbol)

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

        # 配置代理（如果设置了 HTTP_PROXY，支持 Clash 等 VPN 代理）
        proxies = None
        if self.settings.http_proxy or self.settings.https_proxy:
            proxies = {
                "http": self.settings.http_proxy or self.settings.https_proxy,
                "https": self.settings.https_proxy or self.settings.http_proxy,
            }
            # 只在首次初始化时记录代理信息，避免重复日志
            if not hasattr(BinanceFuturesClient, '_proxy_logged'):
                proxy_info = f"HTTP: {self.settings.http_proxy or '未设置'}, HTTPS: {self.settings.https_proxy or '未设置'}"
                logger.info("使用代理: {}", proxy_info)
                BinanceFuturesClient._proxy_logged = True
        
        # 使用 Client 类，通过 base_endpoint 参数指定合约交易端点
        # 注意：禁用 ping 以避免初始化时的网络请求
        self.client = Client(
            api_key=self.settings.binance_api_key,
            api_secret=self.settings.binance_api_secret,
            base_endpoint="https://fapi.binance.com",  # 币安合约交易 API 端点
            ping=False,  # 禁用初始化时的 ping，避免网络错误
        )
        
        # 如果设置了代理，配置 requests session 的代理
        # binance 库内部使用 requests，需要手动设置 session 的代理
        if proxies:
            self.client.session.proxies.update(proxies)

    def get_symbol_info(self, symbol: str) -> dict:
        """获取交易对信息（包括 stepSize 等精度参数），带缓存"""
        symbol = symbol.upper()
        
        # 检查缓存
        with BinanceFuturesClient._cache_lock:
            if symbol in BinanceFuturesClient._symbol_info_cache:
                return BinanceFuturesClient._symbol_info_cache[symbol]
        
        try:
            import requests
            from datetime import datetime, timezone
            
            # 获取交易对信息（不需要签名）
            url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.get(url, proxies=proxies, timeout=self.settings.binance_http_timeout)
            response.raise_for_status()
            data = response.json()
            
            # 查找目标交易对
            symbol_info = {}
            for s in data.get("symbols", []):
                if s.get("symbol") == symbol:
                    # 提取数量精度（stepSize）和价格精度（tickSize）
                    for f in s.get("filters", []):
                        if f.get("filterType") == "LOT_SIZE":
                            step_size = f.get("stepSize", "1")
                            symbol_info["stepSize"] = Decimal(step_size)
                        elif f.get("filterType") == "PRICE_FILTER":
                            tick_size = f.get("tickSize", "0.01")
                            symbol_info["tickSize"] = Decimal(tick_size)
                    break
            
            # 如果没找到，使用默认值
            if "stepSize" not in symbol_info:
                symbol_info["stepSize"] = Decimal("0.1")  # 默认值
            if "tickSize" not in symbol_info:
                symbol_info["tickSize"] = Decimal("0.01")  # 默认值
            
            # 更新缓存
            with BinanceFuturesClient._cache_lock:
                BinanceFuturesClient._symbol_info_cache[symbol] = symbol_info
            
            return symbol_info
        except Exception as exc:
            logger.warning("获取交易对信息失败 {}，使用默认精度: {}", symbol, exc)
            # 返回默认值
            default_info = {"stepSize": Decimal("0.1"), "tickSize": Decimal("0.01")}
            with BinanceFuturesClient._cache_lock:
                BinanceFuturesClient._symbol_info_cache[symbol] = default_info
            return default_info
    
    def get_position_mode(self) -> str:
        """获取账户持仓模式：ONE_WAY_MODE（单向）或 HEDGE_MODE（双向）"""
        try:
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                raise ValueError("API密钥未配置")
            
            url = "https://fapi.binance.com/fapi/v1/positionSide/dual"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            params = {"timestamp": timestamp}
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.get(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            response.raise_for_status()
            data = response.json()
            
            # 返回持仓模式：True 表示双向持仓（HEDGE_MODE），False 表示单向持仓（ONE_WAY_MODE）
            return "HEDGE_MODE" if data.get("dualSidePosition", False) else "ONE_WAY_MODE"
        except Exception as exc:
            logger.warning("获取持仓模式失败，默认使用单向持仓: {}", exc)
            return "ONE_WAY_MODE"  # 默认单向持仓

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """设置合约杠杆倍数"""
        try:
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            # 检查API密钥是否配置
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                raise ValueError("API密钥未配置")
            
            url = "https://fapi.binance.com/fapi/v1/leverage"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            params = {
                "symbol": symbol,
                "leverage": leverage,
                "timestamp": timestamp,
            }
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.post(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            
            if response.status_code == 401:
                error_msg = response.json().get("msg", "Unauthorized")
                logger.error("设置杠杆API认证失败: {} (code: {})", error_msg, response.status_code)
                raise ValueError(f"API认证失败: {error_msg}")
            
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network
            logger.error("设置杠杆失败 {}: {}", symbol, exc)
            raise

    def _make_signed_request(self, url: str, params: dict = None) -> dict:
        """通用的签名请求方法"""
        import requests
        import hmac
        import hashlib
        from urllib.parse import urlencode
        from datetime import datetime, timezone
        
        if params is None:
            params = {}
        
        # 使用 UTC 时间，而不是系统本地时间
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        params["timestamp"] = timestamp
        
        # 生成签名（币安 API 使用 SHA256）
        query_string = urlencode(params, doseq=True)
        signature = hmac.new(
            self.settings.binance_api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        params["signature"] = signature
        headers = {
            "X-MBX-APIKEY": self.settings.binance_api_key
        }
        
        # 使用代理
        proxies = None
        if self.settings.http_proxy or self.settings.https_proxy:
            proxies = {
                "http": self.settings.http_proxy or self.settings.https_proxy,
                "https": self.settings.https_proxy or self.settings.http_proxy,
            }
        
        response = requests.get(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
        
        # 检查响应
        if response.status_code == 401:
            error_msg = response.json().get("msg", "Unauthorized")
            logger.error("币安API认证失败: {} (code: {})", error_msg, response.status_code)
            raise ValueError(f"API认证失败: {error_msg}")
        
        response.raise_for_status()
        return response.json()

    def get_futures_balance(self) -> Decimal:
        """获取合约账户可用余额（availableBalance，带缓存）"""
        # 检查缓存
        with BinanceFuturesClient._cache_lock:
            if "futures" in BinanceFuturesClient._balance_cache:
                value, timestamp = BinanceFuturesClient._balance_cache["futures"]
                if time.time() - timestamp < self.settings.balance_cache_ttl:
                    return Decimal(str(value))
        
        try:
            # 检查API密钥是否配置
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                logger.warning("币安API密钥未配置，无法获取账户余额")
                raise ValueError("API密钥未配置")
            
            # 获取合约账户余额
            url = "https://fapi.binance.com/fapi/v2/balance"
            account = self._make_signed_request(url)
            
            if not account or not isinstance(account, list):
                logger.error("获取合约账户余额返回格式错误: {}", type(account))
                raise ValueError("合约账户余额API返回格式错误")
            
            total = Decimal("0")
            for item in account:
                if item.get("asset") == "USDT":
                    # 使用 availableBalance（可用余额）而不是 balance（总余额）
                    balance_str = item.get("availableBalance", item.get("balance", "0"))
                    total = Decimal(str(balance_str))
                    break
            
            # 更新缓存
            with BinanceFuturesClient._cache_lock:
                BinanceFuturesClient._balance_cache["futures"] = (float(total), time.time())
            
            return total
        except Exception as exc:
            logger.error("获取合约账户余额失败: {} (类型: {})", exc, type(exc).__name__)
            raise

    def get_futures_wallet_balance(self) -> Decimal:
        """获取合约账户的资金账户（钱包）USDT 余额"""
        try:
            # 检查API密钥是否配置
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                logger.warning("币安API密钥未配置，无法获取账户余额")
                raise ValueError("API密钥未配置")
            
            # 获取合约账户资金账户余额
            url = "https://fapi.binance.com/fapi/v2/balance"
            account = self._make_signed_request(url)
            
            if not account or not isinstance(account, list):
                logger.error("获取合约资金账户余额返回格式错误: {}", type(account))
                raise ValueError("合约资金账户余额API返回格式错误")
            
            total = Decimal("0")
            for item in account:
                if item.get("asset") == "USDT":
                    # 合约账户的资金账户余额在 walletBalance 字段
                    wallet_balance = item.get("walletBalance", item.get("balance", "0"))
                    total = Decimal(str(wallet_balance))
                    break
            
            return total
        except Exception as exc:
            logger.error("获取合约资金账户余额失败: {} (类型: {})", exc, type(exc).__name__)
            raise

    def get_spot_balance(self) -> Decimal:
        """获取现货账户（Spot）USDT 余额（带缓存）"""
        # 检查缓存
        with BinanceFuturesClient._cache_lock:
            if "spot" in BinanceFuturesClient._balance_cache:
                value, timestamp = BinanceFuturesClient._balance_cache["spot"]
                if time.time() - timestamp < self.settings.balance_cache_ttl:
                    return Decimal(str(value))
        
        try:
            # 检查API密钥是否配置
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                logger.warning("币安API密钥未配置，无法获取账户余额")
                raise ValueError("API密钥未配置")
            
            # 获取现货账户余额
            url = "https://api.binance.com/api/v3/account"
            account = self._make_signed_request(url)
            
            if not account or not isinstance(account, dict):
                logger.error("获取现货账户余额返回格式错误: {}", type(account))
                raise ValueError("现货账户余额API返回格式错误")
            
            balances = account.get("balances", [])
            total = Decimal("0")
            for item in balances:
                if item.get("asset") == "USDT":
                    free = Decimal(str(item.get("free", "0")))
                    locked = Decimal(str(item.get("locked", "0")))
                    total = free + locked
                    break
            
            # 更新缓存
            with BinanceFuturesClient._cache_lock:
                BinanceFuturesClient._balance_cache["spot"] = (float(total), time.time())
            
            return total
        except Exception as exc:
            logger.error("获取现货账户余额失败: {} (类型: {})", exc, type(exc).__name__)
            raise

    def get_margin_balance(self) -> Decimal:
        """获取杠杆账户（Margin）USDT 余额（带缓存）"""
        # 检查缓存
        with BinanceFuturesClient._cache_lock:
            if "margin" in BinanceFuturesClient._balance_cache:
                value, timestamp = BinanceFuturesClient._balance_cache["margin"]
                if time.time() - timestamp < self.settings.balance_cache_ttl:
                    return Decimal(str(value))
        
        try:
            # 检查API密钥是否配置
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                logger.warning("币安API密钥未配置，无法获取账户余额")
                raise ValueError("API密钥未配置")
            
            # 获取杠杆账户余额
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            url = "https://api.binance.com/sapi/v1/margin/account"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            params = {"timestamp": timestamp}
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.get(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            
            if response.status_code == 401:
                error_msg = response.json().get("msg", "Unauthorized")
                raise ValueError(f"API认证失败: {error_msg}")
            
            response.raise_for_status()
            account = response.json()
            
            # 杠杆账户返回的是 userAssets 数组
            user_assets = account.get("userAssets", [])
            total = Decimal("0")
            for item in user_assets:
                if item.get("asset") == "USDT":
                    free = Decimal(str(item.get("free", "0")))
                    locked = Decimal(str(item.get("locked", "0")))
                    borrowed = Decimal(str(item.get("borrowed", "0")))
                    interest = Decimal(str(item.get("interest", "0")))
                    # 净余额 = 可用 + 锁定 - 借款 - 利息
                    total = free + locked - borrowed - interest
                    break
            
            # 更新缓存
            with BinanceFuturesClient._cache_lock:
                BinanceFuturesClient._balance_cache["margin"] = (float(total), time.time())
            
            return total
        except Exception as exc:
            logger.error("获取杠杆账户余额失败: {} (类型: {})", exc, type(exc).__name__)
            raise

    def get_wallet_futures_balance(self) -> Decimal:
        """获取资金账户（Wallet）中分配给合约账户的 USDT 余额"""
        try:
            # 使用钱包API获取合约账户余额
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            url = "https://api.binance.com/sapi/v3/asset/getUserAsset"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            params = {"timestamp": timestamp, "asset": "USDT"}
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.post(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            
            if response.status_code == 401:
                error_msg = response.json().get("msg", "Unauthorized")
                raise ValueError(f"API认证失败: {error_msg}")
            
            response.raise_for_status()
            wallet_data = response.json()
            
            # 查找合约账户余额
            if isinstance(wallet_data, list):
                for item in wallet_data:
                    if item.get("asset") == "USDT":
                        # 查找合约账户的余额
                        futures_balance = Decimal(str(item.get("futures", "0")))
                        return futures_balance
            
            return Decimal("0")
        except Exception as exc:
            logger.error("获取资金账户合约余额失败: {} (类型: {})", exc, type(exc).__name__)
            raise

    def get_wallet_spot_balance(self) -> Decimal:
        """获取资金账户（Wallet）中分配给现货账户的 USDT 余额"""
        try:
            # 使用钱包API获取现货账户余额
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            url = "https://api.binance.com/sapi/v3/asset/getUserAsset"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            params = {"timestamp": timestamp, "asset": "USDT"}
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.post(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            
            if response.status_code == 401:
                error_msg = response.json().get("msg", "Unauthorized")
                raise ValueError(f"API认证失败: {error_msg}")
            
            response.raise_for_status()
            wallet_data = response.json()
            
            # 查找现货账户余额
            if isinstance(wallet_data, list):
                for item in wallet_data:
                    if item.get("asset") == "USDT":
                        # 查找现货账户的余额
                        spot_balance = Decimal(str(item.get("spot", "0")))
                        return spot_balance
            
            return Decimal("0")
        except Exception as exc:
            logger.error("获取资金账户现货余额失败: {} (类型: {})", exc, type(exc).__name__)
            raise

    def get_wallet_balance(self) -> Decimal:
        """获取币安钱包（Wallet）USDT 余额（使用 sapi）"""
        try:
            # 检查API密钥是否配置
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                logger.warning("币安API密钥未配置，无法获取账户余额")
                raise ValueError("API密钥未配置")
            
            # 使用钱包API获取余额
            # 注意：sapi 使用 POST 方法
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            url = "https://api.binance.com/sapi/v3/asset/getUserAsset"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            params = {"timestamp": timestamp, "asset": "USDT"}
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            # sapi 使用 POST 方法
            response = requests.post(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            
            if response.status_code == 401:
                error_msg = response.json().get("msg", "Unauthorized")
                logger.error("币安钱包API认证失败: {} (code: {})", error_msg, response.status_code)
                raise ValueError(f"API认证失败: {error_msg}")
            
            response.raise_for_status()
            wallet_data = response.json()
            
            # 钱包API返回的是数组
            # sapi/v3/asset/getUserAsset 返回的字段（根据币安官方文档）：
            # - free: 可用余额
            # - locked: 锁定余额（订单中）
            # - freeze: 冻结余额
            # - withdrawing: 提现中余额（不算在总资产里，因为正在提现）
            # - ipoable: IPO可用余额
            # 总资产 = free + locked + freeze + ipoable
            total = Decimal("0")
            if isinstance(wallet_data, list) and len(wallet_data) > 0:
                # 查找USDT
                for item in wallet_data:
                    if item.get("asset") == "USDT":
                        # 读取所有字段
                        free = Decimal(str(item.get("free", "0")))
                        locked = Decimal(str(item.get("locked", "0")))
                        freeze = Decimal(str(item.get("freeze", "0")))
                        withdrawing = Decimal(str(item.get("withdrawing", "0")))
                        ipoable = Decimal(str(item.get("ipoable", "0")))
                        
                        # 总资产 = 可用 + 锁定 + 冻结 + IPO可用
                        # withdrawing 不算在总资产里，因为正在提现中
                        total = free + locked + freeze + ipoable
                        
                        # 只在值发生变化时记录日志（避免频繁刷屏）
                        # 检查缓存中的旧值
                        old_total = None
                        with BinanceFuturesClient._cache_lock:
                            if "wallet" in BinanceFuturesClient._balance_cache:
                                old_total, _ = BinanceFuturesClient._balance_cache["wallet"]
                        
                        # 只在值变化时记录（或首次记录）
                        if old_total is None or abs(float(total) - old_total) > 0.001:
                            logger.debug(
                                "资金账户USDT详情: free={}, locked={}, freeze={}, withdrawing={}, ipoable={}, total={}",
                                free, locked, freeze, withdrawing, ipoable, total
                            )
                        break
                else:
                    # 如果没找到USDT，记录警告
                    logger.warning("资金账户API返回的数据中未找到USDT资产，返回的数据: {}", wallet_data)
            else:
                # 如果返回空数组，记录警告
                logger.warning("资金账户API返回空数组，可能账户中没有资产或API调用失败")
            
            # 更新缓存
            with BinanceFuturesClient._cache_lock:
                BinanceFuturesClient._balance_cache["wallet"] = (float(total), time.time())
            
            return total
                
        except Exception as exc:
            logger.error("获取钱包余额失败: {} (类型: {})", exc, type(exc).__name__)
            raise

    def get_spot_balances(self) -> dict:
        """获取资金账户（现货）所有非零余额"""
        try:
            # 检查API密钥是否配置
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                logger.warning("币安API密钥未配置，无法获取账户余额")
                raise ValueError("API密钥未配置")
            
            # 获取资金账户余额
            url = "https://api.binance.com/api/v3/account"
            account = self._make_signed_request(url)
            
            if not account or not isinstance(account, dict):
                logger.error("获取资金账户余额返回格式错误: {}", type(account))
                raise ValueError("资金账户余额API返回格式错误")
            
            balances = account.get("balances", [])
            result = {}
            for item in balances:
                asset = item.get("asset", "")
                free = Decimal(str(item.get("free", "0")))
                locked = Decimal(str(item.get("locked", "0")))
                total = free + locked
                
                # 只返回非零余额
                if total > 0:
                    result[asset] = {
                        "free": float(free),
                        "locked": float(locked),
                        "total": float(total),
                    }
            
            return result
        except Exception as exc:
            logger.error("获取资金账户余额失败: {} (类型: {})", exc, type(exc).__name__)
            raise

    def get_account_balance(self) -> Decimal:
        """获取合约账户余额（保持向后兼容）"""
        return self.get_futures_balance()

    def place_market_order(self, symbol: str, side: str, quantity: Decimal, reduce_only: bool = False) -> dict:
        """
        下市价单（直接使用 requests，避免 python-binance 库的 URL 拼接问题）
        
        Args:
            symbol: 交易对
            side: 方向 (BUY/SELL)
            quantity: 数量
            reduce_only: 是否只减仓（平仓时使用，避免需要额外保证金）
        """
        try:
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                raise ValueError("API密钥未配置")
            
            url = "https://fapi.binance.com/fapi/v1/order"
            
            # 确保数量精度正确（动态获取交易对的 stepSize）
            from decimal import Decimal, ROUND_DOWN
            quantity_decimal = Decimal(str(quantity))
            
            # 获取交易对的 stepSize（数量精度）
            symbol_info = self.get_symbol_info(symbol)
            step_size = symbol_info.get("stepSize", Decimal("0.1"))
            
            # 根据 stepSize 调整数量精度
            if step_size < 1:
                # 如果 stepSize < 1，需要向下取整到 stepSize 的倍数
                quantity_decimal = (quantity_decimal / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
            else:
                # 如果 stepSize >= 1，直接向下取整
                quantity_decimal = (quantity_decimal / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
            
            # 格式化数量字符串（去掉末尾的0）
            quantity_str = format(quantity_decimal, 'f').rstrip('0').rstrip('.')
            
            # 检查账户持仓模式，如果是双向持仓模式，需要指定 positionSide
            try:
                position_mode = self.get_position_mode()
                logger.debug("账户持仓模式: %s (symbol=%s, side=%s, reduce_only=%s)", position_mode, symbol, side, reduce_only)
            except Exception as exc:
                logger.warning("获取持仓模式失败，默认使用单向持仓: %s", exc)
                position_mode = "ONE_WAY_MODE"
            
            # 重要：在生成签名之前重新生成时间戳，避免因为前面的操作导致时间戳过期
            # 使用UTC时间生成时间戳，并添加recvWindow参数以允许更大的时间窗口
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            
            params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": quantity_str,
                "timestamp": timestamp,
                "recvWindow": 10000,  # 增加到10秒的时间窗口，避免时间戳过期
            }
            
            # 如果是双向持仓模式，需要指定 positionSide
            if position_mode == "HEDGE_MODE":
                # BUY 对应 LONG，SELL 对应 SHORT
                params["positionSide"] = "LONG" if side == "BUY" else "SHORT"
                
                # 在双向持仓模式下，如果是平仓操作，添加 reduceOnly 参数（避免需要额外保证金）
                # 注意：单向持仓模式（ONE_WAY_MODE）不支持 reduceOnly 参数，会报错
                if reduce_only:
                    params["reduceOnly"] = "true"
                    logger.info("平仓订单（双向持仓模式）: symbol=%s, side=%s, quantity=%s, reduceOnly=true", symbol, side, quantity_str)
            else:
                # 单向持仓模式下，币安会自动识别平仓操作（通过反向操作），不需要 reduceOnly 参数
                # 明确不添加 reduceOnly 参数，避免错误
                if reduce_only:
                    logger.info("平仓订单（单向持仓模式）: symbol=%s, side=%s, quantity=%s (单向模式，不添加reduceOnly参数)", symbol, side, quantity_str)
                # 确保在单向模式下不添加 reduceOnly
                if "reduceOnly" in params:
                    del params["reduceOnly"]
            
            # 生成签名（在构建完所有参数后）
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.post(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            
            if response.status_code != 200:
                try:
                    error_data = response.json()
                    error_msg = error_data.get("msg", f"HTTP {response.status_code}")
                    error_code = error_data.get("code", response.status_code)
                    logger.error("市价单失败 {}: {} (code: {})", symbol, error_msg, error_code)
                    raise ValueError(f"市价单失败: {error_msg} (code: {error_code})")
                except ValueError:
                    raise
                except Exception:
                    logger.error("市价单失败 {}: HTTP {} - {}", symbol, response.status_code, response.text)
                    response.raise_for_status()
            
            return response.json()
        except Exception as exc:
            logger.error("市价单失败 {}: {}", symbol, exc)
            raise

    def place_limit_order(
        self, 
        symbol: str, 
        side: str, 
        quantity: Decimal, 
        price: Decimal,
        time_in_force: str = "GTC"  # GTC: Good Till Cancel, IOC: Immediate or Cancel, FOK: Fill or Kill
    ) -> dict:
        """下限价单（直接使用 requests，避免 python-binance 库的 URL 拼接问题）"""
        try:
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                raise ValueError("API密钥未配置")
            
            url = "https://fapi.binance.com/fapi/v1/order"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            
            # 确保数量精度正确（动态获取交易对的 stepSize）
            from decimal import Decimal, ROUND_DOWN
            quantity_decimal = Decimal(str(quantity))
            
            # 获取交易对的 stepSize（数量精度）
            symbol_info = self.get_symbol_info(symbol)
            step_size = symbol_info.get("stepSize", Decimal("0.1"))
            
            # 根据 stepSize 调整数量精度
            if step_size < 1:
                quantity_decimal = (quantity_decimal / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
            else:
                quantity_decimal = (quantity_decimal / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
            
            # 格式化数量字符串
            quantity_str = format(quantity_decimal, 'f').rstrip('0').rstrip('.')
            
            # 确保价格精度正确（使用 tickSize）
            tick_size = symbol_info.get("tickSize", Decimal("0.01"))
            price_decimal = Decimal(str(price))
            if tick_size < 1:
                price_decimal = (price_decimal / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size
            else:
                price_decimal = (price_decimal / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size
            
            # 格式化价格字符串
            price_str = format(price_decimal, 'f').rstrip('0').rstrip('.')
            
            # 检查账户持仓模式
            position_mode = self.get_position_mode()
            params = {
                "symbol": symbol,
                "side": side,
                "type": "LIMIT",
                "timeInForce": time_in_force,
                "quantity": quantity_str,
                "price": price_str,
                "timestamp": timestamp,
            }
            
            # 如果是双向持仓模式，需要指定 positionSide
            if position_mode == "HEDGE_MODE":
                params["positionSide"] = "LONG" if side == "BUY" else "SHORT"
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.post(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            
            if response.status_code != 200:
                try:
                    error_data = response.json()
                    error_msg = error_data.get("msg", f"HTTP {response.status_code}")
                    error_code = error_data.get("code", response.status_code)
                    logger.error("限价单失败 {}: {} (code: {})", symbol, error_msg, error_code)
                    raise ValueError(f"限价单失败: {error_msg} (code: {error_code})")
                except ValueError:
                    raise
                except Exception:
                    logger.error("限价单失败 {}: HTTP {} - {}", symbol, response.status_code, response.text)
                    response.raise_for_status()
            
            return response.json()
        except Exception as exc:
            logger.error("限价单失败 {}: {}", symbol, exc)
            raise

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        """取消订单"""
        try:
            return self.client._request(
                "delete",
                "fapi/v1/order",
                signed=True,
                data={
                    "symbol": symbol,
                    "orderId": order_id,
                },
            )
        except Exception as exc:
            logger.error("取消订单失败 {}: {}", symbol, exc)
            raise

    def get_order_status(self, symbol: str, order_id: str) -> dict:
        """查询订单状态（直接使用 requests，避免 python-binance 库的 URL 拼接问题）"""
        try:
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                raise ValueError("API密钥未配置")
            
            url = "https://fapi.binance.com/fapi/v1/order"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            
            params = {
                "symbol": symbol,
                "orderId": order_id,
                "timestamp": timestamp,
            }
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.get(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            
            if response.status_code != 200:
                try:
                    error_data = response.json()
                    error_msg = error_data.get("msg", f"HTTP {response.status_code}")
                    error_code = error_data.get("code", response.status_code)
                    logger.error("查询订单状态失败 {}: {} (code: {})", symbol, error_msg, error_code)
                    raise ValueError(f"查询订单状态失败: {error_msg} (code: {error_code})")
                except ValueError:
                    raise
                except Exception:
                    logger.error("查询订单状态失败 {}: HTTP {} - {}", symbol, response.status_code, response.text)
                    response.raise_for_status()
            
            return response.json()
        except Exception as exc:
            logger.error("查询订单状态失败 {}: {}", symbol, exc)
            raise

    def get_mark_price(self, symbol: str) -> Decimal | None:
        """获取单个交易对的标记价格（优先使用WebSocket缓存，回退到HTTP API）"""
        symbol = symbol.upper()
        
        # 优先尝试从WebSocket缓存获取价格
        if self.settings.websocket_price_enabled:
            try:
                ws_service = get_websocket_price_service()
                price = ws_service.get_price(symbol)
                if price is not None:
                    return price
            except Exception as exc:
                logger.debug("从WebSocket获取价格失败 {}: {}", symbol, exc)
        
        # 回退到HTTP API（带缓存）
        # 检查HTTP缓存
        with BinanceFuturesClient._cache_lock:
            if symbol in BinanceFuturesClient._price_cache:
                price, timestamp = BinanceFuturesClient._price_cache[symbol]
                if time.time() - timestamp < self.settings.price_cache_ttl:
                    return price
        
        try:
            # 直接使用 requests 获取标记价格（避免 python-binance 库的 URL 拼接问题）
            import requests
            url = "https://fapi.binance.com/fapi/v1/premiumIndex"
            params = {"symbol": symbol}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.get(url, params=params, proxies=proxies, timeout=self.settings.binance_http_timeout)
            response.raise_for_status()
            data = response.json()
            price = Decimal(data.get("markPrice", "0"))
            
            # 更新缓存
            with BinanceFuturesClient._cache_lock:
                BinanceFuturesClient._price_cache[symbol] = (price, time.time())
            
            return price
        except Exception as exc:  # pragma: no cover - network
            logger.warning("获取标记价格失败 {}: {}", symbol, exc)
            return None
    
    def get_all_mark_prices(self) -> dict[str, Decimal]:
        """批量获取所有交易对的标记价格（带缓存）"""
        # 检查缓存
        with BinanceFuturesClient._cache_lock:
            if "all" in BinanceFuturesClient._all_prices_cache:
                prices, timestamp = BinanceFuturesClient._all_prices_cache["all"]
                if time.time() - timestamp < self.settings.price_cache_ttl:
                    return prices
        
        try:
            # 直接使用 requests 获取所有标记价格（避免 python-binance 库的 URL 拼接问题）
            import requests
            url = "https://fapi.binance.com/fapi/v1/premiumIndex"
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.get(url, params={}, proxies=proxies, timeout=self.settings.binance_http_timeout)
            response.raise_for_status()
            data = response.json()
            
            # 解析结果（可能是列表或单个对象）
            prices = {}
            if isinstance(data, list):
                for item in data:
                    symbol = item.get("symbol", "")
                    mark_price = item.get("markPrice", "0")
                    if symbol and mark_price:
                        prices[symbol] = Decimal(mark_price)
            elif isinstance(data, dict) and "symbol" in data:
                # 单个结果
                prices[data.get("symbol", "")] = Decimal(data.get("markPrice", "0"))
            
            # 更新缓存
            with BinanceFuturesClient._cache_lock:
                BinanceFuturesClient._all_prices_cache["all"] = (prices, time.time())
                # 同时更新单个价格缓存
                for symbol, price in prices.items():
                    BinanceFuturesClient._price_cache[symbol] = (price, time.time())
            
            return prices
        except Exception as exc:
            logger.warning("批量获取标记价格失败: {}", exc)
            return {}
    
    def get_mark_prices_batch(self, symbols: list[str]) -> dict[str, Decimal]:
        """批量获取指定交易对的标记价格（优先使用批量API）"""
        if not symbols:
            return {}
        
        # 如果请求的交易对很多，使用批量API获取所有价格
        if len(symbols) > 5:
            all_prices = self.get_all_mark_prices()
            return {symbol: all_prices.get(symbol, Decimal("0")) for symbol in symbols}
        
        # 少量交易对，并行获取
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        result = {}
        with ThreadPoolExecutor(max_workers=min(len(symbols), 10)) as executor:
            futures = {executor.submit(self.get_mark_price, symbol): symbol for symbol in symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    price = future.result()
                    if price:
                        result[symbol] = price
                except Exception:
                    pass
        
        return result
    
    def get_positions_from_binance(self) -> list[dict]:
        """
        从币安API获取所有实际持仓（包括非系统下单的持仓）
        返回格式: [
            {
                "symbol": "BTCUSDT",
                "positionSide": "LONG" or "SHORT" or "BOTH",
                "positionAmt": "1.5",  # 持仓数量（正数表示做多，负数表示做空）
                "entryPrice": "50000.0",  # 入场价格
                "markPrice": "51000.0",  # 标记价格
                "unRealizedProfit": "1500.0",  # 未实现盈亏
                "leverage": "5",  # 杠杆
                "updateTime": 1234567890000,  # 更新时间戳
            },
            ...
        ]
        """
        try:
            import requests
            import hmac
            import hashlib
            from urllib.parse import urlencode
            from datetime import datetime, timezone
            
            if not self.settings.binance_api_key or not self.settings.binance_api_secret:
                raise ValueError("API密钥未配置")
            
            # 使用 fapi/v2/positionRisk 获取所有持仓信息
            url = "https://fapi.binance.com/fapi/v2/positionRisk"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            params = {"timestamp": timestamp}
            
            query_string = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params["signature"] = signature
            headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
            
            proxies = None
            if self.settings.http_proxy or self.settings.https_proxy:
                proxies = {
                    "http": self.settings.http_proxy or self.settings.https_proxy,
                    "https": self.settings.https_proxy or self.settings.http_proxy,
                }
            
            response = requests.get(url, params=params, headers=headers, proxies=proxies, timeout=self.settings.binance_http_timeout)
            response.raise_for_status()
            data = response.json()
            
            # 过滤出有持仓的交易对（positionAmt != 0）
            positions = []
            for item in data:
                position_amt = Decimal(str(item.get("positionAmt", "0")))
                if abs(position_amt) > Decimal("0"):  # 有持仓
                    position_side = item.get("positionSide", "BOTH")
                    symbol = item.get("symbol", "")
                    
                    # 确定方向：positionAmt > 0 表示做多，< 0 表示做空
                    side = "BUY" if position_amt > 0 else "SELL"
                    
                    positions.append({
                        "symbol": symbol,
                        "side": side,
                        "position_side": position_side,  # LONG, SHORT, or BOTH
                        "position_amt": abs(position_amt),  # 持仓数量（绝对值）
                        "entry_price": Decimal(str(item.get("entryPrice", "0"))),
                        "mark_price": Decimal(str(item.get("markPrice", "0"))),
                        "unrealized_profit": Decimal(str(item.get("unRealizedProfit", "0"))),
                        "leverage": int(item.get("leverage", "1")),
                        "update_time": int(item.get("updateTime", 0)),
                    })
            
            return positions
        except Exception as exc:
            logger.error("从币安获取持仓失败: {}", exc)
            return []
