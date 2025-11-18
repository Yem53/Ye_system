from enum import Enum


class AnnouncementStatus(str, Enum):
    NEW = "new"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    EXECUTED = "executed"


class TradePlanStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    ACTIVE = "active"
    EXITED = "exited"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ManualPlanStatus(str, Enum):
    PENDING = "PENDING"
    ARMED = "ARMED"
    EXECUTING = "EXECUTING"  # 正在执行中，防止并发执行
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PositionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    LIQUIDATED = "LIQUIDATED"
