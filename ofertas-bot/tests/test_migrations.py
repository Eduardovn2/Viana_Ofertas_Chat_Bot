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
from sqlalchemy import MetaData, create_engine, event, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.core.config import get_settings
from app.models import (
    AffiliateLink,
    Availability,
    Base,
    Category,
    CategoryMode,
    CategorySource,
    CouponEvidence,
    CouponEvidenceType,
    DestinationCategory,
    DestinationChannel,
    IntegrationEvent,
    MonitoringLease,
    Offer,
    OfferStatus,
    Platform,
    PriceAccess,
    Product,
    ProductCategory,
    ProductPriceHistory,
    PublicationDestination,
    PublicationQueueItem,
    QueueStatus,
    SourceType,
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
    "monitoring_leases",
    "publication_queue_items",
    "categories",
    "product_categories",
    "destination_categories",
    "coupon_evidence",
    "integration_events",
}


@pytest.fixture
def migration_database(tmp_path, monkeypatch, request):
    database_url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, getattr(request, "param", "head"))
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
    assert "CREATE TABLE publication_queue_items" in sql
    assert "CREATE TABLE monitoring_leases" in sql
    assert "CREATE TABLE categories" in sql
    assert "CREATE TABLE product_categories" in sql
    assert "CREATE TABLE destination_categories" in sql
    assert "CREATE TABLE coupon_evidence" in sql
    assert "CREATE TABLE integration_events" in sql
    assert "FREE_SHIPPING" in sql
    assert "CONDITIONAL" in sql
    assert "JSONB" in sql
    assert "DEFAULT 'unconfigured' NOT NULL" in sql
    assert "SIMULATED" in sql
    assert "uq_offers_price_history_id" in sql
    assert "NUMERIC(12, 2)" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "CHECK" in sql


def _seed_legacy_data(engine):
    """Every phase-1 table receives data, including FKs to rebuilt parents."""
    metadata = MetaData()
    metadata.reflect(engine)
    tables = metadata.tables
    observed_at = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    timestamps = {"created_at": observed_at, "updated_at": observed_at}
    with engine.begin() as connection:

        def insert_id(table_name, **values):
            return connection.execute(
                tables[table_name].insert().values(**values)
            ).inserted_primary_key[0]

        product_id = insert_id(
            "products",
            platform="MERCADO_LIVRE",
            external_id="MLB3810190843",
            name="Produto legado",
            original_url="https://produto.mercadolivre.com.br/MLB-3810190843",
            **timestamps,
        )
        insert_id(
            "affiliate_links", product_id=product_id, url="https://meli.la/1JQSATy", **timestamps
        )
        history_id = insert_id(
            "product_price_history",
            product_id=product_id,
            current_price=Decimal("99.90"),
            source="simulated",
            is_simulated=True,
            observed_at=observed_at,
            created_at=observed_at,
        )
        coupon_id = insert_id(
            "coupons",
            platform="MERCADO_LIVRE",
            code="LEGACY",
            discount_type="FIXED",
            discount_value=Decimal("1.00"),
            **timestamps,
        )
        connection.execute(
            tables["coupon_products"].insert().values(product_id=product_id, coupon_id=coupon_id)
        )
        destination_id = insert_id(
            "publication_destinations",
            name="Destino legado",
            channel="TELEGRAM",
            external_id="legacy-test",
            **timestamps,
        )
        offer_id = insert_id(
            "offers",
            product_id=product_id,
            price_history_id=history_id,
            current_price=Decimal("99.90"),
            status="READY",
            **timestamps,
        )
        insert_id("monitoring_jobs", product_id=product_id, **timestamps)
        insert_id("app_settings", key="legacy", value={"phase": 1}, **timestamps)
        insert_id(
            "publication_logs",
            product_id=product_id,
            offer_id=offer_id,
            destination_id=destination_id,
            status="SENT",
            sent_at=observed_at,
            **timestamps,
        )
        return {
            "product_id": product_id,
            "offer_id": offer_id,
            "destination_id": destination_id,
            "price_history_id": history_id,
        }


def _assert_legacy_data_intact(engine, ids):
    metadata = MetaData()
    metadata.reflect(engine)
    tables = metadata.tables
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(tables["products"].c.name).where(
                    tables["products"].c.id == ids["product_id"]
                )
            )
            == "Produto legado"
        )
        assert (
            connection.scalar(
                select(tables["affiliate_links"].c.url).where(
                    tables["affiliate_links"].c.product_id == ids["product_id"]
                )
            )
            == "https://meli.la/1JQSATy"
        )
        assert connection.scalar(
            select(tables["product_price_history"].c.current_price).where(
                tables["product_price_history"].c.id == ids["price_history_id"]
            )
        ) == Decimal("99.90")
        assert (
            connection.scalar(
                select(tables["coupons"].c.code)
                .join(tables["coupon_products"])
                .where(tables["coupon_products"].c.product_id == ids["product_id"])
            )
            == "LEGACY"
        )
        assert (
            connection.scalar(
                select(tables["offers"].c.status).where(tables["offers"].c.id == ids["offer_id"])
            )
            == "READY"
        )
        assert (
            connection.scalar(
                select(tables["publication_destinations"].c.channel).where(
                    tables["publication_destinations"].c.id == ids["destination_id"]
                )
            )
            == "TELEGRAM"
        )
        assert connection.scalar(select(func.count()).select_from(tables["publication_logs"])) == 1
        assert connection.scalar(select(func.count()).select_from(tables["monitoring_jobs"])) == 1
        assert connection.scalar(select(tables["app_settings"].c.value)) == {"phase": 1}
        if connection.dialect.name == "sqlite":
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("migration_database", ["0001_initial_foundation"], indirect=True)
def test_phase2_migration_preserves_phase1_rows_and_foreign_keys(migration_database):
    config, engine = migration_database
    ids = _seed_legacy_data(engine)
    command.upgrade(config, "head")
    _assert_legacy_data_intact(engine, ids)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, Base.metadata) == []
    command.downgrade(config, "0001_initial_foundation")
    _assert_legacy_data_intact(engine, ids)
    command.upgrade(config, "head")
    _assert_legacy_data_intact(engine, ids)


@pytest.mark.parametrize("migration_database", ["0001_initial_foundation"], indirect=True)
def test_phase2_refuses_duplicate_observations_without_partial_upgrade(migration_database):
    config, engine = migration_database
    ids = _seed_legacy_data(engine)
    with Session(engine) as session:
        session.add(
            Offer(
                product_id=ids["product_id"],
                price_history_id=ids["price_history_id"],
                current_price=Decimal("99.90"),
                status=OfferStatus.READY,
            )
        )
        session.commit()
    with pytest.raises(RuntimeError, match="duplicate offers"):
        command.upgrade(config, "head")
    assert "publication_queue_items" not in inspect(engine).get_table_names()
    assert "monitoring_leases" not in inspect(engine).get_table_names()
    _assert_legacy_data_intact(engine, ids)
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0001_initial_foundation"
        )


@pytest.mark.parametrize("migration_database", ["0001_initial_foundation"], indirect=True)
def test_sqlite_rolls_back_rebuilt_parent_tables_if_migration_fails(migration_database):
    config, engine = migration_database
    ids = _seed_legacy_data(engine)

    def fail_after_parent_rebuild(connection, cursor, statement, parameters, context, executemany):
        if "CREATE TABLE monitoring_leases" in statement:
            raise RuntimeError("Injected migration failure after parent rebuild")

    event.listen(Engine, "before_cursor_execute", fail_after_parent_rebuild)
    try:
        with pytest.raises(RuntimeError, match="Injected migration failure"):
            command.upgrade(config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_parent_rebuild)
    _assert_legacy_data_intact(engine, ids)
    inspector = inspect(engine)
    assert "publication_queue_items" not in inspector.get_table_names()
    assert not any(name.startswith("_alembic_tmp_") for name in inspector.get_table_names())
    assert "uq_offers_price_history_id" not in {
        constraint["name"] for constraint in inspector.get_unique_constraints("offers")
    }
    channel = next(
        column
        for column in inspector.get_columns("publication_destinations")
        if column["name"] == "channel"
    )
    assert channel["type"].length == 8
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0001_initial_foundation"
        )


def test_queue_enforces_one_active_slot_and_allows_terminal_history(migration_database):
    _, engine = migration_database
    ids = _seed_legacy_data(engine)
    common = {key: ids[key] for key in ("product_id", "offer_id", "destination_id")}
    with Session(engine) as session:
        first = PublicationQueueItem(**common)
        session.add(first)
        session.commit()
        first_id = first.id
        session.add(PublicationQueueItem(**common, status=QueueStatus.READY))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        first = session.get(PublicationQueueItem, first_id)
        first.status = QueueStatus.PUBLISHED
        first.active_slot = None
        session.commit()
        session.add_all(
            [
                PublicationQueueItem(**common, status=QueueStatus.FAILED, active_slot=None),
                PublicationQueueItem(**common, status=QueueStatus.EXPIRED, active_slot=None),
                PublicationQueueItem(**common, status=QueueStatus.PENDING),
            ]
        )
        session.commit()
        assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 4


@pytest.mark.parametrize(
    "values",
    [
        {"status": QueueStatus.PENDING, "active_slot": None},
        {"status": QueueStatus.READY, "active_slot": 2},
        {"status": QueueStatus.PUBLISHED, "active_slot": 1},
        {"is_simulated": False},
        {"attempts": -1},
    ],
)
def test_queue_database_rejects_invalid_states(migration_database, values):
    _, engine = migration_database
    ids = _seed_legacy_data(engine)
    with Session(engine) as session:
        session.add(
            PublicationQueueItem(
                product_id=ids["product_id"],
                offer_id=ids["offer_id"],
                destination_id=ids["destination_id"],
                **values,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize("new_state", ["offer", "destination"])
def test_downgrade_refuses_phase2_states_without_dropping_queue(migration_database, new_state):
    config, engine = migration_database
    ids = _seed_legacy_data(engine)
    with Session(engine) as session:
        if new_state == "offer":
            session.get(Offer, ids["offer_id"]).status = OfferStatus.FAILED
        else:
            session.get(
                PublicationDestination, ids["destination_id"]
            ).channel = DestinationChannel.SIMULATED
        session.add(
            PublicationQueueItem(
                product_id=ids["product_id"],
                offer_id=ids["offer_id"],
                destination_id=ids["destination_id"],
            )
        )
        session.commit()
    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        command.downgrade(config, "0001_initial_foundation")
    assert "publication_queue_items" in inspect(engine).get_table_names()
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 1


@pytest.mark.parametrize("migration_database", ["0002_simulated_pipeline"], indirect=True)
def test_category_migration_preserves_phase2_rows_and_defaults_to_unconfigured(migration_database):
    config, engine = migration_database
    ids = _seed_legacy_data(engine)
    legacy_metadata = MetaData()
    legacy_metadata.reflect(engine)
    observed_at = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            legacy_metadata.tables["publication_queue_items"]
            .insert()
            .values(
                product_id=ids["product_id"],
                offer_id=ids["offer_id"],
                destination_id=ids["destination_id"],
                status="READY",
                created_at=observed_at,
                updated_at=observed_at,
            )
        )
    with Session(engine) as session:
        session.add_all(
            [
                MonitoringLease(
                    name="legacy-lease",
                    owner="legacy-owner",
                    expires_at=datetime(2026, 10, 18, 13, 0, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    command.upgrade(config, "head")
    _assert_legacy_data_intact(engine, ids)
    with Session(engine) as session:
        product = session.get(Product, ids["product_id"])
        destination = session.get(PublicationDestination, ids["destination_id"])
        assert product.description is None
        assert product.classification_metadata is None
        assert product.manual_category_override is False
        assert destination.category_mode == CategoryMode.UNCONFIGURED
        assert session.scalar(select(func.count()).select_from(Category)) == 0
        assert session.scalar(select(func.count()).select_from(ProductCategory)) == 0
        assert session.scalar(select(func.count()).select_from(DestinationCategory)) == 0
        assert session.scalar(select(PublicationQueueItem)).status == QueueStatus.READY
        assert session.get(MonitoringLease, "legacy-lease").owner == "legacy-owner"
    command.downgrade(config, "0002_simulated_pipeline")
    _assert_legacy_data_intact(engine, ids)
    assert {"categories", "product_categories", "destination_categories"}.isdisjoint(
        inspect(engine).get_table_names()
    )
    assert "category_mode" not in {
        column["name"] for column in inspect(engine).get_columns("publication_destinations")
    }
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 1
        assert session.get(MonitoringLease, "legacy-lease").owner == "legacy-owner"
    command.upgrade(config, "head")
    _assert_legacy_data_intact(engine, ids)


def _create_category_links(engine, ids=None):
    ids = ids or _seed_legacy_data(engine)
    with Session(engine) as session:
        category = Category(slug="casa-decoracao", name="Casa e Decoração")
        session.add(category)
        session.flush()
        session.add_all(
            [
                ProductCategory(
                    product_id=ids["product_id"],
                    category_id=category.id,
                    source=CategorySource.MANUAL,
                    confidence=Decimal("1.0000"),
                    explanation="Escolha administrativa",
                ),
                DestinationCategory(destination_id=ids["destination_id"], category_id=category.id),
            ]
        )
        session.commit()
        return {**ids, "category_id": category.id}


def test_category_relationships_metadata_decimal_and_manual_empty_override(migration_database):
    _, engine = migration_database
    ids = _create_category_links(engine)
    with Session(engine) as session:
        product = session.get(Product, ids["product_id"])
        product.description = "Descrição local do produto"
        product.classification_metadata = {
            "store_category_slugs": ["casa-decoracao"],
            "store_category_trusted": True,
        }
        product.manual_category_override = True
        destination = session.get(PublicationDestination, ids["destination_id"])
        destination.category_mode = CategoryMode.SELECTED
        session.commit()
        session.expire_all()
        assert product.classification_metadata["store_category_trusted"] is True
        assert product.category_links[0].confidence == Decimal("1.0000")
        assert product.category_links[0].source == CategorySource.MANUAL
        assert product.category_links[0].created_at.tzinfo is UTC
        assert destination.category_links[0].category.slug == "casa-decoracao"
        product.category_links.clear()
        session.commit()
        session.expire_all()
        assert product.category_links == []
        assert product.manual_category_override is True


@pytest.mark.parametrize("duplicate", ["slug", "product_pair", "destination_pair"])
def test_category_database_uniqueness(migration_database, duplicate):
    _, engine = migration_database
    ids = _create_category_links(engine)
    with Session(engine) as session:
        if duplicate == "slug":
            row = Category(slug="casa-decoracao", name="Duplicada")
        elif duplicate == "product_pair":
            row = ProductCategory(
                product_id=ids["product_id"],
                category_id=ids["category_id"],
                source=CategorySource.RULE,
            )
        else:
            row = DestinationCategory(
                destination_id=ids["destination_id"], category_id=ids["category_id"]
            )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize("confidence", [Decimal("-0.0001"), Decimal("1.0001")])
def test_category_database_rejects_invalid_confidence(migration_database, confidence):
    _, engine = migration_database
    _create_category_links(engine)
    with Session(engine) as session:
        association = session.scalar(select(ProductCategory))
        association.confidence = confidence
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize("target", ["source", "category_mode"])
def test_category_database_rejects_unknown_enum_values(migration_database, target):
    _, engine = migration_database
    _create_category_links(engine)
    statement = (
        "UPDATE product_categories SET source = 'unknown'"
        if target == "source"
        else "UPDATE publication_destinations SET category_mode = 'unknown'"
    )
    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(statement))
        connection.rollback()


def test_category_fk_restricts_deletion_and_parent_cascades_remove_only_links(migration_database):
    _, engine = migration_database
    ids = _create_category_links(engine)
    with Session(engine) as session:
        category = session.get(Category, ids["category_id"])
        # Loaded relationships must retain RESTRICT rather than ORM-cascading links.
        assert category.product_links and category.destination_links
        session.delete(category)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    with engine.begin() as connection:
        connection.execute(ProductCategory.__table__.delete())
    with Session(engine) as session:
        session.delete(session.get(Category, ids["category_id"]))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        unreferenced_product = Product(
            platform=Platform.SHOPEE,
            name="Sem auditoria",
            original_url="https://shopee.com.br/teste",
        )
        unreferenced_destination = PublicationDestination(
            name="Sem auditoria",
            channel=DestinationChannel.SIMULATED,
            external_id="category-cascade",
        )
        session.add_all([unreferenced_product, unreferenced_destination])
        session.flush()
        session.add_all(
            [
                ProductCategory(
                    product_id=unreferenced_product.id,
                    category_id=ids["category_id"],
                    source=CategorySource.RULE,
                ),
                DestinationCategory(
                    destination_id=unreferenced_destination.id, category_id=ids["category_id"]
                ),
            ]
        )
        session.commit()
        product_id, destination_id = unreferenced_product.id, unreferenced_destination.id
    with engine.begin() as connection:
        connection.execute(Product.__table__.delete().where(Product.id == product_id))
        connection.execute(
            PublicationDestination.__table__.delete().where(
                PublicationDestination.id == destination_id
            )
        )
        assert connection.scalar(select(func.count()).select_from(ProductCategory)) == 0
        assert connection.scalar(select(func.count()).select_from(DestinationCategory)) == 1
        assert connection.scalar(select(func.count()).select_from(Category)) == 1


@pytest.mark.parametrize("migration_database", ["0003_category_routing"], indirect=True)
def test_marketplace_migration_preserves_legacy_rows_and_derives_only_simulated_source(
    migration_database,
):
    config, engine = migration_database
    ids = _seed_legacy_data(engine)
    _create_category_links(engine, ids)
    metadata = MetaData()
    metadata.reflect(engine)
    observed_at = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["product_price_history"]
            .insert()
            .values(
                product_id=ids["product_id"],
                current_price=Decimal("89.90"),
                price_access="EXCLUSIVE",
                source="legacy-manual",
                is_simulated=False,
                observed_at=observed_at,
                created_at=observed_at,
            )
        )
        connection.execute(
            metadata.tables["publication_queue_items"]
            .insert()
            .values(
                product_id=ids["product_id"],
                offer_id=ids["offer_id"],
                destination_id=ids["destination_id"],
                status="READY",
                created_at=observed_at,
                updated_at=observed_at,
            )
        )
    command.upgrade(config, "head")
    _assert_legacy_data_intact(engine, ids)
    with Session(engine) as session:
        product = session.get(Product, ids["product_id"])
        assert product.variant_id == ""
        assert product.category_links[0].source == CategorySource.MANUAL
        simulated = session.get(ProductPriceHistory, ids["price_history_id"])
        assert simulated.source_type == SourceType.SIMULATED
        assert simulated.evidence_metadata == {}
        assert simulated.raw_fingerprint is None
        unknown = session.scalar(
            select(ProductPriceHistory).where(ProductPriceHistory.source == "legacy-manual")
        )
        assert unknown.source_type == SourceType.UNKNOWN
        assert unknown.price_access == PriceAccess.EXCLUSIVE
        item = session.scalar(select(PublicationQueueItem))
        assert item.blocked_reason is None and item.next_eligible_at is None
        assert item.is_simulated is True
    command.downgrade(config, "0003_category_routing")
    _assert_legacy_data_intact(engine, ids)
    assert "variant_id" not in {
        column["name"] for column in inspect(engine).get_columns("products")
    }
    command.upgrade(config, "head")
    _assert_legacy_data_intact(engine, ids)


def test_product_variants_are_distinct_and_same_variant_remains_unique(migration_database):
    _, engine = migration_database
    with Session(engine) as session:
        common = dict(
            platform=Platform.SHOPEE,
            external_id="shop:123:item:456",
            name="Produto",
            original_url="https://shopee.com.br/teste",
        )
        rows = [Product(**common, variant_id=variant) for variant in ("", "red-128", "blue-256")]
        session.add_all(rows)
        session.commit()
        assert len({product.id for product in rows}) == 3
        session.add(Product(**common, variant_id="red-128"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_snapshot_fingerprint_deduplicates_same_instant_but_preserves_fresh_revalidation(
    migration_database,
):
    _, engine = migration_database
    ids = _seed_legacy_data(engine)
    observed_at = datetime(2026, 10, 18, 13, 0, tzinfo=UTC)
    values = dict(
        product_id=ids["product_id"],
        current_price=Decimal("80.00"),
        source="official_synthetic_fixture",
        source_type=SourceType.OFFICIAL_API,
        source_reference="synthetic:product-price",
        raw_fingerprint="a" * 64,
        observed_at=observed_at,
        evidence_metadata={"fixture": True},
    )
    with Session(engine) as session:
        session.add(ProductPriceHistory(**values))
        session.commit()
        session.add(ProductPriceHistory(**values))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(ProductPriceHistory(**{**values, "observed_at": observed_at.replace(hour=17)}))
        session.commit()
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProductPriceHistory)
                .where(ProductPriceHistory.raw_fingerprint == "a" * 64)
            )
            == 2
        )
        session.add(ProductPriceHistory(**{**values, "raw_fingerprint": "invalid"}))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize(
    "access", [PriceAccess.CONDITIONAL, PriceAccess.UNKNOWN, PriceAccess.EXCLUSIVE]
)
def test_new_access_states_and_noncalculable_coupon_evidence_are_preserved(
    migration_database, access
):
    _, engine = migration_database
    ids = _seed_legacy_data(engine)
    with Session(engine) as session:
        history = session.get(ProductPriceHistory, ids["price_history_id"])
        history.price_access = access
        evidence = CouponEvidence(
            product_id=ids["product_id"],
            platform=Platform.MERCADO_LIVRE,
            type=CouponEvidenceType.FREE_SHIPPING,
            price_access=access,
            conditions={"minimum_known": False},
        )
        session.add(evidence)
        session.commit()
        session.refresh(evidence)
        assert evidence.code is None and evidence.value is None
        assert evidence.validation_state.value == "UNVERIFIED"
        assert evidence.price_access == access
        assert evidence.source_type == SourceType.UNKNOWN
        assert evidence.observed_at.tzinfo is UTC
        assert evidence.conditions == {"minimum_known": False}


@pytest.mark.parametrize(
    "values",
    [
        {"type": CouponEvidenceType.PERCENT, "value": Decimal("100.01")},
        {"value": Decimal("0")},
        {"value": Decimal("-1")},
        {"minimum_purchase": Decimal("-0.01")},
        {"max_discount": Decimal("0")},
    ],
)
def test_coupon_evidence_database_rejects_invalid_amounts(migration_database, values):
    _, engine = migration_database
    with Session(engine) as session:
        session.add(CouponEvidence(platform=Platform.SHOPEE, **values))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_integration_events_preserve_errors_without_changing_valid_prices(migration_database):
    _, engine = migration_database
    ids = _seed_legacy_data(engine)
    with Session(engine) as session:
        session.add(
            IntegrationEvent(
                product_id=ids["product_id"],
                platform=Platform.MERCADO_LIVRE,
                operation="fetch_product",
                status="FAILED",
                error_code="TIMEOUT",
                source_reference="official:item-price",
            )
        )
        session.commit()
        event_row = session.scalar(select(IntegrationEvent))
        assert event_row.observed_at.tzinfo is UTC
        assert session.get(ProductPriceHistory, ids["price_history_id"]).current_price == Decimal(
            "99.90"
        )
        assert {column.name for column in IntegrationEvent.__table__.columns} == {
            "id",
            "product_id",
            "platform",
            "operation",
            "status",
            "error_code",
            "observed_at",
            "source_reference",
        }


@pytest.mark.parametrize(
    "risk",
    [
        "variant",
        "history_access",
        "coupon_access",
        "source",
        "source_reference",
        "metadata",
        "fingerprint",
        "next_eligible",
        "blocked_reason",
        "coupon_evidence",
        "integration_event",
    ],
)
def test_marketplace_downgrade_refuses_data_loss_before_any_schema_change(migration_database, risk):
    config, engine = migration_database
    ids = _seed_legacy_data(engine)
    with Session(engine) as session:
        if risk == "variant":
            session.get(Product, ids["product_id"]).variant_id = "red-128"
        elif risk == "history_access":
            session.get(
                ProductPriceHistory, ids["price_history_id"]
            ).price_access = PriceAccess.CONDITIONAL
        elif risk == "coupon_access":
            session.execute(text("UPDATE coupons SET price_access = 'UNKNOWN'"))
        elif risk in ("source", "source_reference", "metadata", "fingerprint"):
            row = session.get(ProductPriceHistory, ids["price_history_id"])
            if risk == "source":
                row.source_type = SourceType.OFFICIAL_API
            elif risk == "source_reference":
                row.source_reference = "official:price"
            elif risk == "metadata":
                row.evidence_metadata = {"seller_id": "synthetic-seller"}
            else:
                row.raw_fingerprint = "b" * 64
        elif risk in ("next_eligible", "blocked_reason"):
            row = PublicationQueueItem(
                product_id=ids["product_id"],
                offer_id=ids["offer_id"],
                destination_id=ids["destination_id"],
            )
            if risk == "next_eligible":
                row.next_eligible_at = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)
            else:
                row.blocked_reason = "AFFILIATE_PENDING"
            session.add(row)
        elif risk == "coupon_evidence":
            session.add(CouponEvidence(platform=Platform.SHOPEE, type=CouponEvidenceType.UNKNOWN))
        else:
            session.add(
                IntegrationEvent(
                    platform=Platform.SHOPEE, operation="fetch_product", status="DISABLED"
                )
            )
        session.commit()
    with pytest.raises(RuntimeError, match="without data loss"):
        command.downgrade(config, "0003_category_routing")
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES | {"alembic_version"}
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0004_marketplace_provenance"
        )


def test_marketplace_downgrade_offline_postgresql_emits_guard_before_drop(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test@localhost/test")
    get_settings.cache_clear()
    output = StringIO()
    config = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=output)
    try:
        command.downgrade(config, "0004_marketplace_provenance:0003_category_routing", sql=True)
    finally:
        get_settings.cache_clear()
    sql = output.getvalue()
    assert sql.index("Cannot downgrade marketplace provenance without data loss") < sql.index(
        "DROP TABLE integration_events"
    )


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
        isolated_engine = create_engine(isolated_url)
        command.upgrade(config, "0001_initial_foundation")
        legacy_ids = _seed_legacy_data(isolated_engine)
        command.upgrade(config, "head")
        _assert_legacy_data_intact(isolated_engine, legacy_ids)
        category_ids = _create_category_links(isolated_engine, legacy_ids)
        with Session(isolated_engine) as session:
            destination = session.get(PublicationDestination, legacy_ids["destination_id"])
            assert destination.category_mode == CategoryMode.UNCONFIGURED
            association = session.scalar(select(ProductCategory))
            assert association.confidence == Decimal("1.0000")
            assert association.source == CategorySource.MANUAL
            assert association.created_at.utcoffset().total_seconds() == 0
            association.confidence = Decimal("1.0001")
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            session.add(
                ProductCategory(
                    product_id=legacy_ids["product_id"],
                    category_id=category_ids["category_id"],
                    source=CategorySource.RULE,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            session.add(
                DestinationCategory(
                    destination_id=legacy_ids["destination_id"],
                    category_id=category_ids["category_id"],
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            session.add(Category(slug="casa-decoracao", name="Duplicada"))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            category = session.get(Category, category_ids["category_id"])
            assert category.product_links and category.destination_links
            session.delete(category)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
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
            snapshot = session.scalar(
                select(ProductPriceHistory).where(ProductPriceHistory.product_id == product.id)
            )
            assert snapshot.current_price == Decimal("1234.56")
            assert snapshot.observed_at.utcoffset().total_seconds() == 0
        command.downgrade(config, "0001_initial_foundation")
        _assert_legacy_data_intact(isolated_engine, legacy_ids)
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
