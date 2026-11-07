"""Synthetic API contracts with a real PostgreSQL test schema; no marketplace network."""

from decimal import Decimal

import pytest
import test_marketplace_pipeline as fixtures
import test_postgres_categories as postgres_fixtures
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.models import (
    CategoryMode,
    DestinationChannel,
    Offer,
    Platform,
    ProductPriceHistory,
    PublicationDestination,
    PublicationQueueItem,
)
from app.services.categories import CategoryService
from app.services.monitoring import collect_price
from app.services.offers import OfferEngine

categories_postgres = postgres_fixtures.categories_postgres
ml_payloads = fixtures.ml_payloads


@pytest.mark.postgres
@pytest.mark.parametrize("platform", [Platform.MERCADO_LIVRE, Platform.SHOPEE])
def test_synthetic_connector_pipeline_on_real_postgresql(
    categories_postgres, ml_payloads, platform
):
    engine, _ = categories_postgres
    settings = Settings(_env_file=None, app_env="test", scheduler_enabled=False)
    with Session(engine, expire_on_commit=False) as session:
        CategoryService().seed(session)
        session.add(
            PublicationDestination(
                name="SYNTHETIC real PG dry-run",
                channel=DestinationChannel.SIMULATED,
                external_id="synthetic:postgres:dry-run",
                category_mode=CategoryMode.ALL,
                enabled=True,
                manual_approval_required=False,
            )
        )
        product = fixtures.registered_product(session, platform, verified_link=True)
        provider, _ = (
            fixtures.ml_provider(ml_payloads)
            if platform == Platform.MERCADO_LIVRE
            else fixtures.shopee_provider()
        )
        history = collect_price(session, product, provider, now=fixtures.NOW)
        offer = OfferEngine(settings, clock=lambda: fixtures.NOW).process(session, product, history)
        session.commit()
        session.expire_all()
        stored = session.get(ProductPriceHistory, history.id)
        assert stored.current_price == (
            Decimal("80.00") if platform == Platform.MERCADO_LIVRE else Decimal("99.90")
        )
        assert stored.source_type == "OFFICIAL_API" and stored.is_simulated is False
        assert stored.observed_at.utcoffset().total_seconds() == 0
        assert stored.evidence_metadata["image_url"].startswith("https://")
        if platform == Platform.MERCADO_LIVRE:
            assert stored.previous_price == Decimal("100.00") and offer is not None
            assert session.scalar(select(PublicationQueueItem)).is_simulated is True
        else:
            assert stored.previous_price is None and offer is None
            assert stored.evidence_metadata["commission_rate"] == "0.1234"
            assert session.scalar(select(func.count()).select_from(Offer)) == 0
        variant = fixtures.registered_product(session, platform, variant_id="SYNTHETIC-VARIANT")
        provider, _ = (
            fixtures.ml_provider(ml_payloads)
            if platform == Platform.MERCADO_LIVRE
            else fixtures.shopee_provider()
        )
        variant_history = collect_price(session, variant, provider, now=fixtures.NOW)
        session.commit()
        assert variant_history.current_price is None
        assert variant_history.product_id != history.product_id

        class TimeoutFixture:
            def fetch(self, product):
                raise IntegrationError("TIMEOUT", "SYNTHETIC timeout; no network was used.")

        with pytest.raises(IntegrationError):
            collect_price(session, product, TimeoutFixture(), now=fixtures.NOW)
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 2
        assert session.get(ProductPriceHistory, history.id).current_price == stored.current_price
        house = session.scalar(
            select(PublicationDestination).where(
                PublicationDestination.external_id == "local:ofertas-de-casa"
            )
        )
        assert house.enabled is False
