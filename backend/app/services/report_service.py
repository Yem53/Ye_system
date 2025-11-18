from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosmtplib
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.announcement import Announcement
from app.models.trade_plan import TradePlan


class DailyReporter:
    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()

    def build_report(self) -> str:
        now = datetime.now(UTC)
        start = datetime( now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=1)
        end = start + timedelta(days=1)
        announcements = list(self.db.scalars(select(Announcement).where(Announcement.created_at.between(start, end))))
        trades = list(self.db.scalars(select(TradePlan).where(TradePlan.created_at.between(start, end))))
        lines = [
            f"日报区间: {start.isoformat()} - {end.isoformat()}",
            "",
            f"新增公告: {len(announcements)} 条",
        ]
        for ann in announcements:
            lines.append(f"- {ann.title} ({ann.symbol or '未知'}) 状态: {ann.status.value}")
        lines.append("")
        lines.append(f"新建交易计划: {len(trades)} 条")
        for plan in trades:
            lines.append(
                f"- {plan.announcement.title if plan.announcement else plan.announcement_id} -> 状态 {plan.status.value}"
            )
        return "\n".join(lines)

    async def send_report(self, body: str) -> None:
        if not self.settings.smtp_host:
            logger.warning("SMTP 配置为空，跳过日报发送")
            return
        message = f"From: {self.settings.smtp_user}\nTo: {self.settings.report_recipient}\nSubject: Quant News 每日汇总\n\n{body}"
        await aiosmtplib.send(
            message,
            hostname=self.settings.smtp_host,
            port=self.settings.smtp_port or 587,
            username=self.settings.smtp_user or None,
            password=self.settings.smtp_password or None,
            start_tls=True,
        )

    async def build_and_send(self) -> None:
        body = self.build_report()
        await self.send_report(body)
