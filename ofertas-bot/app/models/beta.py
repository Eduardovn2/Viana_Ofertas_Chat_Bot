"""Beta Shopee mode: one row per offer the beta worker published (or simulated)."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime

BETA_STATUSES = ("DRY_RUN", "SENDING", "SENT", "FAILED", "UNCERTAIN")


class BetaPost(TimestampMixin, Base):
    __tablename__ = "beta_posts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRY_RUN', 'SENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="status_valid",
        ),
        CheckConstraint("price_min > 0", name="price_positive"),
        Index("ix_beta_posts_item_created", "item_key", "created_at"),
        Index("ix_beta_posts_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    keyword: Mapped[str] = mapped_column(String(100))
    price_min: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_percent: Mapped[int] = mapped_column(Integer)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    sales: Mapped[int | None] = mapped_column(Integer)
    commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    score: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    offer_link: Mapped[str] = mapped_column(String(2048))
    image_url: Mapped[str | None] = mapped_column(String(2048))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    chat_id: Mapped[str | None] = mapped_column(String(32))
    telegram_message_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
