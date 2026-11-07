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


def comparable_context(left, right) -> bool:
    """A quoted range or verified basis cannot silently become another price basis."""
    return all(
        (left.evidence_metadata or {}).get(key) == (right.evidence_metadata or {}).get(key)
        for key in ("price_range", "price_basis", "price_context")
    )


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
    def rank_observation(self, session, observation, *, now):
        """Read-only radar projection; does not change Offer.score or history."""
        from app.integrations.contracts import fingerprint
        from app.models import Product
        from app.services.opportunity_ranking import rank_rows
        from app.services.product_documentation import DocumentedIdentity
        from app.services.ranking_signal_import import load_ranking_signals

        product = session.get(Product, observation.product_id)
        identity = product.external_id
        row = dict(
            identity=identity,
            shop_id=(identity or "").split(":")[0],
            name=product.name,
            source=observation.source,
            source_reference=observation.source_reference,
            announced_price=observation.current_price,
            currency=observation.currency,
            observed_at=observation.observed_at,
            price_status="VALID_ANNOUNCED_PRICE"
            if valid_money(observation.current_price)
            else "UNKNOWN",
            seller=(observation.evidence_metadata or {}).get("seller_information", {}),
        )
        reference = None
        try:
            proof = DocumentedIdentity.model_validate(
                (observation.evidence_metadata or {}).get("documented_identity") or {}
            )
            proof.validate_product(product, now)
            if proof.message_scope == "CORE_ATTRIBUTES_ONLY":
                raise ValueError("Composição limitada não comprova benchmark histórico.")
            comparison = self.compare(session, observation, now=now)
            if (
                observation.is_simulated
                or comparison.previous_historical_price is None
                or not (observation.evidence_metadata or {}).get("price_basis")
            ):
                raise ValueError("Histórico sem contexto documental suficiente.")
            binding = ("identity", "identity_level", "variant_id", "original_url_sha256", "facts")
            current_proof = proof.model_dump(mode="json")
            prior = session.scalars(
                select(ProductPriceHistory)
                .where(
                    ProductPriceHistory.product_id == observation.product_id,
                    ProductPriceHistory.observed_at < observation.observed_at,
                    ProductPriceHistory.is_simulated.is_(False),
                    ProductPriceHistory.source == observation.source,
                    ProductPriceHistory.source_type == observation.source_type,
                    ProductPriceHistory.currency == observation.currency,
                    ProductPriceHistory.availability == Availability.AVAILABLE,
                    ProductPriceHistory.price_access == PriceAccess.PUBLIC,
                )
                .order_by(ProductPriceHistory.observed_at.desc(), ProductPriceHistory.id.desc())
            )
            for old in prior:
                if (
                    not comparable_context(old, observation)
                    or old.current_price != comparison.previous_historical_price
                ):
                    continue
                old_proof = DocumentedIdentity.model_validate(
                    (old.evidence_metadata or {}).get("documented_identity") or {}
                )
                old_proof.validate_product(product, now)
                data = old_proof.model_dump(mode="json")
                if any(current_proof.get(key) != data.get(key) for key in binding):
                    break
                reference = dict(
                    kind="SAME_LISTING_HISTORY",
                    identity=identity,
                    same_documented_context=True,
                    price=str(old.current_price),
                    currency="BRL",
                    observed_at=old.observed_at.isoformat(),
                    evidence_reference=f"history:{old.id}",
                    source=old.source,
                    price_basis=(old.evidence_metadata or {})["price_basis"],
                    evidence_sha256=fingerprint(old.evidence_metadata),
                )
                break
        except (ValueError, TypeError, KeyError):
            reference = None
        if reference:
            row["historical_price_reference"] = reference
        signals = load_ranking_signals(session, [identity], now=now)
        signals.setdefault(identity, {}).update(
            (observation.evidence_metadata or {}).get("documented_ranking_signals") or {}
        )
        ranked = rank_rows([row], now=now, signals_by_identity=signals)[0]
        return dict(
            product_id=product.id,
            history_id=observation.id,
            product=row,
            ranking=ranked["intelligent_ranking"],
            history_writes=0,
            publication_authorized=False,
        )

    @staticmethod
    def amounts(previous, current):
        """Shared arithmetic; callers remain responsible for commercial verification."""
        if current is None:
            return PriceComparison(None, None, PriceDirection.UNAVAILABLE)
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

    def reported_quotes(self, session, observation):
        """Informational change only. Never establishes stock/public access or eligibility."""
        current = observation.current_price if valid_money(observation.current_price) else None
        rows = session.scalars(
            select(ProductPriceHistory)
            .where(
                ProductPriceHistory.product_id == observation.product_id,
                ProductPriceHistory.source == observation.source,
                ProductPriceHistory.source_type == observation.source_type,
                ProductPriceHistory.is_simulated == observation.is_simulated,
                ProductPriceHistory.currency == observation.currency,
                ProductPriceHistory.availability == observation.availability,
                ProductPriceHistory.price_access == observation.price_access,
                ProductPriceHistory.observed_at < observation.observed_at,
            )
            .order_by(ProductPriceHistory.observed_at.desc(), ProductPriceHistory.id.desc())
        )
        previous = None
        for row in rows:
            if not comparable_context(row, observation):
                continue
            # An intervening unavailable quote breaks comparability rather than being skipped.
            previous = row.current_price if valid_money(row.current_price) else None
            break
        return self.amounts(previous, current)

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
        if (observation.evidence_metadata or {}).get("observation_only") is True:
            return PriceComparison(None, current, PriceDirection.EXCLUDED)
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
                ProductPriceHistory.source_type == observation.source_type,
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
            (
                row.current_price
                for row in previous_rows
                if comparable_context(row, observation) and valid_money(row.current_price)
            ),
            None,
        )
        return self.amounts(previous, current)
