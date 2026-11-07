"""Administrative technical publications, never associated with commercial data."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime, utcnow
from app.models.entities import state_enum
from app.models.enums import TelegramPublicationStatus


class TelegramTechnicalPublication(TimestampMixin, Base):
    __tablename__ = "telegram_technical_publications"
    __table_args__ = (
        UniqueConstraint("destination_id", "content_hash", name="uq_technical_destination_content"),
        CheckConstraint(
            "length(content_hash) = 64 AND length(preview_hash) = 64", name="hash_lengths"
        ),
        CheckConstraint("attempt_number IN (0, 1)", name="single_attempt"),
        CheckConstraint(
            "(status IN ('PENDING', 'APPROVED', 'EXPIRED') AND attempt_number = 0) OR "
            "(status IN ('SENDING', 'SENT', 'FAILED', 'UNCERTAIN') AND attempt_number = 1)",
            name="attempt_matches_status",
        ),
        CheckConstraint(
            "status NOT IN ('APPROVED', 'SENDING', 'SENT', 'FAILED', 'UNCERTAIN') OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="approval_required",
        ),
        Index("ix_technical_destination_status", "destination_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("publication_destinations.id", ondelete="RESTRICT")
    )
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    preview_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    preview_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[TelegramPublicationStatus] = mapped_column(
        state_enum(TelegramPublicationStatus, "technical_publication_status"),
        default=TelegramPublicationStatus.PENDING,
        server_default="PENDING",
    )
    created_by: Mapped[str] = mapped_column(String(200))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempt_number: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    message_id: Mapped[str | None] = mapped_column(String(200))
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class TelegramTechnicalAudit(Base):
    __tablename__ = "telegram_technical_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_technical_publications.id", ondelete="RESTRICT"), index=True
    )
    event: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str | None] = mapped_column(String(200))
    preview_hash: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
