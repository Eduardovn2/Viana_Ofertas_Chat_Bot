"""Opt-in category migration checks against a real, isolated PostgreSQL schema."""

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.core.config import Settings, get_settings
from app.integrations.mock import SimulatedPriceProvider
from app.models import (
    AffiliateLink,
    Base,
    Category,
    CategoryMode,
    CategorySource,
    DestinationCategory,
    DestinationChannel,
    Platform,
    Product,
    ProductCategory,
    PublicationDestination,
    PublicationQueueItem,
)
from app.services.categories import CategoryService
from app.services.category_routing import destination_accepts
from app.services.monitoring import collect_price
from app.services.offers import OfferEngine
from app.services.publication_queue import SimulatedQueueService

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def categories_postgres(monkeypatch):
    """Never connect unless explicitly pointed at a dedicated *_test database."""
    configured_url = os.getenv("TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("TEST_DATABASE_URL não configurada; categorias PostgreSQL opcionais")
    url = make_url(configured_url)
    if url.drivername != "postgresql+psycopg" or not (url.database or "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL deve usar postgresql+psycopg e banco com sufixo _test")

    schema_name = f"ofertas_bot_categories_test_{uuid4().hex}"
    admin_engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    isolated_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        schema_created = True
        previous_options = str(url.query.get("options", ""))
        isolated_url = url.update_query_dict(
            {"options": f"{previous_options} -csearch_path={schema_name}".strip()}
        )
        monkeypatch.setenv("DATABASE_URL", isolated_url.render_as_string(hide_password=False))
        get_settings.cache_clear()
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        command.upgrade(config, "0003_category_routing")
        isolated_engine = create_engine(
            isolated_url, hide_parameters=True, connect_args={"connect_timeout": 5}
        )
        yield isolated_engine, isolated_url
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
        get_settings.cache_clear()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()


@pytest.mark.postgres
def test_category_migration_seed_routing_and_health_on_postgresql(categories_postgres):
    engine, isolated_url = categories_postgres
    inspector = inspect(engine)
    assert {"categories", "product_categories", "destination_categories"} <= set(
        inspector.get_table_names()
    )
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, Base.metadata) == []
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("categories")
    }
    assert ("slug",) in unique_columns
    for table, pair in (
        ("product_categories", ("product_id", "category_id")),
        ("destination_categories", ("destination_id", "category_id")),
    ):
        assert pair in {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table)
        }
        assert len(inspector.get_foreign_keys(table)) == 2
    assert inspector.get_check_constraints("product_categories")
    assert inspector.get_check_constraints("publication_destinations")

    now = datetime.now(UTC)
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=isolated_url.render_as_string(hide_password=False),
        scheduler_enabled=False,
        provider_min_interval_seconds=Decimal("0"),
    )
    service = CategoryService()
    with Session(engine, expire_on_commit=False) as session:
        service.seed(session)
        service.seed(session)
        session.commit()
        categories = {category.slug: category for category in session.scalars(select(Category))}
        assert set(categories) == {
            "casa-decoracao",
            "materiais-ferramentas",
            "eletrodomesticos",
            "moda",
            "infantil",
            "tecnologia",
            "games",
            "beleza",
            "gerais",
        }
        seeded = session.scalars(select(PublicationDestination)).one()
        assert seeded.name == "Ofertas de Casa"
        assert seeded.external_id == "local:ofertas-de-casa"
        assert seeded.enabled is False
        assert seeded.category_mode == CategoryMode.SELECTED
        seeded_ids = set(
            session.scalars(
                select(DestinationCategory.category_id).where(
                    DestinationCategory.destination_id == seeded.id
                )
            )
        )
        assert len(seeded_ids) == 8
        assert categories["gerais"].id not in seeded_ids

        # Re-running the seed preserves preferences edited by the administrator.
        seeded.name = "Ofertas de Casa - nome ajustado localmente"
        seeded.category_mode = CategoryMode.ALL
        seeded.enabled = True
        session.execute(
            delete(DestinationCategory).where(
                DestinationCategory.destination_id == seeded.id,
                DestinationCategory.category_id == categories["games"].id,
            )
        )
        service.seed(session)
        session.commit()
        session.refresh(seeded)
        assert seeded.enabled is True and seeded.category_mode == CategoryMode.ALL
        assert seeded.name.endswith("ajustado localmente")
        assert (
            session.scalar(
                select(func.count())
                .select_from(DestinationCategory)
                .where(DestinationCategory.destination_id == seeded.id)
            )
            == 7
        )

        product = Product(
            platform=Platform.MERCADO_LIVRE,
            external_id="MLB3810190843",
            name="Produto controlado da integração PostgreSQL",
            original_url="https://produto.mercadolivre.com.br/MLB-3810190843-exemplo_JM",
            affiliate_link=AffiliateLink(url="https://meli.la/1JQSATy"),
        )
        fallback = Product(
            platform=Platform.SHOPEE, name="XYZQT 123", original_url="https://shopee.com.br/exemplo"
        )
        selected = PublicationDestination(
            name="Categorias selecionadas - teste",
            channel=DestinationChannel.SIMULATED,
            external_id="pg-categories-selected",
            category_mode=CategoryMode.SELECTED,
            enabled=True,
            manual_approval_required=False,
        )
        accepts_all = PublicationDestination(
            name="Todas as categorias - teste",
            channel=DestinationChannel.SIMULATED,
            external_id="pg-categories-all",
            category_mode=CategoryMode.ALL,
            enabled=True,
            manual_approval_required=False,
        )
        unconfigured = PublicationDestination(
            name="Sem configuração - teste",
            channel=DestinationChannel.SIMULATED,
            external_id="pg-categories-unconfigured",
            enabled=True,
        )
        session.add_all([product, fallback, selected, accepts_all, unconfigured])
        session.flush()
        allowed_ids = [categories["casa-decoracao"].id, categories["games"].id]
        service.set_manual(session, product, allowed_ids)
        service.classify(session, product)
        manual = session.scalars(
            select(ProductCategory).where(ProductCategory.product_id == product.id)
        ).all()
        assert {row.category_id for row in manual} == set(allowed_ids)
        assert all(row.source == CategorySource.MANUAL for row in manual)
        service.classify(session, fallback)
        for category_id in allowed_ids:
            session.add(DestinationCategory(destination_id=selected.id, category_id=category_id))
        session.flush()
        assert destination_accepts(session, product, selected)
        assert destination_accepts(session, product, accepts_all)
        assert not destination_accepts(session, product, unconfigured)
        assert not destination_accepts(session, fallback, selected)
        assert destination_accepts(session, fallback, accepts_all)

        for slug in ("casa-decoracao", "games"):
            categories[slug].active = False
        session.flush()
        assert not destination_accepts(session, product, selected)
        assert not destination_accepts(session, product, accepts_all)
        for slug in ("casa-decoracao", "games"):
            categories[slug].active = True
        session.flush()

        history = collect_price(
            session,
            product,
            SimulatedPriceProvider(
                current_price=Decimal("99.90"), previous_price=Decimal("199.90"), clock=lambda: now
            ),
            now=now,
        )
        offer = OfferEngine(settings, clock=lambda: now).process(session, product, history)
        assert offer is not None
        queue = SimulatedQueueService(settings, clock=lambda: now)
        queue.enqueue(session, offer, selected)
        queue.enqueue(session, offer, selected)
        session.commit()
        assert (
            session.scalar(
                select(func.count())
                .select_from(PublicationQueueItem)
                .where(
                    PublicationQueueItem.product_id == product.id,
                    PublicationQueueItem.destination_id == selected.id,
                    PublicationQueueItem.active_slot == 1,
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(PublicationQueueItem)
                .where(PublicationQueueItem.destination_id == unconfigured.id)
            )
            == 0
        )

        invalid_rows = [
            Category(slug="gerais", name="Slug duplicado"),
            ProductCategory(
                product_id=product.id, category_id=allowed_ids[0], source=CategorySource.MANUAL
            ),
            DestinationCategory(destination_id=selected.id, category_id=allowed_ids[0]),
            ProductCategory(
                product_id=product.id,
                category_id=max(row.id for row in categories.values()) + 1000000,
                source=CategorySource.MANUAL,
            ),
            ProductCategory(
                product_id=product.id,
                category_id=categories["tecnologia"].id,
                source=CategorySource.MANUAL,
                confidence=Decimal("1.1000"),
            ),
        ]
        for invalid_row in invalid_rows:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(invalid_row)
                session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE publication_destinations SET category_mode = 'invalid' WHERE id = :id"
                ),
                {"id": selected.id},
            )
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO product_categories "
                    "(product_id, category_id, source, created_at, updated_at) "
                    "VALUES (:product, :category, 'other', :now, :now)"
                ),
                {"product": product.id, "category": categories["tecnologia"].id, "now": now},
            )

    from app.main import create_app

    application = create_app(settings)
    with TestClient(
        application, base_url="http://localhost", client=("127.0.0.1", 50000)
    ) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["database"] == "connected"
        assert application.state.monitoring.status()["running"] is False
        assert application.state.monitoring.status()["threads_blocked"] is True
