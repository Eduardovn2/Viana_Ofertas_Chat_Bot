"""Explicit, single-listing validation using the existing history/category/dry-run pipeline."""

import re
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.config import Settings
from app.integrations.contracts import ProductRef
from app.integrations.errors import IntegrationError
from app.integrations.registry import ConnectorRegistry
from app.models import (
    AffiliateLink,
    AppSetting,
    IntegrationEvent,
    Platform,
    Product,
    PublicationQueueItem,
)
from app.services.configuration import save_monitoring_configuration
from app.services.leases import acquire_lease, conflict_insert, release_lease, renew_lease
from app.services.monitoring import apply_product_snapshot, record_price
from app.services.offers import OfferEngine
from app.services.products import validate_affiliate_url


def validate_listing(
    session_factory,
    settings: Settings,
    item_id: str,
    affiliate_url: str,
    *,
    registry=None,
    clock=None,
) -> dict:
    """One authorized read; never starts a scheduler, resolves short links or publishes.

    Failed reads retain the supplied manual configuration and an audit event, not a
    fabricated product or observation. Commercial enablement exists in this call only.
    """
    if not re.fullmatch(r"MLB[0-9]{1,97}", item_id):
        raise ValueError("Informe um identificador de anúncio MLB numérico.")
    validate_affiliate_url(Platform.MERCADO_LIVRE, affiliate_url)
    now = clock or (lambda: datetime.now(UTC))
    effective = settings.model_copy(
        update={"scheduler_enabled": False, "mercado_livre_enabled": True, "shopee_enabled": False}
    )
    own_registry = registry is None
    registry = registry or ConnectorRegistry(effective, session_factory=session_factory, clock=now)
    lease_name = f"commercial-check:{item_id}"
    owner = acquire_lease(session_factory, lease_name, effective.monitoring_lease_seconds, now)
    if owner is None:
        if own_registry:
            registry.close()
        raise IntegrationError("MONITORING_BUSY", "Já existe consulta pontual para este anúncio.")
    reference = f"https://api.mercadolibre.com/items/{item_id}"
    report = {
        "item_id": item_id,
        "checked_at": now().isoformat(),
        "publication_mode": "dry-run",
        "automatic_monitoring": False,
        "status": "FAILED",
    }
    try:
        with session_factory() as session:
            save_monitoring_configuration(session, settings, {"scheduler_enabled": False})
            # This is user-supplied configuration, not a claim of API verification.
            key = f"manual-link:mercadolivre:{item_id}"
            value = {"item_id": item_id, "url": affiliate_url, "source": "MANUAL_LINKBUILDER"}
            previous = session.scalar(select(AppSetting).where(AppSetting.key == key))
            if previous is not None and previous.value != value:
                raise IntegrationError(
                    "AFFILIATE_LINK_CONFLICT", "Preserve a configuração manual existente."
                )
            if previous is None:
                session.add(AppSetting(key=key, value=value))
            existing = session.scalar(
                select(Product).where(
                    Product.platform == Platform.MERCADO_LIVRE,
                    Product.external_id == item_id,
                    Product.variant_id == "",
                )
            )
            canonical = (
                existing.original_url
                if existing
                else f"https://produto.mercadolivre.com.br/MLB-{item_id[3:]}"
            )
            product_id = existing.id if existing else None
            session.commit()
        try:
            # An explicit check can revalidate a previously blocked credential.
            snapshot = registry.get(Platform.MERCADO_LIVRE).fetch_product(
                ProductRef(Platform.MERCADO_LIVRE, item_id, canonical)
            )
        except IntegrationError as error:
            with session_factory() as session:
                session.add(
                    IntegrationEvent(
                        platform=Platform.MERCADO_LIVRE,
                        product_id=product_id,
                        operation="manual_commercial_check",
                        status="FAILED",
                        error_code=error.code,
                        source_reference=reference,
                        observed_at=now(),
                    )
                )
                session.commit()
            report.update(error_code=error.code, http_status=getattr(error, "status_code", None))
            return report
        with session_factory() as session:
            if not renew_lease(session, lease_name, owner, effective.monitoring_lease_seconds, now):
                raise IntegrationError(
                    "MONITORING_BUSY", "Consulta perdeu sua reserva; nada foi importado."
                )
            session.execute(
                conflict_insert(session, Product)
                .values(
                    platform=Platform.MERCADO_LIVRE,
                    external_id=item_id,
                    variant_id="",
                    original_url=snapshot.canonical_url,
                    name=snapshot.name,
                )
                .on_conflict_do_nothing(
                    index_elements=[Product.platform, Product.external_id, Product.variant_id]
                )
            )
            product = session.scalar(
                select(Product)
                .where(
                    Product.platform == Platform.MERCADO_LIVRE,
                    Product.external_id == item_id,
                    Product.variant_id == "",
                )
                .with_for_update()
            )
            if product.affiliate_link is not None and product.affiliate_link.url != affiliate_url:
                raise IntegrationError(
                    "AFFILIATE_LINK_CONFLICT", "O anúncio possui outro link; alteração recusada."
                )
            if product.affiliate_link is None:
                session.add(
                    AffiliateLink(
                        product_id=product.id,
                        url=affiliate_url,
                        # The association is explicitly supplied by the responsible user.
                        source="MANUAL_LINKBUILDER_VERIFIED",
                    )
                )
                session.flush()
                session.expire(product, ["affiliate_link"])
            apply_product_snapshot(product, snapshot)
            history = record_price(session, product, snapshot, now=now())
            offer = OfferEngine(effective, clock=now).process(session, product, history)
            partial = (
                snapshot.evidence_metadata.get("price_access_reason") == "price_permission_denied"
            )
            session.add(
                IntegrationEvent(
                    platform=Platform.MERCADO_LIVRE,
                    product_id=product.id,
                    operation="manual_commercial_check",
                    status="PARTIAL" if partial else "SUCCEEDED",
                    error_code="PRICE_PERMISSION_DENIED" if partial else None,
                    source_reference=history.source_reference,
                    observed_at=now(),
                )
            )
            report.update(
                status="PARTIAL" if partial else "SUCCEEDED",
                product_id=product.id,
                history_id=history.id,
                name=product.name,
                current_price=str(history.current_price)
                if history.current_price is not None
                else None,
                previous_price=str(history.previous_price)
                if history.previous_price is not None
                else None,
                currency=history.currency,
                availability=history.availability.value,
                price_access=history.price_access.value,
                category_external_id=snapshot.category_external_id,
                image_url=snapshot.image_url,
                source_reference=history.source_reference,
                offer_id=offer.id if offer else None,
                queue_items=session.scalar(
                    select(func.count())
                    .select_from(PublicationQueueItem)
                    .where(PublicationQueueItem.product_id == product.id)
                ),
            )
            session.commit()
        if not partial:
            registry.unblock(Platform.MERCADO_LIVRE)
        return report
    finally:
        release_lease(session_factory, lease_name, owner)
        if own_registry:
            registry.close()
