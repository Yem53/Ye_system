from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.enums import TradePlanStatus
from app.models.trade_analysis import TradeAnalysis
from app.models.trade_plan import TradePlan


@dataclass
class SecondBar:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class HistoricalAnalyzer:
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

    def sync_pending(self) -> None:
        stmt = select(TradePlan).where(
            TradePlan.status.in_([TradePlanStatus.QUEUED, TradePlanStatus.ACTIVE, TradePlanStatus.EXITED])
        )
        for plan in self.db.scalars(stmt):
            if any(plan.analysis):
                continue
            if not plan.planned_entry_time:
                continue
            window_end = plan.planned_entry_time + self._window_delta
            bars = self.fetch_bars(plan, window_end)
            if not bars:
                continue
            analysis = self.compute_plan(plan, bars)
            self.db.add(analysis)
            self.db.commit()
            logger.info("已生成计划 %s 的历史收益分析", plan.id)

    @property
    def _window_delta(self):
        from datetime import timedelta

        return timedelta(seconds=self.settings.analysis_window_seconds)

    def fetch_bars(self, plan: TradePlan, window_end: datetime) -> list[SecondBar]:
        announcement = plan.announcement
        if not announcement or not announcement.symbol:
            return []
        start_ms = int(plan.planned_entry_time.replace(tzinfo=UTC).timestamp() * 1000)
        end_ms = int(window_end.replace(tzinfo=UTC).timestamp() * 1000)
        params = {
            "symbol": f"{announcement.symbol}USDT",
            "interval": "1s",
            "startTime": start_ms,
            "endTime": end_ms,
        }
        try:
            resp = self.client.get(str(self.settings.futures_mark_price_url), params=params)
            resp.raise_for_status()
        except Exception as exc:  # pragma: no cover
            logger.warning("无法获取秒级 K 线 %s: %s", announcement.symbol, exc)
            return []
        data = resp.json()
        bars: list[SecondBar] = []
        for item in data:
            bars.append(
                SecondBar(
                    open_time=datetime.fromtimestamp(item[0] / 1000, tz=UTC),
                    open=Decimal(item[1]),
                    high=Decimal(item[2]),
                    low=Decimal(item[3]),
                    close=Decimal(item[4]),
                )
            )
        return bars

    def compute_plan(self, plan: TradePlan, bars: list[SecondBar]) -> TradeAnalysis:
        entry = bars[0].open
        highest = entry
        lowest = entry
        trailing_threshold = Decimal(1) - Decimal(plan.trailing_exit_pct or self.settings.trailing_exit_pct)
        stop_threshold = Decimal(1) - Decimal(plan.stop_loss_pct or self.settings.stop_loss_pct)
        exit_price = entry

        for bar in bars:
            highest = max(highest, bar.high)
            lowest = min(lowest, bar.low)
            if bar.close <= highest * trailing_threshold:
                exit_price = highest * trailing_threshold
                break
            if bar.low <= entry * stop_threshold:
                exit_price = entry * stop_threshold
                break
            exit_price = bar.close

        pnl_pct = (exit_price - entry) / entry
        analysis = TradeAnalysis(
            trade_plan_id=plan.id,
            entry_price=entry,
            exit_price=exit_price,
            highest_price=highest,
            lowest_price=lowest,
            pnl_percent=pnl_pct,
            data_points=str(len(bars)),
            window_seconds=self.settings.analysis_window_seconds,
        )
        return analysis
