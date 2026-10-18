"""Synthetic connector → collector → SQLite → offer/queue contracts, entirely offline.

The reused connector fixtures contain synthetic commercial data, never captured
responses or live credentials. No external HTTP client is used in these tests.
"""

from copy import deepcopy
from decimal import Decimal

import pytest
import test_mercado_livre as ml_fixtures
import test_shopee as shopee_fixtures
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.errors import AuthorizationDenied, IntegrationError, InvalidPayload
from app.integrations.mercado_livre import MercadoLivreConnector
from app.integrations.registry import ConnectorProvider
from app.integrations.shopee import ShopeeConnector
from app.models import (
    AffiliateLink,
    Availability,
    Base,
    Category,
    CategoryMode,
    CategorySource,
    DestinationChannel,
    Offer,
    OfferStatus,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    PublicationQueueItem,
    QueueStatus,
    SourceType,
)
from app.services.categories import CategoryService
from app.services.monitoring import collect_price
from app.services.offers import OfferEngine

NOW = ml_fixtures.NOW
ml_payloads = ml_fixtures.payloads


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        CategoryService().seed(session)
        session.add(
            PublicationDestination(
                name="SYNTHETIC pipeline dry-run",
                channel=DestinationChannel.SIMULATED,
                external_id="local:synthetic-pipeline",
                category_mode=CategoryMode.ALL,
                enabled=True,
                manual_approval_required=False,
            )
        )
        session.flush()
        yield session
    engine.dispose()


@pytest.fixture
def settings():
    return Settings(_env_file=None, scheduler_enabled=False)


def registered_product(session, platform, *, variant_id="", verified_link=False):
    product = Product(
        platform=platform,
        external_id=ml_fixtures.ITEM_ID if platform == Platform.MERCADO_LIVRE else "101:202",
        variant_id=variant_id,
        original_url=ml_fixtures.URL if platform == Platform.MERCADO_LIVRE else shopee_fixtures.URL,
        name="SYNTHETIC cadastro local",
        monitoring_enabled=True,
        priority=False,
    )
    session.add(product)
    session.flush()
    if verified_link:
        session.add(
            AffiliateLink(
                product_id=product.id,
                url="https://meli.la/SYNTHETIC"
                if platform == Platform.MERCADO_LIVRE
                else "https://shope.ee/SYNTHETIC",
                source="MANUAL_LINKBUILDER_VERIFIED"
                if platform == Platform.MERCADO_LIVRE
                else "MANUAL_SHOPEE_VERIFIED",
            )
        )
        session.flush()
    return product


def ml_provider(payloads):
    source, transport = ml_fixtures.connector(payloads)
    assert isinstance(source, MercadoLivreConnector)
    return ConnectorProvider(source), transport


def shopee_provider():
    transport = shopee_fixtures.FakeTransport(shopee_fixtures.payload(), fetched_at=NOW)
    source = ShopeeConnector(
        transport,
        app_id=SecretStr(shopee_fixtures.APP_ID),
        secret=SecretStr(shopee_fixtures.SECRET),
        enabled=True,
        clock=lambda: NOW,
    )
    return ConnectorProvider(source), transport


@pytest.mark.parametrize("verified_link", [False, True])
def test_ml_concrete_connector_persists_official_context_and_creates_only_dry_run_queue(
    session, settings, ml_payloads, verified_link
):
    product = registered_product(session, Platform.MERCADO_LIVRE, verified_link=verified_link)
    provider, transport = ml_provider(ml_payloads)
    history = collect_price(session, product, provider, now=NOW)
    offer = OfferEngine(settings, clock=lambda: NOW).process(session, product, history)
    assert history.current_price == Decimal("80.00")
    assert history.previous_price == Decimal("100.00")
    assert history.availability is Availability.AVAILABLE
    assert history.price_access is PriceAccess.PUBLIC
    assert history.source_type is SourceType.OFFICIAL_API and history.is_simulated is False
    assert (
        history.source_reference
        == f"https://api.mercadolibre.com/items/{ml_fixtures.ITEM_ID}/sale_price?context=channel_marketplace"
    )
    assert history.evidence_metadata["category_external_id"] == "MLB12345"
    assert history.evidence_metadata["image_url"] == "https://http2.mlstatic.com/synthetic.jpg"
    assert history.evidence_metadata["seller_id"] == "12345"
    assert history.evidence_metadata["request_context"] == "channel_marketplace"
    assert history.evidence_metadata["price_access_reason"] == "public_context_proven"
    assert ml_fixtures.TOKEN not in str(history.evidence_metadata)
    assert "PRIVATE-SYNTHETIC" not in str(history.evidence_metadata)
    assert product.classification_metadata["category_external_id"] == "MLB12345"
    assert product.name == ml_payloads[0]["title"]
    assert offer is not None and offer.price_history_id == history.id
    assert offer.status is (OfferStatus.READY if verified_link else OfferStatus.PENDING)
    item = session.scalar(select(PublicationQueueItem))
    assert item.status is (QueueStatus.READY if verified_link else QueueStatus.PENDING)
    assert item.blocked_reason == (None if verified_link else "AFFILIATE_PENDING")
    assert item.is_simulated is True and item.offer_id == offer.id
    assert len(transport.requests) == 3
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0
    house = session.scalar(
        select(PublicationDestination).where(PublicationDestination.name == "Ofertas de Casa")
    )
    assert house.enabled is False
    session.commit()
    session.expire_all()
    persisted = session.get(ProductPriceHistory, history.id)
    assert persisted.source_reference == history.source_reference
    assert persisted.evidence_metadata["image_url"] == "https://http2.mlstatic.com/synthetic.jpg"
    assert persisted.evidence_metadata["seller_id"] == "12345"


def test_shopee_concrete_connector_persists_typed_reference_and_commission_without_claiming_offer(
    session, settings
):
    product = registered_product(session, Platform.SHOPEE, verified_link=True)
    provider, transport = shopee_provider()
    history = collect_price(session, product, provider, now=NOW)
    assert history.current_price == Decimal("99.90")
    assert history.previous_price is None
    assert history.price_access is PriceAccess.UNKNOWN
    assert history.availability is Availability.UNKNOWN
    assert history.source_reference == "productOfferV2:101:202"
    assert history.source_type is SourceType.OFFICIAL_API and history.is_simulated is False
    assert history.evidence_metadata["category_external_ids"] == ["1", "2", "3"]
    assert history.evidence_metadata["category_external_id"] == "3"
    assert history.evidence_metadata["commission_rate"] == "0.1234"
    assert history.evidence_metadata["commission_amount"] == "12.32766"
    assert history.evidence_metadata["image_url"] == shopee_fixtures.SYNTHETIC_NODE["imageUrl"]
    assert history.evidence_metadata["seller_id"] is None
    assert shopee_fixtures.SECRET not in str(history.evidence_metadata)
    assert OfferEngine(settings, clock=lambda: NOW).process(session, product, history) is None
    assert session.scalar(select(func.count()).select_from(Offer)) == 0
    assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 0
    assert len(transport.calls) == 1
    session.commit()
    session.expire_all()
    persisted = session.get(ProductPriceHistory, history.id)
    assert persisted.source_reference == "productOfferV2:101:202"
    assert persisted.evidence_metadata["commission_amount"] == "12.32766"


@pytest.mark.parametrize("failure", ["authorization", "timeout", "invalid_price"])
def test_external_failures_preserve_last_valid_history_and_never_insert_an_empty_replacement(
    session, ml_payloads, failure
):
    product = registered_product(session, Platform.MERCADO_LIVRE)
    provider, _ = ml_provider(ml_payloads)
    previous = collect_price(session, product, provider, now=NOW)
    session.commit()
    if failure == "invalid_price":
        invalid = deepcopy(ml_payloads)
        invalid[1]["amount"] = "0"
        provider, _ = ml_provider(invalid)
        expected = InvalidPayload
    else:
        error = (
            AuthorizationDenied()
            if failure == "authorization"
            else IntegrationError("TIMEOUT", "SYNTHETIC timeout seguro.")
        )
        source = MercadoLivreConnector(
            ml_fixtures.SyntheticTransport([error]),
            token=SecretStr(ml_fixtures.TOKEN),
            enabled=True,
            clock=lambda: NOW,
        )
        provider = ConnectorProvider(source)
        expected = AuthorizationDenied if failure == "authorization" else IntegrationError
    with pytest.raises(expected):
        collect_price(session, product, provider, now=NOW)
    assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1
    persisted = session.get(ProductPriceHistory, previous.id)
    assert persisted.current_price == Decimal("80.00")
    assert persisted.source_reference == previous.source_reference
    assert (
        session.scalar(
            select(func.count())
            .select_from(ProductPriceHistory)
            .where(ProductPriceHistory.current_price.is_(None))
        )
        == 0
    )


@pytest.mark.parametrize("platform", [Platform.MERCADO_LIVRE, Platform.SHOPEE])
def test_unresolved_variant_is_separate_and_never_borrows_the_generic_item_price(
    session, settings, ml_payloads, platform
):
    generic = registered_product(session, platform, verified_link=True)
    variant = registered_product(
        session, platform, variant_id="SYNTHETIC-VARIANT", verified_link=True
    )
    first_provider, _ = (
        ml_provider(ml_payloads) if platform == Platform.MERCADO_LIVRE else shopee_provider()
    )
    generic_history = collect_price(session, generic, first_provider, now=NOW)
    OfferEngine(settings, clock=lambda: NOW).process(session, generic, generic_history)
    variant_provider, transport = (
        ml_provider(ml_payloads) if platform == Platform.MERCADO_LIVRE else shopee_provider()
    )
    variant_history = collect_price(session, variant, variant_provider, now=NOW)
    assert variant_history.product_id != generic_history.product_id
    assert variant.external_id == generic.external_id
    assert generic.variant_id == "" and variant.variant_id == "SYNTHETIC-VARIANT"
    assert generic_history.current_price == (
        Decimal("80") if platform == Platform.MERCADO_LIVRE else Decimal("99.90")
    )
    assert variant_history.current_price is None
    assert variant_history.price_access is PriceAccess.UNKNOWN
    assert variant_history.availability is Availability.UNKNOWN
    assert (
        OfferEngine(settings, clock=lambda: NOW).process(session, variant, variant_history) is None
    )
    assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 2
    assert (
        session.scalar(
            select(func.count()).select_from(Offer).where(Offer.product_id == variant.id)
        )
        == 0
    )
    calls = transport.requests if platform == Platform.MERCADO_LIVRE else transport.calls
    assert len(calls) == 1


def test_manual_category_correction_survives_concrete_official_metadata_and_rule_keywords(
    session, settings, ml_payloads
):
    product = registered_product(session, Platform.MERCADO_LIVRE, verified_link=True)
    beauty = session.scalar(select(Category).where(Category.slug == "beleza"))
    CategoryService().set_manual(session, product, [beauty.id])
    ml_payloads[0]["title"] = "Notebook gamer SYNTHETIC com categoria oficial"
    provider, _ = ml_provider(ml_payloads)
    history = collect_price(session, product, provider, now=NOW)
    assert OfferEngine(settings, clock=lambda: NOW).process(session, product, history) is not None
    assert product.manual_category_override is True
    assert {link.category.slug for link in product.category_links} == {"beleza"}
    assert product.category_links[0].source is CategorySource.MANUAL
    assert product.classification_metadata["category_external_id"] == "MLB12345"
    assert history.evidence_metadata["category_external_id"] == "MLB12345"


def test_identical_connector_evidence_at_the_same_observation_time_deduplicates_in_collector(
    session, ml_payloads
):
    product = registered_product(session, Platform.MERCADO_LIVRE)
    first_provider, _ = ml_provider(ml_payloads)
    first = collect_price(session, product, first_provider, now=NOW)
    second_provider, _ = ml_provider(ml_payloads)
    second = collect_price(session, product, second_provider, now=NOW)
    assert first.id == second.id
    assert first.raw_fingerprint == second.raw_fingerprint
    assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1
