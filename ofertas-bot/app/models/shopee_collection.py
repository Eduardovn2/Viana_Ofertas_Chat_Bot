"""Audited, one-shot Shopee reads and non-publishing commercial assessments."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime


class ShopeeCollectionRun(TimestampMixin, Base):
    __tablename__ = "shopee_collection_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('OFFLINE', 'REAL')", name="mode_allowed"),
        CheckConstraint(
            "status IN ('RESERVED','SUCCEEDED','FAILED','UNCERTAIN')", name="status_allowed"
        ),
        CheckConstraint("requests >= 0 AND requests <= 1", name="single_request"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="RESERVED")
    requests: Mapped[int] = mapped_column(default=0)
    actor: Mapped[str] = mapped_column(String(100))
    authorization: Mapped[str | None] = mapped_column(String(300))
    specification: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ShopeeCollectionCandidate(TimestampMixin, Base):
    __tablename__ = "shopee_collection_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "history_id", name="uq_shopee_candidate_run_history"),
        CheckConstraint(
            "status IN ('INITIAL','FILTERED','UNVERIFIED','MANUAL_REVIEW','EXPIRED')",
            name="status_allowed",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("shopee_collection_runs.id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    history_id: Mapped[int] = mapped_column(
        ForeignKey("product_price_history.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20))
    assessment: Mapped[dict[str, Any]] = mapped_column(JSON)
