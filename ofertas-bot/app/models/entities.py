"""Initial relational model. Publication and coupon workflows are later phases."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UTCDateTime, utcnow
from app.models.enums import (
    Availability,
    CouponDiscountType,
    CouponValidationState,
    DestinationChannel,
    MonitoringJobStatus,
    OfferStatus,
    Platform,
    PriceAccess,
    PublicationStatus,
)


def state_enum(enum_class: type, name: str) -> Enum:
    """Portable enum values with database CHECK constraints on both databases."""
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


coupon_products = Table(
    "coupon_products",
    Base.metadata,
    Column("coupon_id", ForeignKey("coupons.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_coupon_products_product_id", "product_id"),
)


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_products_platform_external_id"),
        Index("ix_products_monitoring_enabled_priority", "monitoring_enabled", "priority"),
        Index("ix_products_platform", "platform"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[Platform] = mapped_column(state_enum(Platform, "product_platform"))
    external_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(500))
    original_url: Mapped[str] = mapped_column(String(2048))
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    priority: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    affiliate_link: Mapped[AffiliateLink | None] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    price_history: Mapped[list[ProductPriceHistory]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ProductPriceHistory.observed_at",
    )
    coupons: Mapped[list[Coupon]] = relationship(
        secondary=coupon_products, back_populates="eligible_products", passive_deletes=True
    )


class AffiliateLink(TimestampMixin, Base):
    __tablename__ = "affiliate_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), unique=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    source: Mapped[str] = mapped_column(
        String(100), default="manual_official", server_default="manual_official"
    )
    product: Mapped[Product] = relationship(back_populates="affiliate_link")


class ProductPriceHistory(Base):
    __tablename__ = "product_price_history"
    __table_args__ = (
        CheckConstraint(
            "current_price IS NULL OR current_price > 0", name="current_price_positive"
        ),
        CheckConstraint(
            "previous_price IS NULL OR previous_price > 0", name="previous_price_positive"
        ),
        Index("ix_product_price_history_product_id_observed_at", "product_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    previous_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="BRL", server_default="BRL")
    availability: Mapped[Availability] = mapped_column(
        state_enum(Availability, "price_availability"),
        default=Availability.UNKNOWN,
        server_default=Availability.UNKNOWN.value,
    )
    price_access: Mapped[PriceAccess] = mapped_column(
        state_enum(PriceAccess, "price_access"),
        default=PriceAccess.PUBLIC,
        server_default=PriceAccess.PUBLIC.value,
    )
    source: Mapped[str] = mapped_column(String(100))
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    installment_text: Mapped[str | None] = mapped_column(String(500))
    shipping_text: Mapped[str | None] = mapped_column(String(500))

    product: Mapped[Product] = relationship(back_populates="price_history")


class Coupon(TimestampMixin, Base):
    __tablename__ = "coupons"
    __table_args__ = (
        UniqueConstraint("platform", "code", name="uq_coupons_platform_code"),
        CheckConstraint("discount_value > 0", name="discount_positive"),
        CheckConstraint(
            "discount_type <> 'PERCENTAGE' OR discount_value <= 100", name="percentage_at_most_100"
        ),
        CheckConstraint("max_discount IS NULL OR max_discount > 0", name="max_discount_positive"),
        CheckConstraint(
            "minimum_purchase IS NULL OR minimum_purchase >= 0", name="minimum_purchase_nonnegative"
        ),
        Index("ix_coupons_validation_state_expires_at", "validation_state", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(100))
    platform: Mapped[Platform] = mapped_column(state_enum(Platform, "coupon_platform"))
    discount_type: Mapped[CouponDiscountType] = mapped_column(
        state_enum(CouponDiscountType, "coupon_discount_type")
    )
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    max_discount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    minimum_purchase: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    restrictions: Mapped[str | None] = mapped_column(Text)
    validation_state: Mapped[CouponValidationState] = mapped_column(
        state_enum(CouponValidationState, "coupon_validation_state"),
        default=CouponValidationState.UNVERIFIED,
        server_default=CouponValidationState.UNVERIFIED.value,
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    price_access: Mapped[PriceAccess] = mapped_column(
        state_enum(PriceAccess, "coupon_price_access"),
        default=PriceAccess.PUBLIC,
        server_default=PriceAccess.PUBLIC.value,
    )
    source: Mapped[str | None] = mapped_column(String(100))
    eligible_products: Mapped[list[Product]] = relationship(
        secondary=coupon_products, back_populates="coupons", passive_deletes=True
    )


class Offer(TimestampMixin, Base):
    __tablename__ = "offers"
    __table_args__ = (
        CheckConstraint("current_price > 0", name="current_price_positive"),
        CheckConstraint(
            "previous_price IS NULL OR previous_price > 0", name="previous_price_positive"
        ),
        CheckConstraint(
            "historical_price IS NULL OR historical_price > 0", name="historical_price_positive"
        ),
        CheckConstraint("final_price IS NULL OR final_price > 0", name="final_price_positive"),
        CheckConstraint(
            "discount_amount IS NULL OR discount_amount >= 0", name="discount_amount_nonnegative"
        ),
        CheckConstraint(
            "discount_percentage IS NULL OR (discount_percentage >= 0 AND discount_percentage <= 100)",
            name="discount_percentage_range",
        ),
        CheckConstraint("score >= 0", name="score_nonnegative"),
        Index("ix_offers_status_score", "status", "score"),
        Index("ix_offers_product_id", "product_id"),
        Index("ix_offers_price_history_id", "price_history_id"),
        Index("ix_offers_coupon_id", "coupon_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    price_history_id: Mapped[int] = mapped_column(
        ForeignKey("product_price_history.id", ondelete="RESTRICT")
    )
    coupon_id: Mapped[int | None] = mapped_column(ForeignKey("coupons.id", ondelete="SET NULL"))
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    previous_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    historical_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    final_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    score: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"), server_default="0")
    status: Mapped[OfferStatus] = mapped_column(
        state_enum(OfferStatus, "offer_status"),
        default=OfferStatus.PENDING_REVIEW,
        server_default=OfferStatus.PENDING_REVIEW.value,
    )
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PublicationDestination(TimestampMixin, Base):
    __tablename__ = "publication_destinations"
    __table_args__ = (
        UniqueConstraint(
            "channel", "external_id", name="uq_publication_destinations_channel_external_id"
        ),
        CheckConstraint("min_interval_seconds > 0", name="min_interval_positive"),
        CheckConstraint("max_messages_per_hour > 0", name="max_messages_positive"),
        Index("ix_publication_destinations_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    channel: Mapped[DestinationChannel] = mapped_column(
        state_enum(DestinationChannel, "destination_channel")
    )
    external_id: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    manual_approval_required: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    min_interval_seconds: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    max_messages_per_hour: Mapped[int] = mapped_column(Integer, default=20, server_default="20")


class PublicationLog(TimestampMixin, Base):
    __tablename__ = "publication_logs"
    __table_args__ = (
        UniqueConstraint(
            "offer_id",
            "destination_id",
            "attempt_number",
            name="uq_publication_logs_offer_destination_attempt",
        ),
        CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
        Index(
            "ix_publication_logs_product_destination_sent_at",
            "product_id",
            "destination_id",
            "sent_at",
        ),
        Index("ix_publication_logs_status_created_at", "status", "created_at"),
        Index("ix_publication_logs_destination_id", "destination_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="RESTRICT"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("publication_destinations.id", ondelete="RESTRICT")
    )
    status: Mapped[PublicationStatus] = mapped_column(
        state_enum(PublicationStatus, "publication_status"),
        default=PublicationStatus.PENDING,
        server_default=PublicationStatus.PENDING.value,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    message_id: Mapped[str | None] = mapped_column(String(200))
    message_text: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MonitoringJob(TimestampMixin, Base):
    __tablename__ = "monitoring_jobs"
    __table_args__ = (
        CheckConstraint("interval_seconds > 0", name="interval_positive"),
        CheckConstraint("failure_count >= 0", name="failure_count_nonnegative"),
        Index("ix_monitoring_jobs_enabled_next_run_at", "enabled", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    status: Mapped[MonitoringJobStatus] = mapped_column(
        state_enum(MonitoringJobStatus, "monitoring_job_status"),
        default=MonitoringJobStatus.IDLE,
        server_default=MonitoringJobStatus.IDLE.value,
    )
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)


class AppSetting(TimestampMixin, Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
