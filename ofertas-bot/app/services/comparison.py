"""Compare public observations against the preceding comparable history entry."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import Availability, PriceAccess, ProductPriceHistory

CENT = Decimal("0.01")


def valid_money(value: object, *, allow_zero: bool = False) -> bool:
    """Reject nonfinite, out-of-range and fractional-cent monetary data."""
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    if value < 0 or (value == 0 and not allow_zero) or value > Decimal("9999999999.99"):
        return False
    try:
        return value == value.quantize(CENT)
    except InvalidOperation:
        return False


def aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A data deve incluir fuso horário.")
    return value.astimezone(UTC)


def validate_max_age(max_age_seconds: int) -> None:
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
        raise ValueError("A idade máxima deve ser um número inteiro de segundos.")
    if max_age_seconds <= 0:
        raise ValueError("A idade máxima deve ser maior que zero.")


class PriceDirection(StrEnum):
    INITIAL = "INITIAL"
    DROP = "DROP"
    RISE = "RISE"
    UNCHANGED = "UNCHANGED"
    UNAVAILABLE = "UNAVAILABLE"
    EXCLUDED = "EXCLUDED"


@dataclass(frozen=True)
class PriceComparison:
    previous_historical_price: Decimal | None
    current_price: Decimal | None
    direction: PriceDirection
    amount: Decimal | None = None
    percentage: Decimal | None = None

    @property
    def is_opportunity(self) -> bool:
        return self.direction == PriceDirection.DROP


class PriceComparisonService:
    def compare(
        self,
        session: Session,
        observation: ProductPriceHistory,
        *,
        now: datetime,
        max_age_seconds: int = 600,
    ) -> PriceComparison:
        """The store's crossed-out price is never used as historical evidence.

        A baseline may be old: its purpose is recording a real historical change.
        Freshness is required for the current observation used to make an offer.
        Equal timestamps are ordered by their persisted history IDs.
        """
        now = aware_utc(now)
        validate_max_age(max_age_seconds)
        if observation.id is None or observation.product_id is None:
            raise ValueError("A observação deve estar persistida antes da comparação.")
        current = observation.current_price if valid_money(observation.current_price) else None
        if observation.availability != Availability.AVAILABLE or current is None:
            return PriceComparison(None, current, PriceDirection.UNAVAILABLE)
        observed_at = aware_utc(observation.observed_at)
        if (
            observation.price_access != PriceAccess.PUBLIC
            or observation.currency != "BRL"
            or observed_at > now
            or observed_at < now - timedelta(seconds=max_age_seconds)
        ):
            return PriceComparison(None, current, PriceDirection.EXCLUDED)
        previous_rows = session.scalars(
            select(ProductPriceHistory)
            .where(
                ProductPriceHistory.product_id == observation.product_id,
                ProductPriceHistory.source == observation.source,
                ProductPriceHistory.is_simulated == observation.is_simulated,
                ProductPriceHistory.currency == observation.currency,
                ProductPriceHistory.availability == Availability.AVAILABLE,
                ProductPriceHistory.price_access == PriceAccess.PUBLIC,
                or_(
                    ProductPriceHistory.observed_at < observed_at,
                    and_(
                        ProductPriceHistory.observed_at == observed_at,
                        ProductPriceHistory.id < observation.id,
                    ),
                ),
            )
            .order_by(ProductPriceHistory.observed_at.desc(), ProductPriceHistory.id.desc())
        )
        previous = next(
            (row.current_price for row in previous_rows if valid_money(row.current_price)), None
        )
        if previous is None:
            return PriceComparison(None, current, PriceDirection.INITIAL)
        amount = (current - previous).quantize(CENT)
        percentage = (amount * Decimal("100") / previous).quantize(CENT, rounding=ROUND_HALF_UP)
        direction = (
            PriceDirection.DROP
            if amount < 0
            else PriceDirection.RISE
            if amount > 0
            else PriceDirection.UNCHANGED
        )
        return PriceComparison(previous, current, direction, amount, percentage)
