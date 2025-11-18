from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import AnnouncementStatus


class AnnouncementBase(BaseModel):
    source_id: str
    source: str
    title: str
    content: str
    symbol: str | None = None
    listing_time: datetime | None = None
    timezone_label: str | None = None
    url: str | None = None
    extra_metadata: dict[str, Any] | None = None


class AnnouncementCreate(AnnouncementBase):
    pass


class AnnouncementRead(AnnouncementBase):
    id: str
    status: AnnouncementStatus
    created_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)
