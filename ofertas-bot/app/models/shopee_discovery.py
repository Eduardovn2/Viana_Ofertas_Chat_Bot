"""Prepared discovery configuration and isolated offline execution evidence."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime


class ShopeeDiscoveryPolicy(TimestampMixin, Base):
    __tablename__ = "shopee_discovery_policies"
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), primary_key=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ShopeeDiscoveryRun(TimestampMixin, Base):
    __tablename__ = "shopee_discovery_runs"
    __table_args__ = (
        CheckConstraint("mode = 'OFFLINE'", name="offline_only"),
        CheckConstraint("requests >= 0 AND requests <= 2", name="bounded_requests"),
        CheckConstraint(
            "operation IN ('CONFIGURE','DISCOVER','MONITOR','EVALUATE','RECOVER')",
            name="operation_allowed",
        ),
        CheckConstraint(
            "status IN ('RESERVED','SUCCEEDED','FAILED','UNCERTAIN')", name="status_allowed"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="RESERVED")
    mode: Mapped[str] = mapped_column(String(7), default="OFFLINE")
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT")
    )
    specification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requests: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ShopeeDiscoveryFinding(TimestampMixin, Base):
    __tablename__ = "shopee_discovery_findings"
    __table_args__ = (
        UniqueConstraint("run_id", "page", "product_id", name="uq_shopee_discovery_finding_page"),
        CheckConstraint("page >= 1 AND page <= 2", name="bounded_page"),
        CheckConstraint(
            "disposition IN ('SELECTED','REVIEW','FILTERED','DUPLICATE')",
            name="disposition_allowed",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("shopee_discovery_runs.id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    history_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_price_history.id", ondelete="RESTRICT")
    )
    page: Mapped[int] = mapped_column()
    disposition: Mapped[str] = mapped_column(String(16))
    score: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    assessment: Mapped[dict[str, Any]] = mapped_column(JSON)


class ShopeeDiscoveryProduct(TimestampMixin, Base):
    __tablename__ = "shopee_discovery_products"
    __table_args__ = (
        CheckConstraint("state IN ('SELECTED','REVIEW','PAUSED')", name="state_allowed"),
        CheckConstraint(
            "manual_decision IS NULL OR manual_decision IN ('ACCEPT','REJECT')",
            name="manual_decision_allowed",
        ),
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    state: Mapped[str] = mapped_column(String(16))
    priority: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    next_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("shopee_collection_candidates.id", ondelete="RESTRICT")
    )
    manual_decision: Mapped[str | None] = mapped_column(String(10))
    manual_actor: Mapped[str | None] = mapped_column(String(100))
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
