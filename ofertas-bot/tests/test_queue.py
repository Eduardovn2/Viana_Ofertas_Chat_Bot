from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.base import PriceSnapshot
from app.models import (
    AffiliateLink,
    Availability,
    Base,
    CategoryMode,
    Coupon,
    CouponDiscountType,
    CouponValidationState,
    DestinationChannel,
    OfferStatus,
    Platform,
    PriceAccess,
    Product,
    PublicationDestination,
    PublicationLog,
    PublicationQueueItem,
    QueueStatus,
)
from app.services.comparison import PriceComparisonService, PriceDirection
from app.services.monitoring import record_price
from app.services.offers import OfferEngine
from app.services.publication_queue import SimulatedQueueService

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture
def context(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'queue.db').as_posix()}")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, scheduler_enabled=False)
    with Session(engine, expire_on_commit=False) as session:
        session.add(
            PublicationDestination(
                name="Simulação",
                channel=DestinationChannel.SIMULATED,
                external_id="simulation",
                enabled=True,
                manual_approval_required=False,
                category_mode=CategoryMode.ALL,
            )
        )
        product = Product(
            platform=Platform.MERCADO_LIVRE,
            name="Produto simulado",
            original_url="https://produto.mercadolivre.com.br/MLB-3810190843-exemplo",
            external_id="MLB3810190843",
            monitoring_enabled=True,
            priority=False,
        )
        session.add(product)
        session.flush()
        session.add(
            AffiliateLink(
                product_id=product.id, url="https://meli.la/teste", source="manual_official"
            )
        )
        session.flush()
        clock = [NOW]
        yield session, product, settings, clock
    engine.dispose()


def observation(session, product, now, price="80", previous="100", **changes):
    return record_price(
        session,
        product,
        PriceSnapshot(
            current_price=Decimal(price) if price is not None else None,
            previous_price=Decimal(previous) if previous is not None else None,
            availability=changes.pop("availability", Availability.AVAILABLE),
            price_access=changes.pop("price_access", PriceAccess.PUBLIC),
            source="simulated",
            is_simulated=True,
            observed_at=now,
            **changes,
        ),
        now=now,
    )


def make_offer(context, *, price="80", previous="100"):
    session, product, settings, clock = context
    history = observation(session, product, clock[0], price, previous)
    offer = OfferEngine(settings, clock=lambda: clock[0]).process(session, product, history)
    item = session.scalar(
        select(PublicationQueueItem).where(PublicationQueueItem.offer_id == offer.id)
    )
    return offer, item


def test_queue_is_deduplicated_and_new_observation_updates_active_offer_link(context):
    session, product, settings, clock = context
    first, item = make_offer(context)
    service = SimulatedQueueService(settings, clock=lambda: clock[0])
    assert service.enqueue(session, first).id == item.id
    clock[0] += timedelta(seconds=1)
    next_history = observation(session, product, clock[0], "70")
    next_offer = OfferEngine(settings, clock=lambda: clock[0]).process(
        session, product, next_history
    )
    assert next_offer.id != first.id
    assert first.status is OfferStatus.EXPIRED
    assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 1
    session.refresh(item)
    assert item.offer_id == next_offer.id
    assert item.status is QueueStatus.READY


def test_simulated_publish_is_explicit_marked_and_idempotent(context):
    session, _, settings, clock = context
    offer, item = make_offer(context)
    service = SimulatedQueueService(settings, clock=lambda: clock[0])
    assert item.status is QueueStatus.READY
    result = service.publish(session, item.id)
    assert result.status is QueueStatus.PUBLISHED
    assert result.active_slot is None
    assert result.is_simulated is True
    assert result.published_at == clock[0]
    assert result.attempts == 1
    assert offer.status is OfferStatus.PUBLISHED
    assert service.publish(session, item.id).status is QueueStatus.PUBLISHED
    logs = session.scalars(select(PublicationLog)).all()
    assert len(logs) == 1
    assert logs[0].message_text.startswith("[SIMULATED]")
    assert logs[0].message_id.startswith("simulated:")


def test_missing_own_link_stays_pending_and_can_be_added_later(context):
    session, product, settings, clock = context
    session.delete(product.affiliate_link)
    session.flush()
    session.expire(product, ["affiliate_link"])
    _, item = make_offer(context)
    service = SimulatedQueueService(settings, clock=lambda: clock[0])
    assert service.publish(session, item.id).status is QueueStatus.PENDING
    assert item.attempts == 0
    session.add(
        AffiliateLink(
            product_id=product.id, url="https://meli.la/meu-link", source="manual_official"
        )
    )
    session.flush()
    assert service.publish(session, item.id).status is QueueStatus.PUBLISHED


@pytest.mark.parametrize("problem", ["stale", "disabled", "unavailable", "exclusive", "newer"])
def test_publication_rechecks_current_data(context, problem):
    session, product, settings, clock = context
    _, item = make_offer(context)
    if problem == "stale":
        clock[0] += timedelta(seconds=601)
    elif problem == "disabled":
        product.monitoring_enabled = False
    else:
        clock[0] += timedelta(seconds=1)
        observation(
            session,
            product,
            clock[0],
            "90",
            **(
                {"availability": Availability.UNAVAILABLE}
                if problem == "unavailable"
                else {"price_access": PriceAccess.EXCLUSIVE}
                if problem == "exclusive"
                else {}
            ),
        )
    result = SimulatedQueueService(settings, clock=lambda: clock[0]).publish(session, item.id)
    assert result.status is QueueStatus.EXPIRED
    assert result.active_slot is None
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0


def test_coupon_is_rechecked_before_publication(context):
    session, product, settings, clock = context
    coupon = Coupon(
        code="SIMULADO",
        platform=product.platform,
        discount_type=CouponDiscountType.FIXED,
        discount_value=Decimal("5"),
        validation_state=CouponValidationState.CONFIRMED,
        price_access=PriceAccess.PUBLIC,
        eligible_products=[product],
        last_verified_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    session.add(coupon)
    session.flush()
    offer, item = make_offer(context, previous=None)
    assert offer.final_price == Decimal("75")
    coupon.eligible_products = []
    session.flush()
    assert (
        SimulatedQueueService(settings, clock=lambda: clock[0]).publish(session, item.id).status
        is QueueStatus.EXPIRED
    )
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0


def test_changed_coupon_amount_does_not_publish_outdated_final_price(context):
    session, product, settings, clock = context
    coupon = Coupon(
        code="SIMULADO",
        platform=product.platform,
        discount_type=CouponDiscountType.FIXED,
        discount_value=Decimal("5"),
        validation_state=CouponValidationState.CONFIRMED,
        price_access=PriceAccess.PUBLIC,
        eligible_products=[product],
        last_verified_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    session.add(coupon)
    session.flush()
    _, item = make_offer(context, previous=None)
    coupon.discount_value = Decimal("2")
    session.flush()
    assert (
        SimulatedQueueService(settings, clock=lambda: clock[0]).publish(session, item.id).status
        is QueueStatus.EXPIRED
    )


def test_three_hour_cooldown_and_fresh_observation_republish_unchanged_proven_drop(context):
    session, product, settings, clock = context
    observation(session, product, NOW - timedelta(minutes=1), "100", None)
    original_history = observation(session, product, NOW, "80", None)
    engine = OfferEngine(settings, clock=lambda: clock[0])
    offer = engine.process(session, product, original_history)
    first = session.scalar(select(PublicationQueueItem))
    service = SimulatedQueueService(settings, clock=lambda: clock[0])
    service.publish(session, first.id)
    clock[0] = NOW + timedelta(hours=3) - timedelta(seconds=1)
    too_early = observation(session, product, clock[0], "80", None)
    early_offer = engine.process(session, product, too_early)
    assert early_offer is not None
    assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 1
    clock[0] = NOW + timedelta(hours=3)
    fresh = observation(session, product, clock[0], "80", None)
    assert (
        PriceComparisonService().compare(session, fresh, now=clock[0]).direction
        is PriceDirection.UNCHANGED
    )
    carried = engine.process(session, product, fresh)
    assert carried.historical_price == Decimal("100")
    assert carried.discount_percentage == Decimal("20")
    assert carried.price_history_id != offer.price_history_id
    second = session.scalar(
        select(PublicationQueueItem).where(PublicationQueueItem.active_slot == 1)
    )
    assert second is not None and second.id != first.id
    assert service.publish(session, second.id).status is QueueStatus.PUBLISHED
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 2


def test_old_observation_cannot_republish_after_cooldown(context):
    session, product, settings, clock = context
    offer, item = make_offer(context)
    service = SimulatedQueueService(settings, clock=lambda: clock[0])
    service.publish(session, item.id)
    clock[0] += timedelta(hours=3)
    assert service.enqueue(session, offer) is None
    assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 1


def test_two_destinations_have_independent_queue_and_cooldown(context):
    session, product, settings, clock = context
    first_destination = SimulatedQueueService(settings).get_or_create_destination(session)
    first_destination.name = "Primeiro"
    first_destination.external_id = "first"
    second_destination = PublicationDestination(
        name="Segundo",
        channel=DestinationChannel.SIMULATED,
        external_id="second",
        enabled=True,
        manual_approval_required=True,
        category_mode=CategoryMode.ALL,
    )
    session.add_all([first_destination, second_destination])
    session.flush()
    make_offer(context)
    items = session.scalars(
        select(PublicationQueueItem).order_by(PublicationQueueItem.destination_id)
    ).all()
    assert len(items) == 2
    assert items[0].status is QueueStatus.READY
    assert items[1].status is QueueStatus.PENDING
    service = SimulatedQueueService(settings, clock=lambda: clock[0])
    service.publish(session, items[0].id)
    clock[0] += timedelta(seconds=1)
    next_history = observation(session, product, clock[0], "70")
    new_offer = OfferEngine(settings, clock=lambda: clock[0]).process(
        session, product, next_history
    )
    active = session.scalars(
        select(PublicationQueueItem).where(PublicationQueueItem.active_slot == 1)
    ).all()
    assert len(active) == 1
    assert active[0].destination_id == second_destination.id
    assert active[0].offer_id == new_offer.id
    assert service.publish(session, active[0].id).status is QueueStatus.PUBLISHED


def test_published_row_seen_by_another_session_cannot_create_a_second_log(context):
    session, _, settings, clock = context
    _, item = make_offer(context)
    session.commit()
    with Session(session.bind, expire_on_commit=False) as second_session:
        stale_item = second_session.get(PublicationQueueItem, item.id)
        assert stale_item.status is QueueStatus.READY
        service = SimulatedQueueService(settings, clock=lambda: clock[0])
        service.publish(session, item.id)
        session.commit()
        assert service.publish(second_session, item.id).status is QueueStatus.PUBLISHED
        second_session.commit()
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 1


def test_queue_attempt_limit_and_destination_are_enforced(context):
    session, _, settings, clock = context
    _, item = make_offer(context)
    item.attempts = settings.max_queue_attempts
    result = SimulatedQueueService(settings, clock=lambda: clock[0]).publish(session, item.id)
    assert result.status is QueueStatus.FAILED
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0


def test_concurrent_simulations_claim_one_row_and_write_one_log(context):
    session, _, settings, clock = context
    _, item = make_offer(context)
    item_id = item.id
    session.commit()
    barrier = Barrier(2)

    def publish():
        with Session(session.bind, expire_on_commit=False) as worker:
            barrier.wait(timeout=10)
            result = SimulatedQueueService(settings, clock=lambda: clock[0]).publish(
                worker, item_id
            )
            worker.commit()
            return result.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish) for _ in range(2)]
        assert [future.result(timeout=20) for future in futures] == [QueueStatus.PUBLISHED] * 2
    session.expire_all()
    assert session.get(PublicationQueueItem, item_id).attempts == 1
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 1


@pytest.mark.parametrize("problem", ["destination_disabled", "real_destination", "not_own_link"])
def test_simulation_does_not_bypass_destination_or_affiliate_ownership(context, problem):
    session, product, settings, clock = context
    _, item = make_offer(context)
    if problem == "not_own_link":
        product.affiliate_link.source = "unverified"
    else:
        destination = session.get(PublicationDestination, item.destination_id)
        if problem == "destination_disabled":
            destination.enabled = False
        else:
            destination.channel = DestinationChannel.TELEGRAM
    result = SimulatedQueueService(settings, clock=lambda: clock[0]).publish(session, item.id)
    assert result.status is (
        QueueStatus.PENDING if problem == "not_own_link" else QueueStatus.FAILED
    )
    assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0
