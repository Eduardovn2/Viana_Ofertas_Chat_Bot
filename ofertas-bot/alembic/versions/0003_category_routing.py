"""Add category provenance and conservative destination routing preferences.

Revision ID: 0003_category_routing
Revises: 0002_simulated_pipeline
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_category_routing"
down_revision: str | Sequence[str] | None = "0002_simulated_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _batch_recreate() -> str:
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    op.add_column("products", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("products", sa.Column("classification_metadata", sa.JSON(), nullable=True))
    op.add_column(
        "products",
        sa.Column(
            "manual_category_override", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    with op.batch_alter_table("publication_destinations", recreate=_batch_recreate()) as batch:
        batch.add_column(
            sa.Column("category_mode", sa.String(12), server_default="unconfigured", nullable=False)
        )
        batch.create_check_constraint(
            op.f("ck_publication_destinations_destination_category_mode"),
            "category_mode IN ('unconfigured', 'all', 'selected')",
        )

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
        sa.UniqueConstraint("slug", name=op.f("uq_categories_slug")),
    )
    op.create_index("ix_categories_active", "categories", ["active"])
    op.create_table(
        "product_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(6), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("explanation", sa.String(500), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_categories")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_categories_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_product_categories_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "product_id", "category_id", name=op.f("uq_product_categories_product_category")
        ),
        sa.CheckConstraint(
            "source IN ('store', 'rule', 'manual')",
            name=op.f("ck_product_categories_category_source"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_product_categories_confidence_range"),
        ),
    )
    op.create_index("ix_product_categories_category_id", "product_categories", ["category_id"])
    op.create_index("ix_product_categories_source", "product_categories", ["source"])
    op.create_table(
        "destination_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_destination_categories")),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["publication_destinations.id"],
            name=op.f("fk_destination_categories_destination_id_publication_destinations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_destination_categories_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "destination_id",
            "category_id",
            name=op.f("uq_destination_categories_destination_category"),
        ),
    )
    op.create_index(
        "ix_destination_categories_category_id", "destination_categories", ["category_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_destination_categories_category_id", table_name="destination_categories")
    op.drop_table("destination_categories")
    op.drop_index("ix_product_categories_source", table_name="product_categories")
    op.drop_index("ix_product_categories_category_id", table_name="product_categories")
    op.drop_table("product_categories")
    op.drop_index("ix_categories_active", table_name="categories")
    op.drop_table("categories")
    with op.batch_alter_table("publication_destinations", recreate=_batch_recreate()) as batch:
        batch.drop_constraint(
            op.f("ck_publication_destinations_destination_category_mode"), type_="check"
        )
        batch.drop_column("category_mode")
    op.drop_column("products", "manual_category_override")
    op.drop_column("products", "classification_metadata")
    op.drop_column("products", "description")
