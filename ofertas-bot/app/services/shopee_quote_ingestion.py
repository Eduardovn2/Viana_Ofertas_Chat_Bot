"""Prepare historical authenticated quotes; ingestion remains test-only."""

import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text

from app.integrations.base import PriceSnapshot
from app.integrations.contracts import PriceContext, fingerprint
from app.integrations.shopee import _announced_fields, _money
from app.models import (
    AppSetting,
    Availability,
    IntegrationEvent,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    ShopeeCollectionRun,
    SourceType,
)
from app.services.comparison import aware_utc
from app.services.monitoring import record_price
from app.services.shopee_single_read import PREFIX
from app.services.simulation_safety import require_simulation_scope


def prepare_historical_quote(session, envelope, product_id):
    """Read-only verification against persisted authority, never trusts file status alone."""
    identity = str(UUID(envelope["authorization_id"]))
    row = session.scalar(select(AppSetting).where(AppSetting.key == PREFIX + identity))
    run = session.get(ShopeeCollectionRun, identity)
    quote = envelope.get("quote")
    if row is None or run is None or not isinstance(quote, dict):
        raise ValueError("Resposta histórica ou auditoria ausente.")
    authority = row.value
    digest = fingerprint(quote)
    if (
        envelope.get("status") != "SUCCEEDED"
        or envelope.get("http_attempts") != 1
        or envelope.get("http_status") != 200
        or quote.get("http_status") != 200
        or quote.get("http_calls") != 1
        or quote.get("mode") != "REAL_SINGLE_READ"
        or authority.get("state") != "SUCCEEDED"
        or authority.get("mode") != "REAL_SINGLE_READ"
        or authority.get("consumed") is not True
        or run.mode != "REAL"
        or run.status != "SUCCEEDED"
        or run.requests != 1
        or authority.get("result") != run.result
        or run.result.get("quote_sha256") != digest
        or run.result.get("http_status") != 200
        or run.result.get("quote_persisted") is not False
        or fingerprint(authority["scope"]) != authority["scope_sha256"]
        or run.specification.get("scope_sha256") != authority["scope_sha256"]
        or authority.get("approval", {}).get("scope_sha256") != authority["scope_sha256"]
    ):
        raise ValueError("Integridade ou proveniência autenticada não comprovada.")
    events = session.execute(
        select(IntegrationEvent.operation, IntegrationEvent.status)
        .where(IntegrationEvent.source_reference == "identity-read:" + identity)
        .order_by(IntegrationEvent.id)
    ).all()
    if list(events) != [
        ("identity_read_prepared", "PREPARED"),
        ("identity_read_approved", "APPROVED"),
        ("identity_read_claimed", "CLAIMED"),
        ("identity_read_result", "SUCCEEDED"),
    ]:
        raise ValueError("Cadeia de auditoria incompleta.")
    product = session.get(Product, product_id)
    scope = authority["scope"]
    expected = f"{scope['shop_id']}:{scope['item_id']}"
    if (
        product is None
        or product.platform != Platform.SHOPEE
        or product.variant_id
        or scope["product_id"] != product_id
        or authority["request"]["product_id"] != product_id
        or product.external_id != expected
        or quote.get("identity") != expected
        or scope.get("operation") != "productOfferV2"
        or scope.get("page") != 1
        or scope.get("limit") != 1
        or scope.get("max_http_calls") != 1
    ):
        raise ValueError("Identidade histórica incompatível com o produto.")
    if (
        quote.get("currency") != "BRL"
        or quote.get("availability") != "UNKNOWN"
        or quote.get("price_access") != "UNKNOWN"
        or quote.get("commercial_verified") is not False
        or quote.get("period_time_unit") != "UNCONFIRMED"
        or quote.get("period_semantics") != "UNCONFIRMED"
    ):
        raise ValueError("Condições comerciais históricas incompatíveis.")
    observed = aware_utc(datetime.fromisoformat(quote["observed_at"]))
    claimed = aware_utc(datetime.fromisoformat(authority["claimed_at"]))
    finished = aware_utc(datetime.fromisoformat(authority["finished_at"]))
    if not claimed <= observed <= finished:
        raise ValueError("Horário da resposta fora da leitura auditada.")
    price, minimum, maximum = (_money(quote.get(k)) for k in ("price", "price_min", "price_max"))
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("Faixa histórica inválida.")
    if price is not None and (
        minimum is None or maximum is None or price != minimum or price != maximum
    ):
        raise ValueError("Preço utilizável não comprovado pela faixa.")
    return quote_snapshot(quote, identity)


def quote_snapshot(quote, identity):
    """Normalize a verified read; callers must verify the durable provenance first."""
    expected = quote["identity"]
    digest = fingerprint(quote)
    observed = aware_utc(datetime.fromisoformat(quote["observed_at"]))
    price, minimum, maximum = (_money(quote.get(k)) for k in ("price", "price_min", "price_max"))
    context = PriceContext(
        origin="shopee:official_api",
        operation="productOfferV2",
        product_identity=expected,
        price_kind="ITEM_QUOTE" if price is not None else "UNRESOLVED_QUOTE",
    )
    metadata = _announced_fields(
        {
            "priceDiscountRate": quote.get("advertised_discount_percent"),
            "periodStartTime": quote.get("offer_period_start_raw"),
            "periodEndTime": quote.get("offer_period_end_raw"),
        }
    )
    metadata.update(
        price_context=context.model_dump(),
        price_basis=context.price_basis,
        price_range="single" if price is not None else "unresolved",
        price_min=str(minimum) if minimum is not None else None,
        price_max=str(maximum) if maximum is not None else None,
        observation_only=True,
        commercial_verified=False,
        variant_status="UNCONFIRMED",
        price_id="identity-read:" + identity,
        source_name="quote_sha256:" + digest,
    )
    return PriceSnapshot(
        current_price=price,
        previous_price=None,
        availability=Availability.UNKNOWN,
        price_access=PriceAccess.UNKNOWN,
        source="shopee:official_api",
        source_type=SourceType.OFFICIAL_API,
        source_reference="productOfferV2:" + expected,
        observed_at=observed,
        is_simulated=False,
        raw_fingerprint=fingerprint({"authorization_id": identity, "quote_sha256": digest}),
        evidence_metadata=metadata,
    )


def ingest_historical_quote(session, settings, envelope, product_id, *, offline=False):
    """No operational entry point until an explicit later authorization is implemented."""
    if not offline:
        raise ValueError("Gravação operacional bloqueada; exige nova autorização explícita.")
    require_simulation_scope(session, settings)
    product = session.scalar(select(Product).where(Product.id == product_id).with_for_update())
    snapshot = prepare_historical_quote(session, envelope, product_id)
    return record_price(session, product, snapshot)


def ingest_authorized_cockpit_quote(session, settings, envelope, *, actor, confirmed=False):
    """Narrow owner-authorized exception; no generic operational ingestion switch."""
    read_id = "ee52b076-4084-4dc3-abd3-4a9e994db463"
    digest = "a567eb6f9d8acf963ee73e54874fbff1b9abb5be747f8f77173a0deece2ab829"
    if confirmed is not True or not re.fullmatch(r"[\w.@-]{3,80}", actor):
        raise ValueError("Autorização explícita e responsável são obrigatórios.")
    if envelope.get("authorization_id") != read_id or fingerprint(envelope.get("quote")) != digest:
        raise ValueError("Cotação fora da autorização específica.")
    if session.get_bind().dialect.name != "postgresql" or session.execute(
        text("SELECT current_database(), current_schema()")
    ).one() != ("ofertas_bot", "public"):
        raise ValueError("Banco operacional incompatível.")
    if any(
        getattr(settings, name)
        for name in (
            "shopee_enabled",
            "shopee_short_links_enabled",
            "mercado_livre_enabled",
            "telegram_publisher_enabled",
            "telegram_real_sends_authorized",
            "simulation_automation_enabled",
        )
    ):
        raise ValueError("Flags devem permanecer desativadas.")
    product = session.scalar(select(Product).where(Product.id == 2).with_for_update())
    snapshot = prepare_historical_quote(session, envelope, 2)
    metadata = snapshot.evidence_metadata
    if (
        product.external_id != "1904573791:23199869203"
        or snapshot.current_price != Decimal("1099.00")
        or snapshot.currency != "BRL"
        or snapshot.observed_at.isoformat() != "2026-10-30T09:29:44.200225+00:00"
        or metadata["price_min"] != "1099.00"
        or metadata["price_max"] != "1099.00"
        or metadata["advertised_discount_percent"] != 31
    ):
        raise ValueError("Valores divergem da autorização.")
    existing = session.scalar(
        select(ProductPriceHistory).where(
            ProductPriceHistory.product_id == 2,
            ProductPriceHistory.source == snapshot.source,
            ProductPriceHistory.raw_fingerprint == snapshot.raw_fingerprint,
            ProductPriceHistory.observed_at == snapshot.observed_at,
        )
    )
    if existing is not None:
        if (
            existing.current_price != snapshot.current_price
            or existing.previous_price is not None
            or existing.source_type != snapshot.source_type
            or existing.evidence_metadata != metadata
            or existing.availability != Availability.UNKNOWN
            or existing.price_access != PriceAccess.UNKNOWN
            or existing.is_simulated
            or existing.currency != "BRL"
        ):
            raise ValueError("Observação existente diverge da evidência autorizada.")
        return existing
    row = record_price(session, product, snapshot)
    session.add(
        IntegrationEvent(
            platform=Platform.SHOPEE,
            product_id=2,
            operation="historical_quote_ingest",
            status="RECORDED",
            source_reference=f"historical-ingest:{read_id}:{digest}:actor:{actor}",
        )
    )
    session.flush()
    return row
