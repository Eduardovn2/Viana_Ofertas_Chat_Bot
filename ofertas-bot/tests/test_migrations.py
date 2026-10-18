"""Exercise the committed revision, rather than creating tables from model metadata."""

import os
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.core.config import get_settings
from app.models import (
    AffiliateLink,
    Availability,
    Base,
    Platform,
    Product,
    ProductPriceHistory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {
    "products",
    "affiliate_links",
    "product_price_history",
    "coupons",
    "coupon_products",
    "offers",
    "publication_destinations",
    "publication_logs",
    "monitoring_jobs",
    "app_settings",
}


@pytest.fixture
def migration_database(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        yield config, engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_initial_revision_matches_models_and_round_trips(migration_database):
    config, engine = migration_database
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES | {"alembic_version"}
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, Base.metadata) == []

    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES | {"alembic_version"}


def test_migrated_schema_preserves_decimal_utc_and_cascade(migration_database):
    _, engine = migration_database
    with Session(engine) as session:
        product = Product(
            platform=Platform.MERCADO_LIVRE,
            external_id="MLB3810190843",
            name="Produto simulado",
            original_url="https://produto.mercadolivre.com.br/MLB-3810190843",
        )
        product.affiliate_link = AffiliateLink(url="https://meli.la/1JQSATy")
        product.price_history.append(
            ProductPriceHistory(
                current_price=Decimal("1234.56"),
                previous_price=None,
                availability=Availability.AVAILABLE,
                source="simulated",
                is_simulated=True,
                observed_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
            )
        )
        session.add(product)
        session.commit()
        session.expire_all()
        snapshot = session.scalar(select(ProductPriceHistory))
        assert snapshot.current_price == Decimal("1234.56")
        assert snapshot.previous_price is None
        assert snapshot.observed_at.tzinfo is UTC
        assert snapshot.created_at.tzinfo is UTC
        product_id = product.id

    # SQL deletion proves the migration's database cascades are present.
    with engine.begin() as connection:
        connection.execute(Product.__table__.delete().where(Product.id == product_id))
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AffiliateLink)) == 0
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 0


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-0.01")])
def test_migrated_schema_rejects_nonpositive_prices(migration_database, price):
    _, engine = migration_database
    with Session(engine) as session:
        product = Product(
            platform=Platform.SHOPEE, name="Teste", original_url="https://shopee.com.br/teste"
        )
        session.add(product)
        session.flush()
        session.add(ProductPriceHistory(product_id=product.id, current_price=price, source="test"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_migrated_schema_rejects_duplicate_product_ids(migration_database):
    _, engine = migration_database
    with Session(engine) as session:
        session.add_all(
            [
                Product(
                    platform=Platform.MERCADO_LIVRE,
                    external_id="MLB1",
                    name="A",
                    original_url="https://example.com/a",
                ),
                Product(
                    platform=Platform.MERCADO_LIVRE,
                    external_id="MLB1",
                    name="B",
                    original_url="https://example.com/b",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_datetime_type_rejects_ambiguous_naive_timestamp(migration_database):
    _, engine = migration_database
    with Session(engine) as session:
        product = Product(
            platform=Platform.SHOPEE, name="Teste", original_url="https://shopee.com.br/teste"
        )
        session.add(product)
        session.flush()
        session.add(
            ProductPriceHistory(
                product_id=product.id,
                current_price=Decimal("1.00"),
                source="test",
                observed_at=datetime(2026, 10, 18, 12, 0),
            )
        )
        with pytest.raises(StatementError, match="timezone"):
            session.commit()
        session.rollback()


def test_initial_revision_compiles_for_postgresql(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test@localhost/test")
    get_settings.cache_clear()
    sql_output = StringIO()
    config = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=sql_output)
    try:
        command.upgrade(config, "head", sql=True)
    finally:
        get_settings.cache_clear()
    sql = sql_output.getvalue()
    assert "CREATE TABLE products" in sql
    assert "CREATE TABLE publication_logs" in sql
    assert "NUMERIC(12, 2)" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "CHECK" in sql


@pytest.mark.postgres
def test_initial_revision_on_isolated_postgresql_schema(monkeypatch):
    """Opt in with a dedicated *_test database; never migrate existing tables."""
    configured_url = os.getenv("TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("TEST_DATABASE_URL não configurada; integração PostgreSQL opcional")
    url = make_url(configured_url)
    if url.drivername != "postgresql+psycopg" or not (url.database or "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL deve usar postgresql+psycopg e banco com sufixo _test")

    schema_name = f"ofertas_bot_test_{uuid4().hex}"
    admin_engine = create_engine(url)
    isolated_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        schema_created = True
        # Preserve any existing connection options, then restrict all unqualified DDL.
        existing_options = str(url.query.get("options", ""))
        isolated_url = url.update_query_dict(
            {"options": f"{existing_options} -csearch_path={schema_name}".strip()}
        )
        monkeypatch.setenv("DATABASE_URL", isolated_url.render_as_string(hide_password=False))
        get_settings.cache_clear()
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        command.upgrade(config, "head")
        isolated_engine = create_engine(isolated_url)
        assert set(inspect(isolated_engine).get_table_names()) == EXPECTED_TABLES | {
            "alembic_version"
        }
        with isolated_engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "compare_server_default": True}
            )
            assert compare_metadata(context, Base.metadata) == []
        with Session(isolated_engine) as session:
            product = Product(
                platform=Platform.SHOPEE,
                name="Simulado",
                original_url="https://shopee.com.br/teste",
            )
            session.add(product)
            session.flush()
            session.add(
                ProductPriceHistory(
                    product_id=product.id,
                    current_price=Decimal("1234.56"),
                    source="simulated",
                    is_simulated=True,
                    observed_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
                )
            )
            session.commit()
            snapshot = session.scalar(select(ProductPriceHistory))
            assert snapshot.current_price == Decimal("1234.56")
            assert snapshot.observed_at.utcoffset().total_seconds() == 0
        command.downgrade(config, "base")
        assert set(inspect(isolated_engine).get_table_names()) == {"alembic_version"}
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
        get_settings.cache_clear()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
