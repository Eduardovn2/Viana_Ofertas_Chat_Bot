"""Simulation-only enrollment, routing policy and audited Telegram dry-run outbox."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime, utcnow


class SimulationProduct(TimestampMixin, Base):
    __tablename__ = "simulation_products"
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    prices: Mapped[list[str | None]] = mapped_column(JSON)


class SimulationPolicy(TimestampMixin, Base):
    __tablename__ = "simulation_policies"
    __table_args__ = (
        CheckConstraint("max_price IS NULL OR max_price > 0", name="max_price_positive"),
        CheckConstraint(
            "min_discount IS NULL OR (min_discount >= 0 AND min_discount <= 100)",
            name="discount_range",
        ),
    )
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("publication_destinations.id", ondelete="RESTRICT"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    automatic_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    product_ids: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list, server_default="[]"
    )
    terms: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list, server_default="[]"
    )
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_discount: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))


class TelegramSimulation(TimestampMixin, Base):
    __tablename__ = "telegram_simulations"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "destination_id",
            "active_slot",
            name="uq_simulation_product_destination_slot",
        ),
        UniqueConstraint("queue_item_id", name="uq_simulation_queue_item"),
        CheckConstraint("is_simulated = true", name="simulation_only"),
        CheckConstraint("attempt_number IN (0,1)", name="single_attempt"),
        CheckConstraint(
            "status IN ('PENDING','READY','DISPATCHING','SIMULATED','FAILED','UNCERTAIN','EXPIRED')",
            name="status_values",
        ),
        CheckConstraint(
            "(status IN ('PENDING','READY','DISPATCHING','UNCERTAIN') AND active_slot IS NOT NULL AND active_slot=1) OR (status IN ('SIMULATED','FAILED','EXPIRED') AND active_slot IS NULL)",
            name="slot_matches_status",
        ),
        CheckConstraint("length(preview_hash)=64", name="hash_length"),
        Index("ix_simulation_destination_claimed", "destination_id", "claimed_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    queue_item_id: Mapped[int] = mapped_column(
        ForeignKey("publication_queue_items.id", ondelete="RESTRICT")
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="RESTRICT"))
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("publication_destinations.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20), default="PENDING", server_default="PENDING")
    active_slot: Mapped[int | None] = mapped_column(
        Integer().evaluates_none(), default=1, server_default="1"
    )
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    preview_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    preview_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempt_number: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    simulated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    mock_message_id: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))


class SimulationAudit(Base):
    __tablename__ = "simulation_audit"
    __table_args__ = (
        Index("ix_simulation_audit_product_destination", "product_id", "destination_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_simulations.id", ondelete="RESTRICT")
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("publication_destinations.id", ondelete="RESTRICT")
    )
    event: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str | None] = mapped_column(String(100))
    preview_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
