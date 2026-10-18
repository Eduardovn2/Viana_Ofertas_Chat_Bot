"""Conservative coupon applicability and configurable deterministic offer scores."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.models import Coupon, CouponDiscountType, CouponValidationState, PriceAccess, Product
from app.services.comparison import CENT, aware_utc, valid_money, validate_max_age


@dataclass(frozen=True)
class CouponEvaluation:
    coupon_id: int
    final_price: Decimal
    discount_amount: Decimal
    discount_percent: Decimal


def evaluate_coupon(
    coupon: Coupon,
    product: Product,
    current_price: Decimal,
    now: datetime,
    max_age_seconds: int = 600,
) -> CouponEvaluation | None:
    """Apply one confirmed public coupon only when all known rules are satisfied."""
    now = aware_utc(now)
    validate_max_age(max_age_seconds)
    if (
        not valid_money(current_price)
        or coupon.id is None
        or product.id is None
        or coupon.platform != product.platform
        or coupon.validation_state != CouponValidationState.CONFIRMED
        or coupon.price_access != PriceAccess.PUBLIC
        or coupon.last_verified_at is None
        or coupon.expires_at is None
        or bool(coupon.restrictions and coupon.restrictions.strip())
        or not any(eligible.id == product.id for eligible in coupon.eligible_products)
        or not valid_money(coupon.discount_value)
    ):
        return None
    try:
        verified_at = aware_utc(coupon.last_verified_at)
        expires_at = aware_utc(coupon.expires_at) if coupon.expires_at is not None else None
    except ValueError:
        return None
    if (
        verified_at > now
        or verified_at < now - timedelta(seconds=max_age_seconds)
        or (expires_at is not None and expires_at <= now)
    ):
        return None
    if coupon.minimum_purchase is not None and (
        not valid_money(coupon.minimum_purchase, allow_zero=True)
        or current_price < coupon.minimum_purchase
    ):
        return None
    if coupon.max_discount is not None and not valid_money(coupon.max_discount):
        return None
    if coupon.discount_type == CouponDiscountType.PERCENTAGE:
        if coupon.discount_value > Decimal("100"):
            return None
        discount = current_price * coupon.discount_value / Decimal("100")
    elif coupon.discount_type == CouponDiscountType.FIXED:
        discount = coupon.discount_value
    else:
        return None
    if coupon.max_discount is not None:
        discount = min(discount, coupon.max_discount)
    discount = discount.quantize(CENT, rounding=ROUND_HALF_UP)
    final_price = current_price - discount
    if discount <= 0 or not valid_money(final_price):
        return None
    discount_percent = (discount * Decimal("100") / current_price).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    return CouponEvaluation(coupon.id, final_price, discount, discount_percent)


def best_coupon(
    session: Session,
    product: Product,
    current_price: Decimal,
    now: datetime,
    max_age_seconds: int = 600,
) -> CouponEvaluation | None:
    """Choose the smallest resulting price, breaking ties by coupon ID."""
    now = aware_utc(now)
    validate_max_age(max_age_seconds)
    coupons = session.scalars(
        select(Coupon)
        .where(
            Coupon.platform == product.platform,
            Coupon.validation_state == CouponValidationState.CONFIRMED,
            Coupon.price_access == PriceAccess.PUBLIC,
            Coupon.eligible_products.any(Product.id == product.id),
        )
        .options(selectinload(Coupon.eligible_products))
        .order_by(Coupon.id)
    )
    evaluations = (
        evaluate_coupon(coupon, product, current_price, now, max_age_seconds) for coupon in coupons
    )
    return min(
        (evaluation for evaluation in evaluations if evaluation is not None),
        key=lambda evaluation: (evaluation.final_price, evaluation.coupon_id),
        default=None,
    )


class OfferScoringService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def score(
        self,
        *,
        historical_drop: bool,
        confirmed_coupon: bool,
        priority: bool,
        discount_percent: Decimal,
        available: bool,
    ) -> Decimal:
        if (
            not isinstance(discount_percent, Decimal)
            or not discount_percent.is_finite()
            or not Decimal("0") <= discount_percent <= Decimal("100")
        ):
            raise ValueError("O percentual de desconto deve estar entre zero e cem.")
        criteria = (
            ("score_price_drop", Decimal("40"), Decimal(int(historical_drop))),
            ("score_confirmed_coupon", Decimal("30"), Decimal(int(confirmed_coupon))),
            ("score_priority", Decimal("20"), Decimal(int(priority))),
            ("score_discount_percent", Decimal("1"), discount_percent),
            ("score_availability", Decimal("5"), Decimal(int(available))),
        )
        score = Decimal("0")
        for setting_name, default, factor in criteria:
            weight = Decimal(str(getattr(self.settings, setting_name, default)))
            if not weight.is_finite() or weight < 0:
                raise ValueError("Os pesos da pontuação devem ser finitos e não negativos.")
            score += weight * factor
        return score.quantize(CENT, rounding=ROUND_HALF_UP)
