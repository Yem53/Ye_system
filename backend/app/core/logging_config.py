"""项目统一的 Loguru 配置，支持终端关键信息与文件全量日志分离。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from loguru import logger

from app.core.config import get_settings

_LOGGING_CONFIGURED = False


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _normalize_level(level_name: str | None, fallback: str) -> str:
    if not level_name:
        return fallback
    candidate = level_name.strip().upper()
    try:
        logger.level(candidate)
        return candidate
    except (ValueError, TypeError):
        return fallback


def _build_console_filter(min_level_no: int, key_events_only: bool) -> Callable[[dict], bool]:
    warning_no = logger.level("WARNING").no

    def _filter(record: dict) -> bool:
        if record["level"].no >= warning_no:
            return True
        if record["extra"].get("key_event"):
            return True
        if key_events_only:
            return False
        return record["level"].no >= min_level_no

    return _filter


def log_key_event(level: str, message: str, *args, **kwargs) -> None:
    """用于标记必须输出到终端的关键事件。"""

    normalized = _normalize_level(level, "INFO")
    if args and "%s" in message:
        try:
            message = message % args
            args = ()
        except Exception:
            pass
    logger.bind(key_event=True).log(normalized, message, *args, **kwargs)


def configure_logging() -> None:
    """配置 Loguru：终端只显示关键事件/高等级日志，文件保留完整内容。"""

    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    settings = get_settings()
    project_root = _project_root()
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    console_level = _normalize_level(settings.terminal_log_level, "INFO")
    file_level = _normalize_level(settings.file_log_level, "DEBUG")
    console_filter = _build_console_filter(
        logger.level(console_level).no,
        settings.terminal_key_events_only,
    )

    logger.remove()
    logger.add(
        sys.stdout,
        level=console_level,
        filter=console_filter,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        log_dir / "runtime.log",
        level=file_level,
        rotation=settings.file_log_rotation,
        retention=settings.file_log_retention,
        enqueue=True,
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
    )

    _LOGGING_CONFIGURED = True

