"""Synthetic single-listing imports; no OAuth profile or external requests."""

from dataclasses import replace
from datetime import timedelta

import pytest
import test_mercado_livre as ml
import test_postgres_categories as pg
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.integrations.errors import AuthorizationDenied, IntegrationError
from app.models import (
    AffiliateLink,
    AppSetting,
    Base,
    CategoryMode,
    DestinationChannel,
    IntegrationEvent,
    Offer,
    Platform,
    Product,
    ProductCategory,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    PublicationQueueItem,
    QueueStatus,
)
from app.services.categories import CategoryService
from app.services.commercial_validation import validate_listing
from app.services.configuration import read_monitoring_configuration
from app.services.leases import acquire_lease
from app.services.publication_queue import SimulatedQueueService

payloads = ml.payloads
categories_postgres = pg.categories_postgres
LINK = "https://meli.la/SYNTHETIC"


@pytest.fixture
def factory(tmp_path):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "synthetic.sqlite3"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    seed(factory)
    yield factory
    engine.dispose()


def seed(factory):
    with factory() as session:
        CategoryService().seed(session)
        session.add(
            PublicationDestination(
                name="SYNTHETIC dry-run",
                channel=DestinationChannel.SIMULATED,
                external_id="local:synthetic-validation",
                enabled=True,
                manual_approval_required=False,
                category_mode=CategoryMode.ALL,
            )
        )
        session.commit()


class Registry:
    def __init__(self, snapshot=None, error=None, clock=lambda: ml.NOW):
        self.snapshot, self.error, self.clock = snapshot, error, clock
        self.calls = 0
        self.unblocked = False

    def get(self, platform):
        assert platform == Platform.MERCADO_LIVRE
        return self

    def fetch_product(self, ref):
        self.calls += 1
        assert ref.external_item_id == ml.ITEM_ID
        if self.error:
            raise self.error
        return replace(self.snapshot, observed_at=self.clock())

    def unblock(self, platform):
        self.unblocked = True


def execute(factory, registry, clock=lambda: ml.NOW, link=LINK):
    return validate_listing(
        factory,
        Settings(_env_file=None, app_env="test"),
        ml.ITEM_ID,
        link,
        registry=registry,
        clock=clock,
    )


def snapshot(payloads):
    connector, _ = ml.connector(payloads)
    return connector.fetch_product(ml.ref())


def verify_failure(factory, registry):
    result = execute(factory, registry)
    assert result["status"] == "FAILED"
    with factory() as session:
        for model in (Product, ProductPriceHistory, Offer, PublicationQueueItem):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        event = session.scalar(select(IntegrationEvent))
        assert event.error_code == "AUTHORIZATION_DENIED" and event.product_id is None
        manual = session.scalar(select(AppSetting).where(AppSetting.key.like("manual-link:%")))
        assert manual.value["url"] == LINK
        assert (
            read_monitoring_configuration(session, Settings(_env_file=None)).scheduler_enabled
            is False
        )
    return result


def test_denied_read_never_creates_fake_product_or_price(factory):
    error = AuthorizationDenied()
    error.status_code = 403
    assert verify_failure(factory, Registry(error=error))["http_status"] == 403


def test_official_snapshot_uses_existing_categories_and_queue_and_deduplicates(factory, payloads):
    registry = Registry(snapshot(payloads))
    first = execute(factory, registry)
    second = execute(factory, registry)
    assert first["status"] == second["status"] == "SUCCEEDED"
    assert first["history_id"] == second["history_id"]
    assert second["current_price"] == "80.00" and second["queue_items"] == 1
    assert registry.unblocked
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProductCategory)) >= 1
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1
        assert session.scalar(select(AffiliateLink)).url == LINK
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0
        assert (
            session.scalar(
                select(PublicationDestination).where(
                    PublicationDestination.external_id == "local:ofertas-de-casa"
                )
            ).enabled
            is False
        )


def test_same_listing_cannot_bypass_three_hour_republication(factory, payloads):
    clock_value = [ml.NOW]

    def clock():
        return clock_value[0]

    registry = Registry(snapshot(payloads), clock=clock)
    execute(factory, registry, clock)
    with factory() as session:
        queued = session.scalar(select(PublicationQueueItem))
        assert (
            SimulatedQueueService(Settings(_env_file=None), clock)
            .publish(session, queued.id)
            .status
            == QueueStatus.PUBLISHED
        )
        session.commit()
    clock_value[0] += timedelta(seconds=10)
    assert execute(factory, registry, clock)["queue_items"] == 1
    clock_value[0] = ml.NOW + timedelta(hours=3, seconds=1)
    assert execute(factory, registry, clock)["queue_items"] == 2


def test_conflicting_manual_link_is_preserved(factory, payloads):
    registry = Registry(snapshot(payloads))
    execute(factory, registry)
    with pytest.raises(IntegrationError, match="configuração manual"):
        execute(factory, registry, link="https://meli.la/OTHER_SYNTHETIC")
    assert registry.calls == 1


def test_active_lease_prevents_duplicate_external_read(factory):
    acquire_lease(factory, f"commercial-check:{ml.ITEM_ID}", 120, lambda: ml.NOW)
    registry = Registry()
    with pytest.raises(IntegrationError) as error:
        execute(factory, registry)
    assert error.value.code == "MONITORING_BUSY" and registry.calls == 0


def test_unknown_price_preserves_metadata_without_eligible_offer(factory, payloads):
    from app.models import PriceAccess

    partial = replace(
        snapshot(payloads),
        current_price=None,
        previous_price=None,
        price_access=PriceAccess.UNKNOWN,
        evidence_metadata={
            "price_access_reason": "price_permission_denied",
            "price_http_status": 403,
        },
    )
    result = execute(factory, Registry(partial))
    assert result["status"] == "PARTIAL" and result["current_price"] is None
    assert result["offer_id"] is None and result["queue_items"] == 0
    with factory() as session:
        stored = session.scalar(select(ProductPriceHistory))
        assert stored.evidence_metadata["price_http_status"] == 403
        assert session.scalar(select(Product)).name == partial.name


@pytest.mark.postgres
@pytest.mark.parametrize("denied", [False, True])
def test_manual_oauth_connector_path_on_isolated_postgresql(categories_postgres, payloads, denied):
    engine, _ = categories_postgres
    factory = sessionmaker(engine, expire_on_commit=False)
    seed(factory)
    if denied:
        verify_failure(factory, Registry(error=AuthorizationDenied()))
    else:
        registry = Registry(snapshot(payloads))
        result = execute(factory, registry)
        assert result["current_price"] == "80.00" and result["queue_items"] == 1
        assert execute(factory, registry)["history_id"] == result["history_id"]
