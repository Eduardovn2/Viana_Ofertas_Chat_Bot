"""Add isolated simulation enrollment, filters and Telegram mock-delivery audit."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

revision = "0007_simulated_automation"
down_revision = "0006_telegram_technical"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _fk(name, table, *, primary_key=False, nullable=False):
    return sa.Column(
        name,
        sa.Integer(),
        sa.ForeignKey(table + ".id", ondelete="RESTRICT"),
        primary_key=primary_key,
        nullable=nullable,
    )


def upgrade():
    op.create_table(
        "simulation_products",
        _fk("product_id", "products", primary_key=True),
        sa.Column("prices", sa.JSON(), nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "simulation_policies",
        _fk("destination_id", "publication_destinations", primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("automatic_approval", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "product_ids",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "terms",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("max_price", sa.Numeric(12, 2)),
        sa.Column("min_discount", sa.Numeric(5, 2)),
        *_timestamps(),
        sa.CheckConstraint("max_price IS NULL OR max_price > 0", name="max_price_positive"),
        sa.CheckConstraint(
            "min_discount IS NULL OR (min_discount >= 0 AND min_discount <= 100)",
            name="discount_range",
        ),
    )
    op.create_table(
        "telegram_simulations",
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk("queue_item_id", "publication_queue_items"),
        _fk("product_id", "products"),
        _fk("offer_id", "offers"),
        _fk("destination_id", "publication_destinations"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("active_slot", sa.Integer(), server_default="1"),
        sa.Column("is_simulated", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "preview_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(200)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("simulated_at", sa.DateTime(timezone=True)),
        sa.Column("mock_message_id", sa.String(100)),
        sa.Column("error_code", sa.String(100)),
        *_timestamps(),
        sa.UniqueConstraint(
            "product_id",
            "destination_id",
            "active_slot",
            name="uq_simulation_product_destination_slot",
        ),
        sa.UniqueConstraint("queue_item_id", name="uq_simulation_queue_item"),
        sa.CheckConstraint("is_simulated = true", name="simulation_only"),
        sa.CheckConstraint("attempt_number IN (0,1)", name="single_attempt"),
        sa.CheckConstraint(
            "status IN ('PENDING','READY','DISPATCHING','SIMULATED','FAILED','UNCERTAIN','EXPIRED')",
            name="status_values",
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING','READY','DISPATCHING','UNCERTAIN') AND active_slot IS NOT NULL AND active_slot=1) OR (status IN ('SIMULATED','FAILED','EXPIRED') AND active_slot IS NULL)",
            name="slot_matches_status",
        ),
        sa.CheckConstraint("length(preview_hash)=64", name="hash_length"),
    )
    op.create_index(
        "ix_simulation_destination_claimed",
        "telegram_simulations",
        ["destination_id", "claimed_at"],
    )
    op.create_table(
        "simulation_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk("publication_id", "telegram_simulations", nullable=True),
        _fk("product_id", "products"),
        _fk("destination_id", "publication_destinations"),
        sa.Column("event", sa.String(50), nullable=False),
        sa.Column("reason", sa.String(100)),
        sa.Column("preview_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_simulation_audit_product_destination",
        "simulation_audit",
        ["product_id", "destination_id"],
    )


def downgrade():
    condition = " OR ".join(
        "EXISTS (SELECT 1 FROM " + table + ")"
        for table in (
            "simulation_products",
            "simulation_policies",
            "telegram_simulations",
            "simulation_audit",
        )
    )
    message = "Cannot downgrade simulated automation without data loss."
    if context.is_offline_mode():
        if op.get_bind().dialect.name != "postgresql":
            raise RuntimeError("Offline simulation downgrade requires PostgreSQL guards.")
        op.execute(f"DO $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{message}'; END IF; END $$")
    elif op.get_bind().execute(sa.text(f"SELECT 1 WHERE {condition}")).first() is not None:
        raise RuntimeError(message)
    op.drop_table("simulation_audit")
    op.drop_table("telegram_simulations")
    op.drop_table("simulation_policies")
    op.drop_table("simulation_products")
