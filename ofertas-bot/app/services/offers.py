"""Create opportunities from fresh public evidence; transaction ownership stays outside."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.models import (
    Availability,
    Coupon,
    DestinationChannel,
    Offer,
    OfferStatus,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationQueueItem,
    QueueStatus,
)
from app.services.affiliate_links import validate_link_for_product
from app.services.categories import CategoryService
from app.services.comparison import (
    CENT,
    PriceComparisonService,
    PriceDirection,
    aware_utc,
    valid_money,
)
from app.services.scoring import OfferScoringService, best_coupon, evaluate_coupon

_ACTIVE_OFFERS = (
    OfferStatus.PENDING,
    OfferStatus.PENDING_REVIEW,
    OfferStatus.READY,
    OfferStatus.QUEUED,
)
_BEST_COUPON = object()


@dataclass(frozen=True)
class OfferAssessment:
    current_price: Decimal
    previous_price: Decimal | None
    historical_price: Decimal | None
    discount_amount: Decimal
    discount_percentage: Decimal
    final_price: Decimal | None
    coupon_id: int | None
    score: Decimal
    expires_at: datetime


def latest_observation(session: Session, product_id: int) -> ProductPriceHistory | None:
    return session.scalar(
        select(ProductPriceHistory)
        .where(ProductPriceHistory.product_id == product_id)
        .order_by(ProductPriceHistory.observed_at.desc(), ProductPriceHistory.id.desc())
        .limit(1)
    )


def has_own_affiliate_link(product: Product, *, require_verified: bool = False) -> bool:
    link = product.affiliate_link
    if link is None or link.source not in {
        "manual_official",
        "MANUAL_LINKBUILDER",
        "MANUAL_SHOPEE",
        "MANUAL_LINKBUILDER_VERIFIED",
        "MANUAL_SHOPEE_VERIFIED",
        "SHOPEE_OFFICIAL_API",
    }:
        return False
    if require_verified and link.source not in {
        "MANUAL_LINKBUILDER_VERIFIED",
        "MANUAL_SHOPEE_VERIFIED",
        "SHOPEE_OFFICIAL_API",
    }:
        return False
    try:
        validate_link_for_product(product, link.url)
    except ValueError:
        return False
    return True


def _carried_historical_price(session: Session, observation: ProductPriceHistory) -> Decimal | None:
    """Retain a recorded drop while a fresh comparable price stays at or below it."""
    candidates = session.execute(
        select(Offer, ProductPriceHistory)
        .join(ProductPriceHistory, Offer.price_history_id == ProductPriceHistory.id)
        .where(
            Offer.product_id == observation.product_id,
            Offer.historical_price > Offer.current_price,
            Offer.current_price >= observation.current_price,
            Offer.status.not_in((OfferStatus.FAILED, OfferStatus.REJECTED)),
            ProductPriceHistory.source == observation.source,
            ProductPriceHistory.is_simulated == observation.is_simulated,
            ProductPriceHistory.currency == "BRL",
            ProductPriceHistory.availability == Availability.AVAILABLE,
            ProductPriceHistory.price_access == PriceAccess.PUBLIC,
            or_(
                ProductPriceHistory.observed_at < observation.observed_at,
                and_(
                    ProductPriceHistory.observed_at == observation.observed_at,
                    ProductPriceHistory.id < observation.id,
                ),
            ),
        )
        .order_by(ProductPriceHistory.observed_at.desc(), ProductPriceHistory.id.desc())
    )
    for prior_offer, prior_observation in candidates:
        reference = prior_offer.historical_price
        if not valid_money(reference) or reference <= observation.current_price:
            continue
        # Require actual comparable history behind the stored reference; a store's
        # crossed-out price and a coupon alone cannot establish this evidence.
        evidence = session.scalar(
            select(ProductPriceHistory.id)
            .where(
                ProductPriceHistory.product_id == observation.product_id,
                ProductPriceHistory.source == observation.source,
                ProductPriceHistory.is_simulated == observation.is_simulated,
                ProductPriceHistory.currency == "BRL",
                ProductPriceHistory.availability == Availability.AVAILABLE,
                ProductPriceHistory.price_access == PriceAccess.PUBLIC,
                ProductPriceHistory.current_price == reference,
                or_(
                    ProductPriceHistory.observed_at < prior_observation.observed_at,
                    and_(
                        ProductPriceHistory.observed_at == prior_observation.observed_at,
                        ProductPriceHistory.id < prior_observation.id,
                    ),
                ),
            )
            .limit(1)
        )
        if evidence is not None:
            return reference
    return None


def assess_observation(
    session: Session,
    product: Product,
    observation: ProductPriceHistory,
    settings: Settings,
    now: datetime,
    *,
    coupon_id: object = _BEST_COUPON,
) -> OfferAssessment | None:
    """Keep historical and declared store prices separate when computing a discount."""
    now = aware_utc(now)
    if not product.monitoring_enabled or observation.product_id != product.id:
        return None
    age = settings.price_max_age_seconds
    comparison = PriceComparisonService().compare(
        session, observation, now=now, max_age_seconds=age
    )
    if comparison.direction in (PriceDirection.UNAVAILABLE, PriceDirection.EXCLUDED):
        return None
    current = comparison.current_price
    if current is None:
        return None
    coupon = None
    coupon_row = None
    if coupon_id is _BEST_COUPON:
        coupon = best_coupon(session, product, current, now, age)
        if coupon is not None:
            coupon_row = session.get(Coupon, coupon.coupon_id)
    elif coupon_id is not None:
        coupon_row = session.scalar(
            select(Coupon)
            .where(Coupon.id == coupon_id)
            .with_for_update()
            .options(selectinload(Coupon.eligible_products))
            .execution_options(populate_existing=True)
        )
        if coupon_row is None:
            return None
        coupon = evaluate_coupon(coupon_row, product, current, now, age)
        if coupon is None:
            return None
    carried_reference = (
        _carried_historical_price(session, observation)
        if comparison.direction != PriceDirection.DROP
        else None
    )
    historical_drop = comparison.direction == PriceDirection.DROP or carried_reference is not None
    previous = observation.previous_price if valid_money(observation.previous_price) else None
    store_drop = previous is not None and previous > current
    if not historical_drop and not store_drop and coupon is None:
        return None
    reference = (
        carried_reference or comparison.previous_historical_price
        if historical_drop
        else previous
        if store_drop
        else current
    )
    effective = coupon.final_price if coupon is not None else current
    amount = (reference - effective).quantize(CENT)
    percentage = (amount * Decimal("100") / reference).quantize(CENT, rounding=ROUND_HALF_UP)
    expires_at = aware_utc(observation.observed_at) + timedelta(seconds=age)
    if coupon_row is not None:
        expires_at = min(
            expires_at,
            aware_utc(coupon_row.expires_at),
            aware_utc(coupon_row.last_verified_at) + timedelta(seconds=age),
        )
    score = OfferScoringService(settings).score(
        historical_drop=historical_drop,
        confirmed_coupon=coupon is not None,
        priority=product.priority,
        discount_percent=percentage,
        available=True,
    )
    return OfferAssessment(
        current,
        previous,
        carried_reference or comparison.previous_historical_price,
        amount,
        percentage,
        coupon.final_price if coupon is not None else None,
        coupon.coupon_id if coupon is not None else None,
        score,
        expires_at,
    )


def expire_product_offers(
    session: Session,
    product_id: int,
    *,
    except_history_id: int | None = None,
    expire_queue: bool = True,
) -> None:
    offers = select(Offer).where(Offer.product_id == product_id, Offer.status.in_(_ACTIVE_OFFERS))
    queue = select(PublicationQueueItem).where(
        PublicationQueueItem.product_id == product_id, PublicationQueueItem.active_slot == 1
    )
    if except_history_id is not None:
        offers = offers.where(Offer.price_history_id != except_history_id)
        queue = queue.where(
            PublicationQueueItem.offer_id.in_(
                select(Offer.id).where(Offer.price_history_id != except_history_id)
            )
        )
    for offer in session.scalars(offers):
        offer.status = OfferStatus.EXPIRED
    if expire_queue:
        for item in session.scalars(queue):
            item.status = QueueStatus.EXPIRED
            item.active_slot = None
            item.last_error = "Oferta substituída, indisponível ou sem dados públicos atuais."
    session.flush()


class OfferEngine:
    def __init__(self, settings: Settings, clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))

    def process(
        self, session: Session, product: Product, observation: ProductPriceHistory
    ) -> Offer | None:
        now = aware_utc(self.clock())
        if observation.id is None or product.id is None or observation.product_id != product.id:
            raise ValueError("A observação persistida deve pertencer ao produto informado.")
        session.flush()
        existing = session.scalar(select(Offer).where(Offer.price_history_id == observation.id))
        latest = latest_observation(session, product.id)
        if latest is None or latest.id != observation.id:
            return existing
        CategoryService().classify(session, product)
        assessment = assess_observation(session, product, observation, self.settings, now)
        if assessment is None:
            expire_product_offers(session, product.id)
            return None
        expire_product_offers(
            session, product.id, except_history_id=observation.id, expire_queue=False
        )
        if existing is not None and existing.status in (
            OfferStatus.PUBLISHED,
            OfferStatus.EXPIRED,
            OfferStatus.FAILED,
            OfferStatus.REJECTED,
        ):
            return existing
        offer = existing or Offer(product_id=product.id, price_history_id=observation.id)
        for field in OfferAssessment.__dataclass_fields__:
            setattr(offer, field, getattr(assessment, field))
        offer.status = (
            OfferStatus.READY
            if has_own_affiliate_link(product, require_verified=not observation.is_simulated)
            else OfferStatus.PENDING
        )
        session.add(offer)
        session.flush()
        # Local import keeps the revalidation helper available to the queue without a cycle.
        from app.services.publication_queue import SimulatedQueueService

        queue_service = SimulatedQueueService(self.settings, self.clock)
        destinations = session.scalars(
            select(PublicationDestination).where(
                PublicationDestination.channel == DestinationChannel.SIMULATED,
                PublicationDestination.enabled.is_(True),
            )
        ).all()
        if not destinations:
            destinations = [queue_service.get_or_create_destination(session)]
        queued = [
            queue_service.enqueue(session, offer, destination) for destination in destinations
        ]
        if not any(item is not None for item in queued):
            expire_product_offers(session, product.id, except_history_id=observation.id)
        return offer
