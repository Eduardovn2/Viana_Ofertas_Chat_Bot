"""Administrator-supplied evidence, independent of commercial connectors and jobs."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.integrations.base import PriceSnapshot
from app.integrations.errors import IntegrationError
from app.models import (
    Availability,
    Coupon,
    ManualOfferDetails,
    Offer,
    OfferStatus,
    PriceAccess,
    Product,
    ProductPriceHistory,
    SourceType,
)
from app.services.categories import CategoryService
from app.services.comparison import CENT, PriceComparisonService, aware_utc, valid_money
from app.services.configuration import load_monitoring_settings
from app.services.message_templates import MessageData, render_message, validate_public_url
from app.services.monitoring import record_price
from app.services.offers import has_own_affiliate_link, latest_observation
from app.services.scoring import CouponEvaluation, evaluate_coupon

_MESSAGES = {
    "MANUAL_OFFER_NOT_FOUND": "Oferta manual não encontrada.",
    "MANUAL_PRODUCT_NOT_FOUND": "Produto não encontrado.",
    "MANUAL_INPUT_INVALID": "Informe preço público válido, disponibilidade e verificação explícita pelo responsável.",
    "MANUAL_EVIDENCE_STALE": "A oferta exige nova verificação manual recente das condições.",
    "MANUAL_LINK_REQUIRED": "Cadastre um link de afiliado próprio e operacionalmente verificado.",
    "MANUAL_COUPON_INVALID": "O cupom não possui confirmação pública válida para este produto e preço.",
    "MANUAL_OFFER_INVALID": "A oferta não corresponde à evidência manual atual ou está encerrada.",
    "MULTISTORE_EVIDENCE_INVALID": "A prévia multiloja mudou ou não comprova o candidato e suas condições.",
    "MANUAL_IDENTITY_EVIDENCE_INVALID": "A evidência documental não corresponde ao anúncio e à variante.",
}


def _error(code):
    return IntegrationError(code, _MESSAGES[code])


def _text(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value.strip()) <= maximum
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def _fresh(value, now, settings):
    try:
        value = aware_utc(value)
        if now - timedelta(seconds=settings.price_max_age_seconds) <= value <= now:
            return value
    except (ValueError, TypeError):
        pass
    raise _error("MANUAL_EVIDENCE_STALE")


def _validate_product(product):
    if not _text(product.name, 500):
        raise _error("MANUAL_INPUT_INVALID")
    if not has_own_affiliate_link(product, require_verified=True):
        raise _error("MANUAL_LINK_REQUIRED")


def _coupon(session, product, coupon_id, current_price, now, settings):
    if coupon_id is None:
        return None, None
    if type(coupon_id) is not int or coupon_id <= 0:
        raise _error("MANUAL_COUPON_INVALID")
    coupon = session.scalar(
        select(Coupon).where(Coupon.id == coupon_id).options(selectinload(Coupon.eligible_products))
    )
    evaluation = (
        evaluate_coupon(coupon, product, current_price, now, settings.price_max_age_seconds)
        if coupon is not None
        else None
    )
    if evaluation is None or not _text(coupon.code, 100):
        raise _error("MANUAL_COUPON_INVALID")
    return coupon, evaluation


@dataclass(frozen=True)
class ManualOfferBundle:
    offer: Offer
    product: Product
    history: ProductPriceHistory
    details: ManualOfferDetails
    coupon: Coupon | None = None
    coupon_evaluation: CouponEvaluation | None = None


def create_manual_offer(
    session: Session,
    settings: Settings,
    *,
    product_id: int,
    current_price: Decimal,
    actor: str,
    verified: bool,
    availability: Availability,
    price_access: PriceAccess,
    previous_price: Decimal | None = None,
    coupon_id: int | None = None,
    installment_text: str | None = None,
    image_url: str | None = None,
    verified_at: datetime | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
    multistore_context: dict | None = None,
    documented_identity: dict | None = None,
) -> ManualOfferBundle:
    """Append a real manual observation and offer. Caller owns commit/rollback.

    Every reverification creates new evidence; existing prices and approvals are
    never silently rewritten. No provider, scheduler or publisher is invoked.
    """
    settings = load_monitoring_settings(session, settings)
    now = aware_utc(now or datetime.now(UTC))
    if (
        type(product_id) is not int
        or product_id <= 0
        or verified is not True
        or not _text(actor, 200)
        or not valid_money(current_price)
        or (previous_price is not None and not valid_money(previous_price))
        or availability != Availability.AVAILABLE
        or price_access != PriceAccess.PUBLIC
        or (installment_text is not None and not _text(installment_text, 500))
    ):
        raise _error("MANUAL_INPUT_INVALID")
    observed = _fresh(verified_at if verified_at is not None else now, now, settings)
    deadline = observed + timedelta(seconds=settings.price_max_age_seconds)
    if expires_at is not None:
        try:
            deadline = min(deadline, aware_utc(expires_at))
        except ValueError:
            raise _error("MANUAL_INPUT_INVALID") from None
    if deadline <= now:
        raise _error("MANUAL_EVIDENCE_STALE")
    product = session.scalar(
        select(Product)
        .where(Product.id == product_id)
        .options(selectinload(Product.affiliate_link))
        .with_for_update()
    )
    if product is None:
        raise _error("MANUAL_PRODUCT_NOT_FOUND")
    _validate_product(product)
    from app.services.affiliate_pipeline import validate_owned_link_receipt

    try:
        validate_owned_link_receipt(session, settings, product, now)
    except (ValueError, KeyError, TypeError):
        raise _error("MANUAL_OFFER_INVALID") from None
    identity_document = None
    if documented_identity is not None:
        from app.services.product_documentation import CORE_CONTENT_NOTICE, DocumentedIdentity

        try:
            identity_document = DocumentedIdentity.model_validate(
                documented_identity
            ).validate_product(product, now)
            identity_document.validate_commercial(
                current_price, observed, coupon_id=coupon_id, previous_price=previous_price
            )
            if identity_document.message_scope == "CORE_ATTRIBUTES_ONLY" and image_url is not None:
                raise ValueError(
                    "Descrição limitada sem composição confirmada não admite imagem de equipamentos."
                )
        except (ValueError, TypeError):
            raise _error("MANUAL_IDENTITY_EVIDENCE_INVALID") from None
    binding = None
    if multistore_context is not None:
        from app.services.shopee_commercial_handoff import validate_binding

        try:
            # A seller's other price is never accepted as a temporal baseline.
            if previous_price is not None:
                raise ValueError("Prévia multiloja não fornece preço anterior.")
            binding = validate_binding(
                session,
                multistore_context,
                product,
                current_price,
                observed,
                allow_simulated=settings.app_env == "test",
            )
        except (LookupError, ValueError, TypeError, KeyError):
            raise _error("MULTISTORE_EVIDENCE_INVALID") from None
    latest = latest_observation(session, product.id)
    if latest is not None and aware_utc(latest.observed_at) > observed:
        raise _error("MANUAL_EVIDENCE_STALE")
    coupon, evaluation = _coupon(session, product, coupon_id, current_price, now, settings)
    if coupon is not None:
        deadline = min(
            deadline,
            aware_utc(coupon.expires_at),
            aware_utc(coupon.last_verified_at) + timedelta(seconds=settings.price_max_age_seconds),
        )
        if deadline <= now:
            raise _error("MANUAL_EVIDENCE_STALE")
    try:
        if image_url is not None:
            validate_public_url(image_url)
        render_message(
            MessageData(
                product.name,
                current_price,
                product.affiliate_link.url,
                previous_price=previous_price,
                coupon_text=coupon.code if coupon else None,
                installments_text=installment_text,
                image_url=image_url,
                content_notice=CORE_CONTENT_NOTICE
                if identity_document and identity_document.message_scope == "CORE_ATTRIBUTES_ONLY"
                else None,
            )
        )
    except ValueError:
        raise _error("MANUAL_INPUT_INVALID") from None
    CategoryService().classify(session, product)
    history = record_price(
        session,
        product,
        PriceSnapshot(
            current_price=current_price,
            previous_price=previous_price,
            availability=Availability.AVAILABLE,
            price_access=PriceAccess.PUBLIC,
            source="manual:administrator",
            observed_at=observed,
            is_simulated=False,
            source_type=SourceType.MANUAL,
            installment_text=installment_text,
        ),
        now=now,
    )
    if identity_document is not None:
        history.evidence_metadata = {
            **(history.evidence_metadata or {}),
            "documented_identity": identity_document.model_dump(mode="json"),
        }
    if binding is not None:
        history.evidence_metadata = {
            **(history.evidence_metadata or {}),
            "multistore_context": binding,
            "multistore_registered_variant_id": product.variant_id,
        }
    comparison = PriceComparisonService().compare(
        session,
        history,
        now=now,
        max_age_seconds=settings.price_max_age_seconds,
    )
    discount = (
        max(Decimal("0"), previous_price - current_price) if previous_price is not None else None
    )
    percentage = (
        (discount * 100 / previous_price).quantize(CENT, rounding=ROUND_HALF_UP)
        if discount is not None
        else None
    )
    offer = Offer(
        product_id=product.id,
        price_history_id=history.id,
        coupon_id=coupon_id,
        current_price=current_price,
        previous_price=previous_price,
        historical_price=comparison.previous_historical_price,
        discount_amount=discount,
        discount_percentage=percentage,
        final_price=evaluation.final_price if evaluation is not None else None,
        status=OfferStatus.PENDING_REVIEW,
        expires_at=deadline,
    )
    session.add(offer)
    session.flush()
    details = ManualOfferDetails(
        offer_id=offer.id,
        verified_by=actor.strip(),
        verified_at=observed,
        image_url=image_url,
    )
    session.add(details)
    session.flush()
    return ManualOfferBundle(offer, product, history, details, coupon, evaluation)


def read_manual_offer(session, offer_id, settings, now=None) -> ManualOfferBundle:
    """Revalidate local evidence before preview/approval; never refresh from a store."""
    settings = load_monitoring_settings(session, settings)
    now = aware_utc(now or datetime.now(UTC))
    offer = session.get(Offer, offer_id)
    details = session.get(ManualOfferDetails, offer_id)
    if offer is None or details is None:
        raise _error("MANUAL_OFFER_NOT_FOUND")
    history = session.get(ProductPriceHistory, offer.price_history_id)
    product = session.scalar(
        select(Product)
        .where(Product.id == offer.product_id)
        .options(selectinload(Product.affiliate_link))
        .with_for_update()
    )
    if (
        product is None
        or history is None
        or history.product_id != product.id
        or history.source_type != SourceType.MANUAL
        or history.is_simulated
        or history.source != "manual:administrator"
        or history.currency != "BRL"
        or history.availability != Availability.AVAILABLE
        or history.price_access != PriceAccess.PUBLIC
        or not valid_money(history.current_price)
        or history.current_price != offer.current_price
        or history.previous_price != offer.previous_price
        or (history.previous_price is not None and not valid_money(history.previous_price))
        or not _text(details.verified_by, 200)
        or offer.status not in (OfferStatus.PENDING_REVIEW, OfferStatus.READY, OfferStatus.QUEUED)
    ):
        raise _error("MANUAL_OFFER_INVALID")
    _validate_product(product)
    _fresh(details.verified_at, now, settings)
    from app.services.affiliate_pipeline import validate_owned_link_receipt

    try:
        validate_owned_link_receipt(session, settings, product, now)
    except (ValueError, KeyError, TypeError):
        raise _error("MANUAL_OFFER_INVALID") from None
    _fresh(history.observed_at, now, settings)
    from app.services.official_offer_import import validate_current_import

    try:
        validate_current_import(session, history)
    except ValueError:
        raise _error("MANUAL_EVIDENCE_STALE") from None
    latest = latest_observation(session, product.id)
    if (
        details.verified_at != history.observed_at
        or latest is None
        or latest.id != history.id
        or offer.expires_at is None
        or aware_utc(offer.expires_at) <= now
    ):
        raise _error("MANUAL_EVIDENCE_STALE")
    coupon, evaluation = _coupon(
        session, product, offer.coupon_id, offer.current_price, now, settings
    )
    if offer.final_price != (evaluation.final_price if evaluation else None):
        raise _error("MANUAL_COUPON_INVALID")
    binding = (history.evidence_metadata or {}).get("multistore_context")
    identity_document = (history.evidence_metadata or {}).get("documented_identity")
    if identity_document is not None:
        from app.services.product_documentation import DocumentedIdentity

        try:
            DocumentedIdentity.model_validate(identity_document).validate_product(
                product, now
            ).validate_commercial(
                offer.current_price,
                details.verified_at,
                coupon_id=offer.coupon_id,
                previous_price=offer.previous_price,
            )
        except (ValueError, TypeError):
            raise _error("MANUAL_IDENTITY_EVIDENCE_INVALID") from None
    if binding is not None:
        from app.services.shopee_commercial_handoff import validate_binding

        try:
            if (
                offer.previous_price is not None
                or history.evidence_metadata.get("multistore_registered_variant_id")
                != product.variant_id
            ):
                raise ValueError("Preço anterior não pertence ao contexto multiloja.")
            validate_binding(
                session,
                binding,
                product,
                offer.current_price,
                details.verified_at,
                allow_simulated=settings.app_env == "test",
            )
        except (LookupError, ValueError, TypeError, KeyError):
            raise _error("MULTISTORE_EVIDENCE_INVALID") from None
    return ManualOfferBundle(offer, product, history, details, coupon, evaluation)
