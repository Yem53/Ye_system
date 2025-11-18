import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Enum, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.models.enums import AnnouncementStatus


def generate_uuid() -> str:
    return str(uuid.uuid4())


class Announcement(Base):
    __tablename__ = "announcements_codex"

    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    source_id = Column(String(255), unique=True, nullable=False)
    source = Column(String(50), nullable=False)
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    symbol = Column(String(64), nullable=True)
    listing_time = Column(DateTime(timezone=True), nullable=True)
    timezone_label = Column(String(64), nullable=True)
    status = Column(Enum(AnnouncementStatus), default=AnnouncementStatus.NEW, nullable=False)
    url = Column(String(1024), nullable=True)
    extra_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
