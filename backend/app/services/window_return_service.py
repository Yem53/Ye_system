from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.announcement import Announcement
from app.models.announcement_return import AnnouncementReturn


class WindowReturnService:
    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        # 使用代理配置以支持 VPN 连接
        proxies = None
        if self.settings.http_proxy:
            proxies = {
                "http": self.settings.http_proxy,
                "https": self.settings.http_proxy,
            }
        self.client = httpx.Client(
            timeout=self.settings.binance_http_timeout,
            proxies=proxies,
        )

    def run(self) -> None:
        stmt = select(Announcement).where(Announcement.symbol.isnot(None), Announcement.listing_time.isnot(None))
        for announcement in self.db.scalars(stmt):
            self.compute_for_announcement(announcement)

    def compute_for_announcement(self, announcement: Announcement) -> None:
        existing_labels = {ret.window_label for ret in announcement.returns}
        targets = [(label, self._window_seconds(label)) for label in self.settings.analysis_windows]
        pending = [item for item in targets if item[0] not in existing_labels]
        if not pending:
            return
        minute_bars = self._fetch_bars(announcement, interval="1m", duration=timedelta(minutes=60))
        hour_bars = self._fetch_bars(announcement, interval="1h", duration=timedelta(days=7))
        if not minute_bars and not hour_bars:
            return
        entry_price = self._entry_price(minute_bars or hour_bars)
        if entry_price is None:
            return
        for label, seconds in pending:
            price = self._price_at(seconds, minute_bars, hour_bars)
            if price is None:
                continue
            return_pct = (price - entry_price) / entry_price if entry_price else None
            record = AnnouncementReturn(
                announcement_id=announcement.id,
                window_label=label,
                window_seconds=seconds,
                entry_price=entry_price,
                exit_price=price,
                return_pct=return_pct,
                data_source="mark_price",
            )
            self.db.add(record)
        self.db.commit()

    def _fetch_bars(self, announcement: Announcement, interval: str, duration: timedelta):
        start = announcement.listing_time.astimezone(UTC)
        end = start + duration
        params = {
            "symbol": f"{announcement.symbol}USDT",
            "interval": interval,
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
            "limit": 1500,
        }
        try:
            resp = self.client.get(str(self.settings.futures_mark_price_url), params=params)
            resp.raise_for_status()
        except Exception as exc:  # pragma: no cover - network
            logger.warning("无法获取%s %s K线: %s", announcement.symbol, interval, exc)
            return []
        data = resp.json()
        bars = []
        for item in data:
            bars.append(
                {
                    "open_time": datetime.fromtimestamp(item[0] / 1000, tz=UTC),
                    "close": Decimal(item[4]),
                    "interval": interval,
                }
            )
        return bars

    def _entry_price(self, bars):
        if not bars:
            return None
        return bars[0]["close"]

    def _price_at(self, seconds: int, minute_bars, hour_bars):
        target_time = seconds
        source = minute_bars if seconds <= 3600 else hour_bars
        if not source:
            return None
        start_time = source[0]["open_time"]
        target_abs = start_time + timedelta(seconds=seconds)
        for bar in source:
            interval_seconds = 60 if bar["interval"] == "1m" else 3600
            if bar["open_time"] <= target_abs < bar["open_time"] + timedelta(seconds=interval_seconds):
                return bar["close"]
        return source[-1]["close"]

    def _window_seconds(self, label: str) -> int:
        unit = label[-1].lower()
        value = int(label[:-1])
        if unit == "m":
            return value * 60
        return value * 3600
