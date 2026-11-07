import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.base import PriceSnapshot
from app.models import (
    AffiliateLink,
    Availability,
    Base,
    Category,
    CategoryMode,
    DestinationCategory,
    DestinationChannel,
    OfferStatus,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    PublicationQueueItem,
    QueueStatus,
)
from app.services.categories import CategoryService
from app.services.category_routing import destination_accepts, invalidate_ineligible_queue
from app.services.monitoring import record_price
from app.services.offers import OfferEngine
from app.services.publication_queue import SimulatedQueueService

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture
def context():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        service = CategoryService()
        service.seed(session)
        product = Product(
            platform=Platform.SHOPEE,
            name="Notebook gamer",
            monitoring_enabled=True,
            original_url="https://shopee.com.br/produto",
            priority=False,
        )
        destination = PublicationDestination(
            name="Destino de teste",
            channel=DestinationChannel.SIMULATED,
            external_id="local:routing-test",
            category_mode=CategoryMode.ALL,
            enabled=True,
            manual_approval_required=False,
        )
        session.add_all([product, destination])
        session.flush()
        session.add(
            AffiliateLink(
                product_id=product.id, url="https://shope.ee/teste", source="manual_official"
            )
        )
        session.flush()
        yield (
            session,
            product,
            destination,
            Settings(_env_file=None, scheduler_enabled=False),
            [NOW],
            service,
        )
    engine.dispose()


def category(session, slug):
    return session.scalar(select(Category).where(Category.slug == slug))


def select_categories(session, destination, *slugs):
    for row in session.scalars(
        select(DestinationCategory).where(DestinationCategory.destination_id == destination.id)
    ):
        session.delete(row)
    session.flush()
    destination.category_mode = CategoryMode.SELECTED
    for slug in slugs:
        session.add(
            DestinationCategory(
                destination_id=destination.id, category_id=category(session, slug).id
            )
        )
    session.flush()


def monitor(context, price="80"):
    session, product, _, settings, clock, _ = context
    history = record_price(
        session,
        product,
        PriceSnapshot(
            current_price=Decimal(price),
            previous_price=Decimal("100"),
            availability=Availability.AVAILABLE,
            price_access=PriceAccess.PUBLIC,
            source="simulated",
            observed_at=clock[0],
            is_simulated=True,
        ),
        now=clock[0],
    )
    return OfferEngine(settings, clock=lambda: clock[0]).process(session, product, history)


def active_items(session):
    return session.scalars(
        select(PublicationQueueItem).where(PublicationQueueItem.active_slot == 1)
    ).all()


def test_unconfigured_destination_does_not_queue_but_price_engine_still_creates_offer(context):
    session, product, destination, _, _, _ = context
    destination.category_mode = CategoryMode.UNCONFIGURED
    offer = monitor(context)
    assert offer is not None and offer.current_price == Decimal("80")
    assert offer.status is OfferStatus.READY
    assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1
    assert not destination_accepts(session, product, destination)
    assert active_items(session) == []


def test_default_simulation_is_unconfigured_and_house_seed_does_not_implicitly_allow_it(context):
    session, _, destination, settings, clock, _ = context
    destination.enabled = False
    default = SimulatedQueueService(settings, clock=lambda: clock[0]).get_or_create_destination(
        session
    )
    assert default.category_mode is CategoryMode.UNCONFIGURED
    assert monitor(context) is not None
    assert active_items(session) == []
    house = session.scalar(
        select(PublicationDestination).where(PublicationDestination.name == "Ofertas de Casa")
    )
    assert house.enabled is False


def test_all_requires_at_least_one_active_product_category_and_can_accept_explicit_fallback(
    context,
):
    session, product, destination, _, _, service = context
    assert not destination_accepts(session, product, destination)
    product.name = "Produto sem informação categorizável"
    service.classify(session, product)
    assert destination_accepts(session, product, destination)
    category(session, "gerais").active = False
    session.flush()
    assert not destination_accepts(session, product, destination)


def test_selected_requires_active_intersection_and_empty_selection_blocks_everything(context):
    session, product, destination, _, _, service = context
    service.classify(session, product)
    select_categories(session, destination)
    assert not destination_accepts(session, product, destination)
    select_categories(session, destination, "beleza")
    assert not destination_accepts(session, product, destination)
    select_categories(session, destination, "tecnologia", "games")
    assert destination_accepts(session, product, destination)
    category(session, "tecnologia").active = False
    category(session, "games").active = False
    session.flush()
    assert not destination_accepts(session, product, destination)


def test_house_uses_exactly_its_eight_categories_and_does_not_accept_gerais(context):
    session, product, _, _, _, service = context
    house = session.scalar(
        select(PublicationDestination).where(PublicationDestination.name == "Ofertas de Casa")
    )
    house.enabled = True
    service.classify(session, product)
    assert destination_accepts(session, product, house)
    product.name = "Produto especial sem descrição"
    service.classify(session, product)
    assert not destination_accepts(session, product, house)


def test_multiple_matching_categories_produce_one_slot_per_product_destination(context):
    session, _, destination, _, _, _ = context
    select_categories(session, destination, "tecnologia", "games")
    first = monitor(context)
    assert len(active_items(session)) == 1
    first_slot = active_items(session)[0]
    assert first_slot.offer_id == first.id
    monitor(context, "70")
    assert len(active_items(session)) == 1
    assert active_items(session)[0].id == first_slot.id


def test_manual_empty_classification_blocks_even_all_while_price_monitoring_continues(context):
    session, product, _, _, _, service = context
    service.set_manual(session, product, [])
    assert monitor(context) is not None
    assert active_items(session) == []
    assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1


def test_manual_category_change_invalidates_only_ineligible_destinations_and_keeps_offer(context):
    session, product, destination, _, _, service = context
    select_categories(session, destination, "tecnologia")
    other = PublicationDestination(
        name="Todas configuradas",
        channel=DestinationChannel.SIMULATED,
        external_id="local:all",
        category_mode=CategoryMode.ALL,
        enabled=True,
        manual_approval_required=False,
    )
    session.add(other)
    session.flush()
    offer = monitor(context)
    assert len(active_items(session)) == 2
    service.set_manual(session, product, [category(session, "beleza").id])
    assert offer.status is OfferStatus.READY
    assert [item.destination_id for item in active_items(session)] == [other.id]
    rejected = session.scalar(
        select(PublicationQueueItem).where(PublicationQueueItem.destination_id == destination.id)
    )
    assert rejected.status is QueueStatus.EXPIRED
    assert rejected.active_slot is None


def test_destination_policy_invalidation_does_not_modify_other_destinations(context):
    session, _, destination, _, _, _ = context
    monitor(context)
    destination.category_mode = CategoryMode.UNCONFIGURED
    assert invalidate_ineligible_queue(session, destination_id=destination.id) == 1
    assert active_items(session) == []


@pytest.mark.parametrize("change", ["policy", "inactive", "manual"])
def test_publish_revalidates_categories_without_relying_on_previous_queue_acceptance(
    context, change
):
    session, product, destination, settings, clock, service = context
    product.name = "Notebook"
    select_categories(session, destination, "tecnologia")
    offer = monitor(context)
    item = active_items(session)[0]
    if change == "policy":
        destination.category_mode = CategoryMode.UNCONFIGURED
    elif change == "inactive":
        category(session, "tecnologia").active = False
    else:
        service.set_manual(session, product, [category(session, "beleza").id])
    result = SimulatedQueueService(settings, clock=lambda: clock[0]).publish(session, item.id)
    assert result.status is QueueStatus.EXPIRED
    assert offer.status is OfferStatus.READY
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0


def test_rule_file_change_is_detected_before_publication(context, tmp_path, monkeypatch):
    session, product, destination, settings, clock, _ = context
    rules = tmp_path / "routing_rules.json"
    document = {"version": 1, "rules": [{"category_slug": "tecnologia", "keywords": ["widget"]}]}
    rules.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("CATEGORY_RULES_PATH", str(rules))
    product.name = "Widget"
    select_categories(session, destination, "tecnologia")
    offer = monitor(context)
    item = active_items(session)[0]
    document["rules"][0]["category_slug"] = "beleza"
    rules.write_text(json.dumps(document), encoding="utf-8")
    assert (
        SimulatedQueueService(settings, clock=lambda: clock[0]).publish(session, item.id).status
        is QueueStatus.EXPIRED
    )
    assert offer.status is OfferStatus.READY
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0


def test_changing_tags_never_erases_publication_history_or_resets_three_hour_cooldown(context):
    session, product, destination, settings, clock, service = context
    select_categories(session, destination, "tecnologia")
    monitor(context)
    first = active_items(session)[0]
    queue = SimulatedQueueService(settings, clock=lambda: clock[0])
    assert queue.publish(session, first.id).status is QueueStatus.PUBLISHED
    published_at = first.published_at
    service.set_manual(session, product, [category(session, "beleza").id])
    service.set_manual(session, product, [category(session, "tecnologia").id])
    clock[0] += timedelta(seconds=1)
    assert monitor(context) is not None
    assert active_items(session) == []
    assert first.status is QueueStatus.PUBLISHED
    assert first.published_at == published_at
    clock[0] = NOW + timedelta(hours=3)
    assert monitor(context) is not None
    second = active_items(session)[0]
    assert second.id != first.id
    assert queue.publish(session, second.id).status is QueueStatus.PUBLISHED
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 2
