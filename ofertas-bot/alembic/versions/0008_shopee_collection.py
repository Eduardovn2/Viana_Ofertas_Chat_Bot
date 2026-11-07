"""Controlled Shopee collection audit and non-publishing candidates.

Revision ID: 0008_shopee_collection
Revises: 0007_simulated_automation
"""

import sqlalchemy as sa

from alembic import context, op

revision = "0008_shopee_collection"
down_revision = "0007_simulated_automation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shopee_collection_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("authorization", sa.String(300)),
        sa.Column("specification", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('OFFLINE', 'REAL')", name=op.f("ck_shopee_collection_runs_mode_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('RESERVED','SUCCEEDED','FAILED','UNCERTAIN')",
            name=op.f("ck_shopee_collection_runs_status_allowed"),
        ),
        sa.CheckConstraint(
            "requests >= 0 AND requests <= 1", name=op.f("ck_shopee_collection_runs_single_request")
        ),
    )
    op.create_table(
        "shopee_collection_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("shopee_collection_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "history_id",
            sa.Integer(),
            sa.ForeignKey("product_price_history.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("assessment", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "history_id", name="uq_shopee_candidate_run_history"),
        sa.CheckConstraint(
            "status IN ('INITIAL','FILTERED','UNVERIFIED','MANUAL_REVIEW','EXPIRED')",
            name=op.f("ck_shopee_collection_candidates_status_allowed"),
        ),
    )
    op.create_index(
        "ix_shopee_collection_candidates_run_id", "shopee_collection_candidates", ["run_id"]
    )


def downgrade():
    condition = "EXISTS (SELECT 1 FROM shopee_collection_runs) OR EXISTS (SELECT 1 FROM shopee_collection_candidates)"
    message = "Cannot downgrade Shopee collection without data loss."
    if context.is_offline_mode():
        if op.get_bind().dialect.name != "postgresql":
            raise RuntimeError("Offline downgrade requires PostgreSQL guards.")
        op.execute(f"DO $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{message}'; END IF; END $$")
    elif op.get_bind().execute(sa.text(f"SELECT 1 WHERE {condition}")).first() is not None:
        raise RuntimeError(message)
    op.drop_table("shopee_collection_candidates")
    op.drop_table("shopee_collection_runs")
