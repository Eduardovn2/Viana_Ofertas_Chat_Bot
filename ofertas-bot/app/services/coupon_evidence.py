"""Manual, product-specific evidence; unknown/free-shipping data never invents a price."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Coupon,
    CouponDiscountType,
    CouponEvidence,
    CouponEvidenceType,
    CouponValidationState,
    PriceAccess,
    Product,
    SourceType,
)
from app.services.comparison import aware_utc
from app.services.monitoring import _validate_price


def save_manual_evidence(session: Session, product: Product, data, *, now=None) -> CouponEvidence:
    now = aware_utc(now or datetime.now(UTC))
    if data.verified_at is not None and aware_utc(data.verified_at) > now:
        raise ValueError("Verificação não pode estar no futuro.")
    for value in (data.value, data.max_discount):
        _validate_price(value, "cupom")
    if data.minimum_purchase is not None and data.minimum_purchase != 0:
        _validate_price(data.minimum_purchase, "compra mínima")
    if data.type == CouponEvidenceType.PERCENT and data.value is not None and data.value > 100:
        raise ValueError("Cupom percentual deve ser no máximo 100.")
    if data.validation_state == CouponValidationState.CONFIRMED:
        if (
            not data.product_eligibility_confirmed
            or not data.conditions_complete
            or not data.source_reference
            or data.verified_at is None
            or data.expires_at is None
            or data.price_access != PriceAccess.PUBLIC
            or data.type == CouponEvidenceType.UNKNOWN
            or (
                data.type in {CouponEvidenceType.PERCENT, CouponEvidenceType.FIXED}
                and (not data.code or data.value is None)
            )
        ):
            raise ValueError(
                "Confirmação requer evidência operacional do produto, condições completas, referência, verificação e validade."
            )
    evidence = CouponEvidence(
        product_id=product.id,
        platform=product.platform,
        code=data.code,
        type=data.type,
        value=data.value,
        minimum_purchase=data.minimum_purchase,
        max_discount=data.max_discount,
        validation_state=data.validation_state,
        price_access=data.price_access,
        observed_at=now,
        verified_at=data.verified_at,
        expires_at=data.expires_at,
        source_type=SourceType.MANUAL,
        source_reference=data.source_reference,
        conditions={
            "product_eligibility_confirmed": data.product_eligibility_confirmed,
            "conditions_complete": data.conditions_complete,
            "confirmation_level": "MANUAL_PRODUCT_VERIFICATION"
            if data.validation_state == CouponValidationState.CONFIRMED
            else "UNVERIFIED",
        },
    )
    if evidence.expires_at and aware_utc(evidence.expires_at) <= now:
        evidence.validation_state = CouponValidationState.INVALID
    # Free shipping cannot lower the product's monetary price; missing code/value stays evidence.
    if (
        evidence.validation_state == CouponValidationState.CONFIRMED
        and evidence.code
        and evidence.value
        and evidence.type in {CouponEvidenceType.PERCENT, CouponEvidenceType.FIXED}
    ):
        discount_type = (
            CouponDiscountType.PERCENTAGE
            if data.type == CouponEvidenceType.PERCENT
            else CouponDiscountType.FIXED
        )
        coupon = session.scalar(
            select(Coupon)
            .where(Coupon.platform == product.platform, Coupon.code == data.code)
            .with_for_update()
        )
        if coupon is not None:
            same_terms = (
                coupon.source == "MANUAL_PRODUCT_VERIFICATION"
                and coupon.discount_type == discount_type
                and coupon.discount_value == data.value
                and coupon.minimum_purchase == data.minimum_purchase
                and coupon.max_discount == data.max_discount
                and coupon.expires_at == data.expires_at
                and coupon.price_access == PriceAccess.PUBLIC
                and any(item.id == product.id for item in coupon.eligible_products)
            )
            if not same_terms:
                raise ValueError(
                    "Código existente com outra origem, produto ou condições; não sobrescrever evidência anterior."
                )
            if coupon.last_verified_at is None or aware_utc(data.verified_at) > aware_utc(
                coupon.last_verified_at
            ):
                coupon.last_verified_at = data.verified_at
                coupon.validation_state = CouponValidationState.CONFIRMED
        else:
            coupon = Coupon(
                code=data.code,
                platform=product.platform,
                discount_type=discount_type,
                discount_value=data.value,
                minimum_purchase=data.minimum_purchase,
                max_discount=data.max_discount,
                expires_at=data.expires_at,
                last_verified_at=data.verified_at,
                validation_state=CouponValidationState.CONFIRMED,
                price_access=PriceAccess.PUBLIC,
                source="MANUAL_PRODUCT_VERIFICATION",
                eligible_products=[product],
            )
        session.add(coupon)
        session.flush()
        evidence.coupon_id = coupon.id
    session.add(evidence)
    session.flush()
    return evidence
