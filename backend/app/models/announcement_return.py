import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class AnnouncementReturn(Base):
    __tablename__ = "announcement_returns_codex"
    __table_args__ = (UniqueConstraint("announcement_id", "window_label", name="uq_return_window_codex"),)

    id = Column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    announcement_id = Column(UUID(as_uuid=False), ForeignKey("announcements_codex.id", ondelete="CASCADE"), nullable=False)
    window_label = Column(String(16), nullable=False)
    window_seconds = Column(Numeric(18, 2), nullable=False)
    entry_price = Column(Numeric(32, 12), nullable=True)
    exit_price = Column(Numeric(32, 12), nullable=True)
    return_pct = Column(Numeric(18, 8), nullable=True)
    data_source = Column(String(16), nullable=False)
    computed_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    announcement = relationship("Announcement", backref="returns")
