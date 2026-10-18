"""Validate and persist price observations without committing transactions."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.base import PriceProvider, PriceSnapshot
from app.integrations.contracts import ProductSnapshot
from app.models import Availability, PriceAccess, Product, ProductPriceHistory, SourceType


def _validate_price(value: Decimal | None, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field} deve ser um Decimal finito e maior que zero.")
    if value > Decimal("9999999999.99"):
        raise ValueError(f"{field} excede o limite de 9.999.999.999,99 BRL.")
    try:
        in_cents = value.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"{field} não pode ser representado em centavos.") from exc
    if value != in_cents:
        raise ValueError(
            f"{field} deve ter no máximo duas casas decimais; não há arredondamento automático."
        )
    return in_cents


def record_price(
    session: Session, product: Product, snapshot: PriceSnapshot, *, now: datetime | None = None
) -> ProductPriceHistory:
    """Preserve the provider's observation, including absent or exclusive prices."""
    current_price = _validate_price(snapshot.current_price, "current_price")
    previous_price = _validate_price(snapshot.previous_price, "previous_price")
    if not isinstance(snapshot.availability, Availability):
        raise ValueError("Disponibilidade inválida.")
    if not isinstance(snapshot.price_access, PriceAccess):
        raise ValueError("Tipo de acesso ao preço inválido.")
    if not isinstance(snapshot.source, str) or not snapshot.source.strip():
        raise ValueError("A fonte da observação é obrigatória.")
    if len(snapshot.source.strip()) > 100:
        raise ValueError("A fonte da observação deve ter no máximo 100 caracteres.")
    if any(ord(char) < 32 or ord(char) == 127 for char in snapshot.source):
        raise ValueError("A fonte da observação contém caracteres de controle.")
    if snapshot.currency != "BRL":
        raise ValueError("A moeda suportada nesta fase é BRL.")
    if not isinstance(snapshot.is_simulated, bool):
        raise ValueError("O indicador de simulação deve ser booleano.")
    for text in (snapshot.installment_text, snapshot.shipping_text):
        if text is not None and (not isinstance(text, str) or len(text) > 500):
            raise ValueError(
                "Os textos de parcelamento e frete devem ter no máximo 500 caracteres."
            )
    if (
        not isinstance(snapshot.observed_at, datetime)
        or snapshot.observed_at.tzinfo is None
        or snapshot.observed_at.utcoffset() is None
    ):
        raise ValueError("A data da observação deve incluir fuso horário.")
    observed_at = snapshot.observed_at.astimezone(UTC)
    if observed_at > (now or datetime.now(UTC)) + timedelta(seconds=60):
        raise ValueError("A data da observação não pode estar no futuro além de 60 segundos.")
    if product.id is None:
        raise ValueError("O produto deve estar persistido antes de registrar preços.")

    source_type = (
        SourceType.SIMULATED if snapshot.is_simulated else SourceType(snapshot.source_type)
    )
    reference = snapshot.source_reference
    if reference is not None:
        parsed = urlsplit(reference)
        is_shopee_reference = re.fullmatch(
            r"productOfferV2:[1-9][0-9]{0,18}:[1-9][0-9]{0,18}", reference
        )
        safe_context_query = (
            parsed.hostname == "api.mercadolibre.com"
            and parsed.query == "context=channel_marketplace"
        )
        if not is_shopee_reference and (
            len(reference) > 500
            or parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or (parsed.query and not safe_context_query)
            or parsed.fragment
            or parsed.port not in (None, 443)
            or parsed.hostname
            not in {
                "api.mercadolibre.com",
                "open-api.affiliate.shopee.com.br",
                "developers.mercadolivre.com.br",
                "www.mercadolivre.com.br",
                "help.shopee.com.br",
            }
        ):
            raise ValueError("Referência de evidência deve ser oficial e omitir segredos.")
    raw_fingerprint = snapshot.raw_fingerprint
    if raw_fingerprint is not None and not re.fullmatch(r"[a-f0-9]{64}", raw_fingerprint):
        raise ValueError("Fingerprint de evidência inválido.")
    metadata = _safe_evidence(snapshot.evidence_metadata)
    if isinstance(snapshot, ProductSnapshot):
        metadata.update(
            _safe_evidence(
                {
                    key: getattr(snapshot, key)
                    for key in (
                        "category_external_id",
                        "category_external_name",
                        "image_url",
                        "seller_id",
                    )
                }
            )
        )
    if raw_fingerprint:
        existing = session.scalar(
            select(ProductPriceHistory).where(
                ProductPriceHistory.product_id == product.id,
                ProductPriceHistory.source == snapshot.source.strip(),
                ProductPriceHistory.raw_fingerprint == raw_fingerprint,
                ProductPriceHistory.observed_at == observed_at,
            )
        )
        if existing is not None:
            return existing

    history = ProductPriceHistory(
        product_id=product.id,
        current_price=current_price,
        previous_price=previous_price,
        availability=snapshot.availability,
        price_access=snapshot.price_access,
        source=snapshot.source.strip(),
        observed_at=observed_at,
        is_simulated=snapshot.is_simulated,
        currency=snapshot.currency,
        installment_text=snapshot.installment_text,
        shipping_text=snapshot.shipping_text,
        source_type=source_type,
        source_reference=reference,
        raw_fingerprint=raw_fingerprint,
        evidence_metadata=metadata,
    )
    session.add(history)
    session.flush()
    return history


def collect_price(
    session: Session, product: Product, provider: PriceProvider, *, now: datetime | None = None
) -> ProductPriceHistory:
    if not product.monitoring_enabled:
        raise ValueError("O monitoramento deste produto está desativado.")
    snapshot = provider.fetch(product)
    if isinstance(snapshot, ProductSnapshot):
        from app.integrations.errors import InvalidPayload

        try:
            apply_product_snapshot(product, snapshot)
            return record_price(session, product, snapshot, now=now)
        except ValueError:
            raise InvalidPayload("Snapshot oficial inválido; preço anterior preservado.") from None
    return record_price(session, product, snapshot, now=now)


_EVIDENCE_FIELDS = {
    "category_external_id",
    "category_external_name",
    "category_external_ids",
    "image_url",
    "seller_id",
    "price_id",
    "price_contexts",
    "conditions_known",
    "commission_rate",
    "commission",
    "variant_unresolved",
    "price_range",
    "price_min",
    "price_max",
    "context_restrictions",
    "category_ids",
    "price_validation",
    "variant_status",
    "request_context",
    "price_access_reason",
    "variant_resolved",
    "quantity_is_referential",
    "commission_amount",
    "source_name",
}


def _safe_evidence(metadata: dict) -> dict:
    """Persist only small public normalized fields; no arbitrary upstream payloads."""
    if not isinstance(metadata, dict):
        raise ValueError("Metadados de evidência inválidos.")
    clean = {}
    for key, value in metadata.items():
        if key not in _EVIDENCE_FIELDS:
            continue
        if isinstance(value, Decimal):
            value = str(value)
        if isinstance(value, str):
            if len(value) > (2048 if key == "image_url" else 500) or any(
                ord(char) < 32 for char in value
            ):
                raise ValueError("Metadado público inválido.")
        elif isinstance(value, list):
            if len(value) > 50 or any(
                not isinstance(item, (str, int)) or len(str(item)) > 100 for item in value
            ):
                raise ValueError("Lista de evidência inválida.")
        elif value is not None and not isinstance(value, (bool, int)):
            continue
        clean[key] = value
    return clean


def apply_product_snapshot(product: Product, snapshot: ProductSnapshot) -> None:
    from app.services.products import normalize_product_identity

    if (
        snapshot.platform != product.platform
        or snapshot.external_item_id != product.external_id
        or snapshot.variant_id != product.variant_id
    ):
        raise ValueError("A resposta não pertence ao produto e variante solicitados.")
    normalize_product_identity(product.platform, snapshot.canonical_url, product.external_id)
    if not snapshot.name or len(snapshot.name) > 500:
        raise ValueError("Nome oficial do produto inválido.")
    # Preserve manual category corrections and explicit local mappings. Unknown IDs do not
    # become trusted local category slugs merely because they came from an official API.
    metadata = dict(product.classification_metadata or {})
    metadata["category_external_id"] = snapshot.category_external_id
    metadata["category_name"] = snapshot.category_external_name
    metadata["source_name"] = snapshot.name
    product.classification_metadata = metadata
    product.name = snapshot.name[:255]
