"""Add a strictly simulated queue and durable monitoring leases.

Revision ID: 0002_simulated_pipeline
Revises: 0001_initial_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "0002_simulated_pipeline"
down_revision: str | Sequence[str] | None = "0001_initial_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_OFFER_STATES = "'PENDING_REVIEW', 'READY', 'QUEUED', 'PUBLISHED', 'REJECTED', 'EXPIRED'"
QUEUE_STATES = "'PENDING', 'READY', 'PUBLISHED', 'FAILED', 'EXPIRED'"


def _preflight_upgrade() -> None:
    message = (
        "Cannot migrate duplicate offers for one price history; resolve duplicates explicitly."
    )
    if context.is_offline_mode():
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                "DO $$ BEGIN IF EXISTS (SELECT price_history_id FROM offers "
                "GROUP BY price_history_id HAVING COUNT(*) > 1) THEN "
                f"RAISE EXCEPTION '{message}'; END IF; END $$"
            )
        return
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT price_history_id FROM offers GROUP BY price_history_id "
                "HAVING COUNT(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(message)


def _preflight_downgrade() -> None:
    message = (
        "Cannot downgrade while offers use PENDING/FAILED or destinations use SIMULATED; "
        "resolve these phase-2 records explicitly first."
    )
    query = (
        "SELECT 1 FROM offers WHERE status IN ('PENDING', 'FAILED') "
        "UNION ALL SELECT 1 FROM publication_destinations WHERE channel = 'SIMULATED'"
    )
    if context.is_offline_mode():
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                f"DO $$ BEGIN IF EXISTS ({query}) THEN RAISE EXCEPTION '{message}'; END IF; END $$"
            )
        return
    if op.get_bind().execute(sa.text(query)).first() is not None:
        raise RuntimeError(message)


def _change_existing_tables(*, upgrading: bool) -> None:
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "auto"
    channels = "'TELEGRAM', 'WHATSAPP', 'SIMULATED'" if upgrading else "'TELEGRAM', 'WHATSAPP'"
    states = f"{LEGACY_OFFER_STATES}, 'PENDING', 'FAILED'" if upgrading else LEGACY_OFFER_STATES
    with op.batch_alter_table("publication_destinations", recreate=recreate) as batch:
        batch.drop_constraint(
            op.f("ck_publication_destinations_destination_channel"), type_="check"
        )
        batch.alter_column(
            "channel",
            existing_type=sa.String(8 if upgrading else 9),
            type_=sa.String(9 if upgrading else 8),
            existing_nullable=False,
        )
        batch.create_check_constraint(
            op.f("ck_publication_destinations_destination_channel"), f"channel IN ({channels})"
        )
    with op.batch_alter_table("offers", recreate=recreate) as batch:
        batch.drop_constraint(op.f("ck_offers_offer_status"), type_="check")
        batch.create_check_constraint(op.f("ck_offers_offer_status"), f"status IN ({states})")
        if upgrading:
            batch.create_unique_constraint(op.f("uq_offers_price_history_id"), ["price_history_id"])
        else:
            batch.drop_constraint(op.f("uq_offers_price_history_id"), type_="unique")


def upgrade() -> None:
    _preflight_upgrade()
    _change_existing_tables(upgrading=True)
    op.create_table(
        "monitoring_leases",
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_monitoring_leases")),
    )
    op.create_table(
        "publication_queue_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(9), server_default="PENDING", nullable=False),
        sa.Column("active_slot", sa.Integer(), server_default="1", nullable=True),
        sa.Column("is_simulated", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_queue_items")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            ondelete="RESTRICT",
            name=op.f("fk_publication_queue_items_product_id_products"),
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            ondelete="RESTRICT",
            name=op.f("fk_publication_queue_items_offer_id_offers"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["publication_destinations.id"],
            ondelete="RESTRICT",
            name=op.f("fk_publication_queue_items_destination_id_publication_destinations"),
        ),
        sa.UniqueConstraint(
            "product_id",
            "destination_id",
            "active_slot",
            name=op.f("uq_publication_queue_items_product_destination_active_slot"),
        ),
        sa.CheckConstraint(
            f"status IN ({QUEUE_STATES})", name=op.f("ck_publication_queue_items_queue_status")
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'READY') AND active_slot IS NOT NULL AND active_slot = 1) "
            "OR (status IN ('PUBLISHED', 'FAILED', 'EXPIRED') AND active_slot IS NULL)",
            name=op.f("ck_publication_queue_items_active_slot_matches_status"),
        ),
        sa.CheckConstraint(
            "is_simulated = true", name=op.f("ck_publication_queue_items_simulated_only")
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_publication_queue_items_attempts_nonnegative")
        ),
    )
    op.create_index(
        "ix_publication_queue_items_status_created_at",
        "publication_queue_items",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_publication_queue_items_product_destination_published_at",
        "publication_queue_items",
        ["product_id", "destination_id", "published_at"],
    )
    op.create_index("ix_publication_queue_items_offer_id", "publication_queue_items", ["offer_id"])
    op.create_index(
        "ix_publication_queue_items_destination_id", "publication_queue_items", ["destination_id"]
    )


def downgrade() -> None:
    # Validate before dropping any phase-2 data. States that cannot fit 0001 must
    # never be silently coerced or deleted by a schema downgrade.
    _preflight_downgrade()
    op.drop_index("ix_publication_queue_items_destination_id", table_name="publication_queue_items")
    op.drop_index("ix_publication_queue_items_offer_id", table_name="publication_queue_items")
    op.drop_index(
        "ix_publication_queue_items_product_destination_published_at",
        table_name="publication_queue_items",
    )
    op.drop_index(
        "ix_publication_queue_items_status_created_at", table_name="publication_queue_items"
    )
    op.drop_table("publication_queue_items")
    op.drop_table("monitoring_leases")
    _change_existing_tables(upgrading=False)
