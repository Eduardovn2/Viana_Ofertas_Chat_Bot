"""Validate and persist price observations without committing transactions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.integrations.base import PriceProvider, PriceSnapshot
from app.models import Availability, PriceAccess, Product, ProductPriceHistory


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
    )
    session.add(history)
    session.flush()
    return history


def collect_price(
    session: Session, product: Product, provider: PriceProvider, *, now: datetime | None = None
) -> ProductPriceHistory:
    if not product.monitoring_enabled:
        raise ValueError("O monitoramento deste produto está desativado.")
    return record_price(session, product, provider.fetch(product), now=now)
