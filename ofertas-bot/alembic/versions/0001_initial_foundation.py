"""Create the initial product, price-history and future workflow foundation.

Revision ID: 0001_initial_foundation
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial_foundation"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=op.f(name), native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "platform",
            _enum("MERCADO_LIVRE", "SHOPEE", name="ck_products_product_platform"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(100), nullable=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("original_url", sa.String(2048), nullable=False),
        sa.Column("monitoring_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("priority", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint(
            "platform", "external_id", name=op.f("uq_products_platform_external_id")
        ),
    )
    op.create_index(
        "ix_products_monitoring_enabled_priority", "products", ["monitoring_enabled", "priority"]
    )
    op.create_index("ix_products_platform", "products", ["platform"])

    op.create_table(
        "affiliate_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("source", sa.String(100), server_default="manual_official", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_affiliate_links")),
        sa.UniqueConstraint("product_id", name=op.f("uq_affiliate_links_product_id")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_affiliate_links_product_id_products"),
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "product_price_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("current_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("previous_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(3), server_default="BRL", nullable=False),
        sa.Column(
            "availability",
            _enum(
                "AVAILABLE",
                "UNAVAILABLE",
                "UNKNOWN",
                name="ck_product_price_history_price_availability",
            ),
            server_default="UNKNOWN",
            nullable=False,
        ),
        sa.Column(
            "price_access",
            _enum("PUBLIC", "EXCLUSIVE", name="ck_product_price_history_price_access"),
            server_default="PUBLIC",
            nullable=False,
        ),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("is_simulated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("installment_text", sa.String(500), nullable=True),
        sa.Column("shipping_text", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_price_history")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_price_history_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "current_price IS NULL OR current_price > 0",
            name=op.f("ck_product_price_history_current_price_positive"),
        ),
        sa.CheckConstraint(
            "previous_price IS NULL OR previous_price > 0",
            name=op.f("ck_product_price_history_previous_price_positive"),
        ),
    )
    op.create_index(
        "ix_product_price_history_product_id_observed_at",
        "product_price_history",
        ["product_id", "observed_at"],
    )

    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column(
            "platform",
            _enum("MERCADO_LIVRE", "SHOPEE", name="ck_coupons_coupon_platform"),
            nullable=False,
        ),
        sa.Column(
            "discount_type",
            _enum("PERCENTAGE", "FIXED", name="ck_coupons_coupon_discount_type"),
            nullable=False,
        ),
        sa.Column("discount_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("max_discount", sa.Numeric(12, 2), nullable=True),
        sa.Column("minimum_purchase", sa.Numeric(12, 2), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restrictions", sa.Text(), nullable=True),
        sa.Column(
            "validation_state",
            _enum("CONFIRMED", "UNVERIFIED", "INVALID", name="ck_coupons_coupon_validation_state"),
            server_default="UNVERIFIED",
            nullable=False,
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "price_access",
            _enum("PUBLIC", "EXCLUSIVE", name="ck_coupons_coupon_price_access"),
            server_default="PUBLIC",
            nullable=False,
        ),
        sa.Column("source", sa.String(100), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coupons")),
        sa.UniqueConstraint("platform", "code", name=op.f("uq_coupons_platform_code")),
        sa.CheckConstraint("discount_value > 0", name=op.f("ck_coupons_discount_positive")),
        sa.CheckConstraint(
            "discount_type <> 'PERCENTAGE' OR discount_value <= 100",
            name=op.f("ck_coupons_percentage_at_most_100"),
        ),
        sa.CheckConstraint(
            "max_discount IS NULL OR max_discount > 0",
            name=op.f("ck_coupons_max_discount_positive"),
        ),
        sa.CheckConstraint(
            "minimum_purchase IS NULL OR minimum_purchase >= 0",
            name=op.f("ck_coupons_minimum_purchase_nonnegative"),
        ),
    )
    op.create_index(
        "ix_coupons_validation_state_expires_at", "coupons", ["validation_state", "expires_at"]
    )

    op.create_table(
        "coupon_products",
        sa.Column("coupon_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("coupon_id", "product_id", name=op.f("pk_coupon_products")),
        sa.ForeignKeyConstraint(
            ["coupon_id"],
            ["coupons.id"],
            name=op.f("fk_coupon_products_coupon_id_coupons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_coupon_products_product_id_products"),
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_coupon_products_product_id", "coupon_products", ["product_id"])

    op.create_table(
        "offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("price_history_id", sa.Integer(), nullable=False),
        sa.Column("coupon_id", sa.Integer(), nullable=True),
        sa.Column("current_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("previous_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("historical_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("discount_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("discount_percentage", sa.Numeric(5, 2), nullable=True),
        sa.Column("final_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("score", sa.Numeric(10, 2), server_default="0", nullable=False),
        sa.Column(
            "status",
            _enum(
                "PENDING_REVIEW",
                "READY",
                "QUEUED",
                "PUBLISHED",
                "REJECTED",
                "EXPIRED",
                name="ck_offers_offer_status",
            ),
            server_default="PENDING_REVIEW",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offers")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_offers_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_history_id"],
            ["product_price_history.id"],
            name=op.f("fk_offers_price_history_id_product_price_history"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["coupon_id"],
            ["coupons.id"],
            name=op.f("fk_offers_coupon_id_coupons"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("current_price > 0", name=op.f("ck_offers_current_price_positive")),
        sa.CheckConstraint(
            "previous_price IS NULL OR previous_price > 0",
            name=op.f("ck_offers_previous_price_positive"),
        ),
        sa.CheckConstraint(
            "historical_price IS NULL OR historical_price > 0",
            name=op.f("ck_offers_historical_price_positive"),
        ),
        sa.CheckConstraint(
            "final_price IS NULL OR final_price > 0", name=op.f("ck_offers_final_price_positive")
        ),
        sa.CheckConstraint(
            "discount_amount IS NULL OR discount_amount >= 0",
            name=op.f("ck_offers_discount_amount_nonnegative"),
        ),
        sa.CheckConstraint(
            "discount_percentage IS NULL OR (discount_percentage >= 0 AND discount_percentage <= 100)",
            name=op.f("ck_offers_discount_percentage_range"),
        ),
        sa.CheckConstraint("score >= 0", name=op.f("ck_offers_score_nonnegative")),
    )
    op.create_index("ix_offers_status_score", "offers", ["status", "score"])
    op.create_index("ix_offers_product_id", "offers", ["product_id"])
    op.create_index("ix_offers_price_history_id", "offers", ["price_history_id"])
    op.create_index("ix_offers_coupon_id", "offers", ["coupon_id"])

    op.create_table(
        "publication_destinations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "channel",
            _enum("TELEGRAM", "WHATSAPP", name="ck_publication_destinations_destination_channel"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "manual_approval_required", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("min_interval_seconds", sa.Integer(), server_default="60", nullable=False),
        sa.Column("max_messages_per_hour", sa.Integer(), server_default="20", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_destinations")),
        sa.UniqueConstraint(
            "channel", "external_id", name=op.f("uq_publication_destinations_channel_external_id")
        ),
        sa.CheckConstraint(
            "min_interval_seconds > 0",
            name=op.f("ck_publication_destinations_min_interval_positive"),
        ),
        sa.CheckConstraint(
            "max_messages_per_hour > 0",
            name=op.f("ck_publication_destinations_max_messages_positive"),
        ),
    )
    op.create_index("ix_publication_destinations_enabled", "publication_destinations", ["enabled"])

    op.create_table(
        "publication_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            _enum("PENDING", "SENT", "FAILED", name="ck_publication_logs_publication_status"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("message_id", sa.String(200), nullable=True),
        sa.Column("message_text", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_logs")),
        sa.UniqueConstraint(
            "offer_id",
            "destination_id",
            "attempt_number",
            name=op.f("uq_publication_logs_offer_destination_attempt"),
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_publication_logs_offer_id_offers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_publication_logs_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["publication_destinations.id"],
            name=op.f("fk_publication_logs_destination_id_publication_destinations"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name=op.f("ck_publication_logs_attempt_number_positive")
        ),
    )
    op.create_index(
        "ix_publication_logs_product_destination_sent_at",
        "publication_logs",
        ["product_id", "destination_id", "sent_at"],
    )
    op.create_index(
        "ix_publication_logs_status_created_at", "publication_logs", ["status", "created_at"]
    )
    op.create_index("ix_publication_logs_destination_id", "publication_logs", ["destination_id"])

    op.create_table(
        "monitoring_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), server_default="300", nullable=False),
        sa.Column(
            "status",
            _enum(
                "IDLE",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                name="ck_monitoring_jobs_monitoring_job_status",
            ),
            server_default="IDLE",
            nullable=False,
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitoring_jobs")),
        sa.UniqueConstraint("product_id", name=op.f("uq_monitoring_jobs_product_id")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_monitoring_jobs_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "interval_seconds > 0", name=op.f("ck_monitoring_jobs_interval_positive")
        ),
        sa.CheckConstraint(
            "failure_count >= 0", name=op.f("ck_monitoring_jobs_failure_count_nonnegative")
        ),
    )
    op.create_index(
        "ix_monitoring_jobs_enabled_next_run_at", "monitoring_jobs", ["enabled", "next_run_at"]
    )

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_settings")),
        sa.UniqueConstraint("key", name=op.f("uq_app_settings_key")),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index("ix_monitoring_jobs_enabled_next_run_at", table_name="monitoring_jobs")
    op.drop_table("monitoring_jobs")
    op.drop_index("ix_publication_logs_destination_id", table_name="publication_logs")
    op.drop_index("ix_publication_logs_status_created_at", table_name="publication_logs")
    op.drop_index("ix_publication_logs_product_destination_sent_at", table_name="publication_logs")
    op.drop_table("publication_logs")
    op.drop_index("ix_publication_destinations_enabled", table_name="publication_destinations")
    op.drop_table("publication_destinations")
    op.drop_index("ix_offers_coupon_id", table_name="offers")
    op.drop_index("ix_offers_price_history_id", table_name="offers")
    op.drop_index("ix_offers_product_id", table_name="offers")
    op.drop_index("ix_offers_status_score", table_name="offers")
    op.drop_table("offers")
    op.drop_index("ix_coupon_products_product_id", table_name="coupon_products")
    op.drop_table("coupon_products")
    op.drop_index("ix_coupons_validation_state_expires_at", table_name="coupons")
    op.drop_table("coupons")
    op.drop_index(
        "ix_product_price_history_product_id_observed_at", table_name="product_price_history"
    )
    op.drop_table("product_price_history")
    op.drop_table("affiliate_links")
    op.drop_index("ix_products_platform", table_name="products")
    op.drop_index("ix_products_monitoring_enabled_priority", table_name="products")
    op.drop_table("products")
