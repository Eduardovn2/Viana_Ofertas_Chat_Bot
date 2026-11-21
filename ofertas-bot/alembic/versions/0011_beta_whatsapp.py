"""Beta mode: per-post WhatsApp delivery status; additive columns only."""

import sqlalchemy as sa

from alembic import op

revision = "0011_beta_whatsapp"
down_revision = "0010_beta_posts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("beta_posts", sa.Column("whatsapp_status", sa.String(length=20), nullable=True))
    op.add_column(
        "beta_posts", sa.Column("whatsapp_message_id", sa.String(length=128), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("beta_posts", "whatsapp_message_id")
    op.drop_column("beta_posts", "whatsapp_status")
