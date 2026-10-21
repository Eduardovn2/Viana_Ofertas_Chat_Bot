"""Add manual verification details and an independent Telegram publication outbox.

Revision ID: 0005_telegram_outbox
Revises: 0004_marketplace_provenance

Existing histories, offers, logs and the simulated-only queue are untouched.
Downgrade refuses to drop either table while it contains audit/publication data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

revision: str = "0005_telegram_outbox"
down_revision: str | Sequence[str] | None = "0004_marketplace_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_offer_details",
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("verified_by", sa.String(200), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("image_url", sa.String(2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("offer_id", name=op.f("pk_manual_offer_details")),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_manual_offer_details_offer_id_offers"),
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "telegram_publications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "SENDING",
                "SENT",
                "FAILED",
                "UNCERTAIN",
                "EXPIRED",
                "REJECTED",
                name=op.f("ck_telegram_publications_telegram_publication_status"),
                native_enum=False,
                create_constraint=True,
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("active_slot", sa.Integer(), server_default="1", nullable=True),
        sa.Column(
            "preview_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(200), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_number", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.String(200), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(200), nullable=True),
        sa.Column("resolution", sa.String(50), nullable=True),
        sa.Column("resolution_evidence", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_publications")),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_telegram_publications_offer_id_offers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_telegram_publications_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["publication_destinations.id"],
            name=op.f("fk_telegram_publications_destination_id_publication_destinations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "product_id",
            "destination_id",
            "active_slot",
            name=op.f("uq_telegram_publications_product_destination_active_slot"),
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'APPROVED', 'SENDING', 'UNCERTAIN') "
            "AND active_slot IS NOT NULL AND active_slot = 1) "
            "OR (status IN ('SENT', 'FAILED', 'EXPIRED', 'REJECTED') AND active_slot IS NULL)",
            name=op.f("ck_telegram_publications_active_slot_matches_status"),
        ),
        sa.CheckConstraint(
            "attempt_number >= 0", name=op.f("ck_telegram_publications_attempt_number_nonnegative")
        ),
        sa.CheckConstraint(
            "length(preview_hash) = 64", name=op.f("ck_telegram_publications_preview_hash_length")
        ),
    )
    op.create_index(
        "ix_telegram_publications_status_created_at",
        "telegram_publications",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_telegram_publications_destination_status_retry_at",
        "telegram_publications",
        ["destination_id", "status", "retry_at"],
    )
    op.create_index(
        "ix_telegram_publications_product_destination_sent_at",
        "telegram_publications",
        ["product_id", "destination_id", "sent_at"],
    )
    op.create_index("ix_telegram_publications_offer_id", "telegram_publications", ["offer_id"])


def _preflight_downgrade() -> None:
    condition = "EXISTS (SELECT 1 FROM manual_offer_details) OR EXISTS (SELECT 1 FROM telegram_publications)"
    message = "Cannot downgrade Telegram outbox without data loss; phase-4 audit and publication records still exist."
    if context.is_offline_mode():
        if op.get_bind().dialect.name != "postgresql":
            raise RuntimeError(
                "Offline Telegram outbox downgrade requires PostgreSQL data-loss guards."
            )
        op.execute(f"DO $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{message}'; END IF; END $$")
    elif op.get_bind().execute(sa.text(f"SELECT 1 WHERE {condition}")).first() is not None:
        raise RuntimeError(message)


def downgrade() -> None:
    _preflight_downgrade()
    op.drop_index("ix_telegram_publications_offer_id", table_name="telegram_publications")
    op.drop_index(
        "ix_telegram_publications_product_destination_sent_at", table_name="telegram_publications"
    )
    op.drop_index(
        "ix_telegram_publications_destination_status_retry_at", table_name="telegram_publications"
    )
    op.drop_index("ix_telegram_publications_status_created_at", table_name="telegram_publications")
    op.drop_table("telegram_publications")
    op.drop_table("manual_offer_details")
