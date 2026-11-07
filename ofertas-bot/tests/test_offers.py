from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.base import PriceSnapshot
from app.models import (
    Availability,
    Base,
    CategoryMode,
    Coupon,
    CouponDiscountType,
    CouponValidationState,
    DestinationChannel,
    Offer,
    OfferStatus,
    Platform,
    PriceAccess,
    Product,
    PublicationDestination,
    PublicationQueueItem,
    QueueStatus,
)
from app.services.comparison import PriceComparisonService, PriceDirection
from app.services.monitoring import record_price
from app.services.offers import OfferEngine
from app.services.scoring import OfferScoringService, best_coupon, evaluate_coupon

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        # Phase 2.1 requires an explicit routing choice before queueing.
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
        session.flush()
        yield session
    engine.dispose()


@pytest.fixture
def settings():
    return Settings(_env_file=None, scheduler_enabled=False)


@pytest.fixture
def product(session):
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
    return product


def observe(session, product, price, *, at=NOW, previous=None, **changes):
    snapshot = PriceSnapshot(
        current_price=Decimal(price) if price is not None else None,
        previous_price=Decimal(previous) if previous is not None else None,
        availability=changes.pop("availability", Availability.AVAILABLE),
        price_access=changes.pop("price_access", PriceAccess.PUBLIC),
        source=changes.pop("source", "simulated"),
        is_simulated=changes.pop("is_simulated", True),
        observed_at=at,
        **changes,
    )
    return record_price(session, product, snapshot, now=at)


@pytest.fixture
def coupon(session, product):
    coupon = Coupon(
        code="SIMULADO10",
        platform=product.platform,
        discount_type=CouponDiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        validation_state=CouponValidationState.CONFIRMED,
        price_access=PriceAccess.PUBLIC,
        last_verified_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        eligible_products=[product],
        source="simulated",
    )
    session.add(coupon)
    session.flush()
    return coupon


@pytest.mark.parametrize(
    ("price", "direction", "amount", "percentage"),
    [
        ("80", PriceDirection.DROP, "-20.00", "-20.00"),
        ("120", PriceDirection.RISE, "20.00", "20.00"),
        ("100", PriceDirection.UNCHANGED, "0.00", "0.00"),
    ],
)
def test_comparison_signed_changes(session, product, price, direction, amount, percentage):
    observe(session, product, "100", at=NOW - timedelta(days=30), previous="200")
    current = observe(session, product, price, previous="999")
    result = PriceComparisonService().compare(session, current, now=NOW)
    assert result.direction is direction
    assert result.previous_historical_price == Decimal("100")
    assert result.amount == Decimal(amount)
    assert result.percentage == Decimal(percentage)


def test_decimal_percentage_and_equal_timestamp_order(session, product):
    observe(session, product, "3.00")
    observation = observe(session, product, "2.99")
    comparison = PriceComparisonService().compare(session, observation, now=NOW)
    assert comparison.direction is PriceDirection.DROP
    assert comparison.amount == Decimal("-0.01")
    assert comparison.percentage == Decimal("-0.33")


def test_comparison_ignores_exclusive_different_source_and_mode(session, product):
    observe(session, product, "100", at=NOW - timedelta(minutes=5))
    observe(
        session, product, "200", at=NOW - timedelta(minutes=4), price_access=PriceAccess.EXCLUSIVE
    )
    observe(session, product, "300", at=NOW - timedelta(minutes=3), source="other")
    observe(session, product, "400", at=NOW - timedelta(minutes=2), is_simulated=False)
    current = observe(session, product, "80")
    assert PriceComparisonService().compare(
        session, current, now=NOW
    ).previous_historical_price == Decimal("100")


@pytest.mark.parametrize(
    "changes",
    [
        {"at": NOW + timedelta(seconds=1)},
        {"at": NOW - timedelta(seconds=601)},
        {"price_access": PriceAccess.EXCLUSIVE},
    ],
)
def test_stale_future_and_exclusive_prices_are_excluded(session, product, changes):
    observation = observe(session, product, "80", **changes)
    assert (
        PriceComparisonService().compare(session, observation, now=NOW).direction
        is PriceDirection.EXCLUDED
    )


def test_absent_price_is_unavailable_and_priority_does_not_invent_offer(session, product, settings):
    product.priority = True
    engine = OfferEngine(settings, clock=lambda: NOW)
    assert engine.process(session, product, observe(session, product, None)) is None
    assert engine.process(session, product, observe(session, product, "80")) is None
    assert session.scalar(select(func.count()).select_from(Offer)) == 0


def test_history_drop_creates_offer_without_coupon_and_keeps_store_price_separate(
    session, product, settings
):
    observe(session, product, "100", at=NOW - timedelta(minutes=5))
    current = observe(session, product, "80", previous="120")
    offer = OfferEngine(settings, clock=lambda: NOW).process(session, product, current)
    assert offer.historical_price == Decimal("100")
    assert offer.previous_price == Decimal("120")
    assert offer.current_price == Decimal("80")
    assert offer.final_price is None
    assert offer.coupon_id is None
    assert offer.discount_amount == Decimal("20")
    assert offer.discount_percentage == Decimal("20")
    assert offer.score == Decimal("65")
    assert offer.status is OfferStatus.PENDING
    assert session.scalar(select(PublicationQueueItem)).status is QueueStatus.PENDING


def test_store_declared_discount_is_not_historical_evidence(session, product, settings):
    current = observe(session, product, "100", previous="150")
    offer = OfferEngine(settings, clock=lambda: NOW).process(session, product, current)
    assert offer.historical_price is None
    assert offer.previous_price == Decimal("150")
    assert offer.discount_percentage == Decimal("33.33")
    assert offer.final_price is None


def test_coupon_only_offer_requires_a_calculable_verified_coupon(
    session, product, coupon, settings
):
    offer = OfferEngine(settings, clock=lambda: NOW).process(
        session, product, observe(session, product, "100")
    )
    assert offer.final_price == Decimal("90.00")
    assert offer.discount_amount == Decimal("10")
    assert offer.coupon_id == coupon.id
    assert offer.historical_price is None
    assert offer.previous_price is None
    assert offer.score == Decimal("45")


@pytest.mark.parametrize(
    "changes",
    [
        {"validation_state": CouponValidationState.UNVERIFIED},
        {"validation_state": CouponValidationState.INVALID},
        {"price_access": PriceAccess.EXCLUSIVE},
        {"platform": Platform.SHOPEE},
        {"expires_at": None},
        {"expires_at": NOW},
        {"last_verified_at": NOW - timedelta(seconds=601)},
        {"last_verified_at": NOW + timedelta(seconds=1)},
        {"eligible_products": []},
        {"minimum_purchase": Decimal("101")},
        {"restrictions": "Somente para novos compradores"},
        {"discount_value": Decimal("100")},
        {"discount_type": CouponDiscountType.FIXED, "discount_value": Decimal("101")},
    ],
)
def test_uncertain_inapplicable_or_zero_final_coupon_is_ignored(coupon, product, changes):
    for field, value in changes.items():
        setattr(coupon, field, value)
    assert evaluate_coupon(coupon, product, Decimal("100"), NOW) is None


def test_coupon_cap_fixed_amount_and_cent_rounding(coupon, product):
    coupon.max_discount = Decimal("5")
    assert evaluate_coupon(coupon, product, Decimal("99.99"), NOW).final_price == Decimal("94.99")
    coupon.max_discount = None
    assert evaluate_coupon(coupon, product, Decimal("99.99"), NOW).final_price == Decimal("89.99")
    coupon.discount_type = CouponDiscountType.FIXED
    coupon.discount_value = Decimal("7.25")
    assert evaluate_coupon(coupon, product, Decimal("99.99"), NOW).final_price == Decimal("92.74")


def test_best_coupon_uses_smallest_verified_price(session, product, coupon):
    other = Coupon(
        code="SIMULADO20",
        platform=product.platform,
        discount_type=CouponDiscountType.FIXED,
        discount_value=Decimal("20"),
        validation_state=CouponValidationState.CONFIRMED,
        price_access=PriceAccess.PUBLIC,
        last_verified_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        eligible_products=[product],
    )
    session.add(other)
    session.flush()
    assert best_coupon(session, product, Decimal("100"), NOW).coupon_id == other.id


def test_scoring_uses_configured_decimal_weights(settings):
    settings.score_price_drop = Decimal("1.50")
    settings.score_confirmed_coupon = Decimal("2.25")
    settings.score_priority = Decimal("3")
    settings.score_discount_percent = Decimal("0.50")
    settings.score_availability = Decimal("4")
    assert OfferScoringService(settings).score(
        historical_drop=True,
        confirmed_coupon=True,
        priority=True,
        discount_percent=Decimal("12.50"),
        available=True,
    ) == Decimal("17.00")


def test_engine_is_idempotent_for_same_observation(session, product, settings):
    current = observe(session, product, "80", previous="100")
    engine = OfferEngine(settings, clock=lambda: NOW)
    first = engine.process(session, product, current)
    assert engine.process(session, product, current).id == first.id
    assert session.scalar(select(func.count()).select_from(Offer)) == 1
    assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 1


@pytest.mark.parametrize(
    "changes",
    [{"price_access": PriceAccess.EXCLUSIVE}, {"availability": Availability.UNAVAILABLE}],
)
def test_new_exclusive_or_unavailable_observation_expires_old_opportunities(
    session, product, settings, changes
):
    engine = OfferEngine(settings, clock=lambda: NOW)
    first = engine.process(session, product, observe(session, product, "80", previous="100"))
    item = session.scalar(select(PublicationQueueItem))
    current = observe(session, product, "70", **changes)
    assert engine.process(session, product, current) is None
    assert first.status is OfferStatus.EXPIRED
    assert item.status is QueueStatus.EXPIRED
    assert item.active_slot is None
