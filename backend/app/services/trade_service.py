from __future__ import annotations

from datetime import datetime

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.announcement import Announcement
from app.models.enums import AnnouncementStatus, TradePlanStatus
from app.models.trade_plan import TradePlan


class TradeService:
    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()

    def ensure_plan(self, announcement: Announcement) -> TradePlan:
        if not announcement.listing_time:
            raise ValueError("无法创建交易计划: 缺少上线时间")
        existing = next((plan for plan in announcement.trade_plans), None)
        if existing:
            return existing
        plan = TradePlan(
            announcement_id=announcement.id,
            leverage=self.settings.leverage,
            position_pct=self.settings.position_pct,
            trailing_exit_pct=self.settings.trailing_exit_pct,
            stop_loss_pct=self.settings.stop_loss_pct,
            planned_entry_time=announcement.listing_time,
            status=TradePlanStatus.QUEUED,
        )
        self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)
        logger.info("已创建交易计划 %s", plan.id)
        return plan

    def activate_plan(self, plan: TradePlan) -> TradePlan:
        plan.status = TradePlanStatus.ACTIVE
        plan.actual_entry_time = datetime.utcnow()
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def complete_plan(self, plan: TradePlan, success: bool = True) -> TradePlan:
        plan.status = TradePlanStatus.EXITED if success else TradePlanStatus.FAILED
        plan.exit_time = datetime.utcnow()
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def approve_announcement(self, announcement: Announcement) -> TradePlan:
        announcement.status = AnnouncementStatus.APPROVED
        self.db.commit()
        self.db.refresh(announcement)
        return self.ensure_plan(announcement)
