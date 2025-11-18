from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ManualPlanStatus
from app.models.manual_plan import ManualPlan


class ManualPlanService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, data: dict) -> ManualPlan:
        plan = ManualPlan(**data)
        self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def list_all(self) -> list[ManualPlan]:
        stmt = select(ManualPlan).order_by(ManualPlan.listing_time.asc())
        return list(self.db.scalars(stmt))

    def get_pending_plans(self) -> list[ManualPlan]:
        """获取所有待执行的计划（包括未到时间的）"""
        stmt = (
            select(ManualPlan)
            .where(ManualPlan.status == ManualPlanStatus.PENDING)
            .order_by(ManualPlan.listing_time.asc())
        )
        return list(self.db.scalars(stmt))

    def due_plans(self) -> list[ManualPlan]:
        """获取已到执行时间的计划"""
        now = datetime.now(timezone.utc)
        stmt = (
            select(ManualPlan)
            .where(ManualPlan.status == ManualPlanStatus.PENDING)
            .where(ManualPlan.listing_time <= now)
            .order_by(ManualPlan.listing_time.asc())
        )
        return list(self.db.scalars(stmt))

    def mark_status(self, plan: ManualPlan, status: ManualPlanStatus) -> ManualPlan:
        plan.status = status
        self.db.commit()
        self.db.refresh(plan)
        logger.info("手动计划 %s -> %s", plan.id, status.value)
        return plan
