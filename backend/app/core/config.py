"""集中管理所有环境变量，方便在 .env 中修改后被整个系统复用。"""

from functools import lru_cache

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings 会在首次加载时读取 .env/.env.example 中的变量。"""

    app_name: str = "Quant News Codex"
    database_url: str = Field(..., env="DATABASE_URL")
    binance_api_key: str = Field("", env="BINANCE_API_KEY")
    binance_api_secret: str = Field("", env="BINANCE_API_SECRET")
    announcement_poll_interval: int = Field(90, description="轮询币安公告的时间间隔（秒）")
    approval_required: bool = Field(True, description="是否启用人工审核流程")
    manual_plan_check_interval: float = Field(
        0.3,
        description="手动计划检查间隔（秒），默认0.3秒。值过低会在运行时被强制提升以避免调度器实例堆积。",
    )
    manual_plan_precision_threshold: float = Field(60.0, description="精确执行模式触发阈值（秒），当计划执行时间在N秒内时启用精确模式，默认60秒（1分钟）")
    manual_plan_precision_mode: bool = Field(True, description="是否启用精确执行模式（毫秒级精度），抢时间策略建议启用")
    leverage: int = Field(5, env="LEVERAGE", description="默认合约杠杆倍数")
    position_pct: float = Field(0.5, env="POSITION_PCT", description="默认使用可用保证金的比例")
    max_order_amount: float | None = Field(
        default=None,
        env="MAX_ORDER_AMOUNT",
        description="单笔订单最大购买金额（USDT），用于测试和风险控制，可选",
    )
    order_type: str = Field(
        "MARKET",
        env="ORDER_TYPE",
        description="订单类型：MARKET（市价单）或 LIMIT（限价单）。市价单成交快但可能有滑点，限价单可控制价格但可能不成交"
    )
    max_slippage_pct: float = Field(
        0.5,
        env="MAX_SLIPPAGE_PCT",
        description="最大滑点百分比（仅对市价单有效）。如果市价单成交价格与预期价格偏差超过此值，将记录警告。例如：0.5 表示最大允许 0.5% 的滑点"
    )
    limit_order_timeout_seconds: int = Field(
        30,
        env="LIMIT_ORDER_TIMEOUT_SECONDS",
        description="限价单超时时间（秒）。如果限价单在此时间内未成交，将自动取消并转为市价单（可选）"
    )
    trailing_exit_pct: float = Field(0.15, env="TRAILING_EXIT_PCT", description="滑动退出百分比（回撤幅度）")
    stop_loss_pct: float = Field(0.05, env="STOP_LOSS_PCT", description="入场价的止损百分比")
    report_recipient: str = Field("yezfm53@gmail.com", env="REPORT_RECIPIENT")
    smtp_host: str = Field("", env="SMTP_HOST")
    smtp_port: int | None = Field(None, env="SMTP_PORT")
    smtp_user: str = Field("", env="SMTP_USER")
    smtp_password: str = Field("", env="SMTP_PASSWORD")
    http_proxy: str | None = Field(default=None, env="HTTP_PROXY", description="访问外部 API 时使用的 HTTP 代理，可选")
    https_proxy: str | None = Field(default=None, env="HTTPS_PROXY", description="访问外部 API 时使用的 HTTPS 代理，可选（通常与 HTTP_PROXY 相同）")
    dashboard_refresh_interval: int = Field(10, description="仪表盘自动刷新接口的间隔（秒）")
    analysis_window_seconds: int = Field(900, description="历史收益分析窗口（秒）")
    analysis_windows: list[str] = Field(
        default_factory=lambda: [
            "5m",
            "10m",
            "30m",
            "50m",
            "6h",
            "12h",
            "24h",
            "36h",
            "48h",
            "72h",
            "96h",
            "120h",
            "144h",
        ],
        description="需要计算收益的时间窗口",
    )
    binance_http_timeout: int = Field(5, description="访问币安 API 的超时时间（秒）")
    binance_max_retries: int = Field(3, description="访问币安 API 的最大重试次数")
    binance_retry_backoff: float = Field(0.5, description="币安 API 重试的初始退避时间（秒），指数退避")
    binance_rest_fail_threshold: int = Field(5, description="连续 REST 失败次数阈值，超过后触发降级日志")
    binance_rest_fail_cooldown: float = Field(10.0, description="连续失败后再次记录警告的冷却时间（秒）")
    price_cache_ttl: float = Field(1.0, description="价格缓存时间（秒），默认1秒")
    balance_cache_ttl: float = Field(2.0, description="余额缓存时间（秒），默认2秒")
    websocket_price_enabled: bool = Field(True, description="是否启用WebSocket价格订阅服务，启用后可大幅降低价格获取延迟")
    websocket_price_symbols: str | None = Field(None, env="WEBSOCKET_PRICE_SYMBOLS", description="WebSocket订阅的交易对列表（逗号分隔），如 'BTCUSDT,ETHUSDT'，如果为空则使用默认列表")
    websocket_subscribe_before_minutes: float = Field(5.0, description="在执行交易前多少分钟开始订阅WebSocket价格（默认5分钟）")
    terminal_log_level: str = Field("INFO", env="TERMINAL_LOG_LEVEL", description="终端日志最低级别（INFO/DEBUG/WARNING等）")
    terminal_key_events_only: bool = Field(True, env="TERMINAL_KEY_EVENTS_ONLY", description="是否只在终端输出关键事件（仍会显示WARNING及以上）")
    file_log_level: str = Field("DEBUG", env="FILE_LOG_LEVEL", description="写入日志文件的最低级别")
    file_log_rotation: str = Field("1 day", env="FILE_LOG_ROTATION", description="日志文件轮转策略，例如 '1 day' 或 '100 MB'")
    file_log_retention: str = Field("7 days", env="FILE_LOG_RETENTION", description="日志文件的保留时间或数量，例如 '7 days' 或 '10 files'")

    alpha_feed_url: HttpUrl | str = Field(
        "https://www.binance.com/bapi/apex/v1/public/apex/announcement/getLatestList",
        description="币安 Alpha 公告接口地址",
    )
    futures_feed_url: HttpUrl | str = Field(
        "https://www.binance.com/bapi/contracts/v1/public/cms/announcement/list",
        description="币安合约公告接口地址",
    )
    # 币安日本公告接口（可选，如果配置了则同时抓取）
    alpha_feed_url_jp: HttpUrl | str | None = Field(
        default=None,
        env="ALPHA_FEED_URL_JP",
        description="币安日本 Alpha 公告接口地址（可选）",
    )
    futures_feed_url_jp: HttpUrl | str | None = Field(
        default=None,
        env="FUTURES_FEED_URL_JP",
        description="币安日本合约公告接口地址（可选）",
    )
    futures_mark_price_url: HttpUrl | str = Field(
        "https://fapi.binance.com/fapi/v1/markPriceKlines",
        description="币安合约 1 秒 K 线接口",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_empty_values(cls, values: dict):
        """把 .env 中的空字符串转为 None，避免类型校验报错。"""

        if isinstance(values, dict):
            for key in ("smtp_port",):
                if values.get(key) == "":
                    values[key] = None
        return values

    @field_validator("analysis_windows", mode="before")
    @classmethod
    def parse_windows(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    class Config:
        # 从项目根目录读取 .env 文件（而不是当前工作目录）
        # 计算项目根目录：从当前文件位置向上查找
        from pathlib import Path
        _current_file = Path(__file__).resolve()
        # 从 backend/app/core/config.py 向上3级到项目根目录
        _project_root = _current_file.parent.parent.parent.parent
        env_file = str(_project_root / ".env")
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
