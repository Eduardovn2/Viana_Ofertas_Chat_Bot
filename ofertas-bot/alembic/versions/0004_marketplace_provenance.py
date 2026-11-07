"""Preserve marketplace provenance, variants and non-calculable coupon evidence.

Revision ID: 0004_marketplace_provenance
Revises: 0003_category_routing

Downgrade deliberately refuses variants, new access states, nonempty provenance,
coupon evidence, integration events or dry-run diagnostics that 0003 cannot keep.
Export or explicitly resolve those records before a downgrade; this revision
never deletes them or coerces a restricted/unknown price into a public price.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

revision: str = "0004_marketplace_provenance"
down_revision: str | Sequence[str] | None = "0003_category_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_TYPES = ("OFFICIAL_API", "MANUAL", "SIMULATED", "UNKNOWN")
PRICE_ACCESS = "'PUBLIC', 'EXCLUSIVE', 'CONDITIONAL', 'UNKNOWN'"
LEGACY_PRICE_ACCESS = "'PUBLIC', 'EXCLUSIVE'"


def _recreate() -> str:
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=op.f(name), native_enum=False, create_constraint=True)


def _metadata_type():
    # PostgreSQL JSONB supports semantic default comparison in Alembic; SQLite
    # uses its portable JSON representation. No raw provider response is stored.
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _preflight_downgrade() -> None:
    checks = (
        "EXISTS (SELECT 1 FROM products WHERE variant_id <> '')",
        "EXISTS (SELECT 1 FROM product_price_history "
        "WHERE price_access NOT IN ('PUBLIC', 'EXCLUSIVE') "
        "OR source_type NOT IN ('UNKNOWN', 'SIMULATED') "
        "OR (source_type = 'SIMULATED' AND is_simulated = false) "
        "OR source_reference IS NOT NULL OR raw_fingerprint IS NOT NULL "
        "OR CAST(evidence_metadata AS TEXT) <> '{}')",
        "EXISTS (SELECT 1 FROM coupons WHERE price_access NOT IN ('PUBLIC', 'EXCLUSIVE'))",
        "EXISTS (SELECT 1 FROM publication_queue_items "
        "WHERE next_eligible_at IS NOT NULL OR blocked_reason IS NOT NULL)",
        "EXISTS (SELECT 1 FROM coupon_evidence)",
        "EXISTS (SELECT 1 FROM integration_events)",
    )
    condition = " OR ".join(checks)
    message = (
        "Cannot downgrade marketplace provenance without data loss; "
        "export or explicitly resolve phase-3 variants, evidence, events and diagnostics first."
    )
    if context.is_offline_mode():
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                f"DO $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{message}'; END IF; END $$"
            )
        return
    if op.get_bind().execute(sa.text(f"SELECT 1 WHERE {condition}")).first() is not None:
        raise RuntimeError(message)


def _coupon_access(*, upgrading: bool) -> None:
    with op.batch_alter_table("coupons", recreate=_recreate()) as batch:
        batch.drop_constraint(op.f("ck_coupons_coupon_price_access"), type_="check")
        batch.alter_column(
            "price_access",
            existing_type=sa.String(9 if upgrading else 11),
            type_=sa.String(11 if upgrading else 9),
            existing_nullable=False,
        )
        batch.create_check_constraint(
            op.f("ck_coupons_coupon_price_access"),
            f"price_access IN ({PRICE_ACCESS if upgrading else LEGACY_PRICE_ACCESS})",
        )


def upgrade() -> None:
    with op.batch_alter_table("products", recreate=_recreate()) as batch:
        batch.add_column(sa.Column("variant_id", sa.String(100), server_default="", nullable=False))
        batch.drop_constraint(op.f("uq_products_platform_external_id"), type_="unique")
        batch.create_unique_constraint(
            op.f("uq_products_platform_external_id_variant_id"),
            ["platform", "external_id", "variant_id"],
        )

    with op.batch_alter_table("product_price_history", recreate=_recreate()) as batch:
        batch.drop_constraint(op.f("ck_product_price_history_price_access"), type_="check")
        batch.alter_column(
            "price_access", existing_type=sa.String(9), type_=sa.String(11), existing_nullable=False
        )
        batch.create_check_constraint(
            op.f("ck_product_price_history_price_access"), f"price_access IN ({PRICE_ACCESS})"
        )
        batch.add_column(
            sa.Column("source_type", sa.String(12), server_default="UNKNOWN", nullable=False)
        )
        batch.add_column(sa.Column("source_reference", sa.String(500), nullable=True))
        batch.add_column(
            sa.Column("evidence_metadata", _metadata_type(), server_default="{}", nullable=False)
        )
        batch.add_column(sa.Column("raw_fingerprint", sa.String(64), nullable=True))
        batch.create_check_constraint(
            op.f("ck_product_price_history_price_source_type"),
            "source_type IN ('OFFICIAL_API', 'MANUAL', 'SIMULATED', 'UNKNOWN')",
        )
        batch.create_check_constraint(
            op.f("ck_product_price_history_fingerprint_length"),
            "raw_fingerprint IS NULL OR length(raw_fingerprint) = 64",
        )
        batch.create_unique_constraint(
            op.f("uq_product_price_history_source_fingerprint_observed"),
            ["product_id", "source", "raw_fingerprint", "observed_at"],
        )
    op.execute(
        "UPDATE product_price_history SET source_type = 'SIMULATED' WHERE is_simulated = true"
    )
    _coupon_access(upgrading=True)
    op.add_column(
        "publication_queue_items",
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "publication_queue_items", sa.Column("blocked_reason", sa.String(100), nullable=True)
    )

    op.create_table(
        "coupon_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("coupon_id", sa.Integer(), nullable=True),
        sa.Column(
            "platform",
            _enum("MERCADO_LIVRE", "SHOPEE", name="ck_coupon_evidence_coupon_evidence_platform"),
            nullable=False,
        ),
        sa.Column("code", sa.String(100), nullable=True),
        sa.Column(
            "type",
            _enum(
                "PERCENT",
                "FIXED",
                "FREE_SHIPPING",
                "UNKNOWN",
                name="ck_coupon_evidence_coupon_evidence_type",
            ),
            server_default="UNKNOWN",
            nullable=False,
        ),
        sa.Column("value", sa.Numeric(12, 2), nullable=True),
        sa.Column("minimum_purchase", sa.Numeric(12, 2), nullable=True),
        sa.Column("max_discount", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "validation_state",
            _enum(
                "CONFIRMED",
                "UNVERIFIED",
                "INVALID",
                name="ck_coupon_evidence_coupon_evidence_validation_state",
            ),
            server_default="UNVERIFIED",
            nullable=False,
        ),
        sa.Column(
            "price_access",
            _enum(
                "PUBLIC",
                "EXCLUSIVE",
                "CONDITIONAL",
                "UNKNOWN",
                name="ck_coupon_evidence_coupon_evidence_price_access",
            ),
            server_default="UNKNOWN",
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source_type",
            _enum(*SOURCE_TYPES, name="ck_coupon_evidence_coupon_evidence_source_type"),
            server_default="UNKNOWN",
            nullable=False,
        ),
        sa.Column("source_reference", sa.String(500), nullable=True),
        sa.Column("conditions", _metadata_type(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coupon_evidence")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_coupon_evidence_product_id_products"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["coupon_id"],
            ["coupons.id"],
            name=op.f("fk_coupon_evidence_coupon_id_coupons"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "value IS NULL OR value > 0", name=op.f("ck_coupon_evidence_value_positive")
        ),
        sa.CheckConstraint(
            "type <> 'PERCENT' OR value IS NULL OR value <= 100",
            name=op.f("ck_coupon_evidence_percentage_at_most_100"),
        ),
        sa.CheckConstraint(
            "minimum_purchase IS NULL OR minimum_purchase >= 0",
            name=op.f("ck_coupon_evidence_minimum_purchase_nonnegative"),
        ),
        sa.CheckConstraint(
            "max_discount IS NULL OR max_discount > 0",
            name=op.f("ck_coupon_evidence_max_discount_positive"),
        ),
    )
    op.create_index(
        "ix_coupon_evidence_product_id_observed_at",
        "coupon_evidence",
        ["product_id", "observed_at"],
    )
    op.create_index(
        "ix_coupon_evidence_platform_validation_state",
        "coupon_evidence",
        ["platform", "validation_state"],
    )
    op.create_index("ix_coupon_evidence_coupon_id", "coupon_evidence", ["coupon_id"])
    op.create_table(
        "integration_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column(
            "platform",
            _enum(
                "MERCADO_LIVRE", "SHOPEE", name="ck_integration_events_integration_event_platform"
            ),
            nullable=False,
        ),
        sa.Column("operation", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_reference", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_events")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_integration_events_product_id_products"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_integration_events_platform_observed_at",
        "integration_events",
        ["platform", "observed_at"],
    )
    op.create_index(
        "ix_integration_events_product_id_observed_at",
        "integration_events",
        ["product_id", "observed_at"],
    )


def downgrade() -> None:
    _preflight_downgrade()
    op.drop_index("ix_integration_events_product_id_observed_at", table_name="integration_events")
    op.drop_index("ix_integration_events_platform_observed_at", table_name="integration_events")
    op.drop_table("integration_events")
    op.drop_index("ix_coupon_evidence_coupon_id", table_name="coupon_evidence")
    op.drop_index("ix_coupon_evidence_platform_validation_state", table_name="coupon_evidence")
    op.drop_index("ix_coupon_evidence_product_id_observed_at", table_name="coupon_evidence")
    op.drop_table("coupon_evidence")
    op.drop_column("publication_queue_items", "blocked_reason")
    op.drop_column("publication_queue_items", "next_eligible_at")
    _coupon_access(upgrading=False)
    with op.batch_alter_table("product_price_history", recreate=_recreate()) as batch:
        batch.drop_constraint(
            op.f("uq_product_price_history_source_fingerprint_observed"), type_="unique"
        )
        batch.drop_constraint(op.f("ck_product_price_history_price_source_type"), type_="check")
        batch.drop_constraint(op.f("ck_product_price_history_fingerprint_length"), type_="check")
        batch.drop_constraint(op.f("ck_product_price_history_price_access"), type_="check")
        batch.alter_column(
            "price_access", existing_type=sa.String(11), type_=sa.String(9), existing_nullable=False
        )
        batch.create_check_constraint(
            op.f("ck_product_price_history_price_access"),
            f"price_access IN ({LEGACY_PRICE_ACCESS})",
        )
        batch.drop_column("raw_fingerprint")
        batch.drop_column("evidence_metadata")
        batch.drop_column("source_reference")
        batch.drop_column("source_type")
    with op.batch_alter_table("products", recreate=_recreate()) as batch:
        batch.drop_constraint(op.f("uq_products_platform_external_id_variant_id"), type_="unique")
        batch.drop_column("variant_id")
        batch.create_unique_constraint(
            op.f("uq_products_platform_external_id"), ["platform", "external_id"]
        )
