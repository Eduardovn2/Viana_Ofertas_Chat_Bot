"""Separate one-attempt administrative technical outbox; commercial data untouched."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

revision = "0006_telegram_technical"
down_revision = "0005_telegram_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_technical_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "destination_id",
            sa.Integer(),
            sa.ForeignKey("publication_destinations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "preview_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("preview_hash", sa.String(64), nullable=False),
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
                name="technical_publication_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("approved_by", sa.String(200)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("message_id", sa.String(200)),
        sa.Column("last_error_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "destination_id", "content_hash", name="uq_technical_destination_content"
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64 AND length(preview_hash) = 64", name="hash_lengths"
        ),
        sa.CheckConstraint("attempt_number IN (0, 1)", name="single_attempt"),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'APPROVED', 'EXPIRED') AND attempt_number = 0) OR (status IN ('SENDING', 'SENT', 'FAILED', 'UNCERTAIN') AND attempt_number = 1)",
            name="attempt_matches_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('APPROVED', 'SENDING', 'SENT', 'FAILED', 'UNCERTAIN') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="approval_required",
        ),
    )
    op.create_index(
        "ix_technical_destination_status",
        "telegram_technical_publications",
        ["destination_id", "status"],
    )
    op.create_table(
        "telegram_technical_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "publication_id",
            sa.Integer(),
            sa.ForeignKey("telegram_technical_publications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event", sa.String(50), nullable=False),
        sa.Column("actor", sa.String(200)),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_telegram_technical_audit_publication_id", "telegram_technical_audit", ["publication_id"]
    )


def downgrade():
    condition = "EXISTS (SELECT 1 FROM telegram_technical_publications) OR EXISTS (SELECT 1 FROM telegram_technical_audit)"
    message = "Cannot downgrade technical outbox without data loss; audit records exist."
    if context.is_offline_mode():
        if op.get_bind().dialect.name != "postgresql":
            raise RuntimeError("Offline technical downgrade requires PostgreSQL guards.")
        op.execute(f"DO $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{message}'; END IF; END $$")
    elif op.get_bind().execute(sa.text(f"SELECT 1 WHERE {condition}")).first() is not None:
        raise RuntimeError(message)
    op.drop_index(
        "ix_telegram_technical_audit_publication_id", table_name="telegram_technical_audit"
    )
    op.drop_table("telegram_technical_audit")
    op.drop_index("ix_technical_destination_status", table_name="telegram_technical_publications")
    op.drop_table("telegram_technical_publications")
