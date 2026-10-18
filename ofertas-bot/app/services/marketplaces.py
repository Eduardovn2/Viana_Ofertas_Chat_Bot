"""Feed official normalized observations into the existing pipeline, without publishing."""

from sqlalchemy import select

from app.models import IntegrationEvent, Product
from app.services.monitoring import apply_product_snapshot, record_price
from app.services.offers import OfferEngine


def ingest_discovery(session, page, platform, settings, *, clock=None):
    ids = []
    for snapshot in page.items:
        if snapshot.platform != platform:
            raise ValueError("Resposta de descoberta pertence a outra plataforma.")
        product = session.scalar(
            select(Product)
            .where(
                Product.platform == platform,
                Product.external_id == snapshot.external_item_id,
                Product.variant_id == snapshot.variant_id,
            )
            .with_for_update()
        )
        if product is None:
            product = Product(
                platform=platform,
                external_id=snapshot.external_item_id,
                variant_id=snapshot.variant_id,
                original_url=snapshot.canonical_url,
                name=snapshot.name[:255],
            )
            session.add(product)
            session.flush()
        if not product.monitoring_enabled:
            continue
        apply_product_snapshot(product, snapshot)
        history = record_price(session, product, snapshot, now=clock() if clock else None)
        OfferEngine(settings, clock=clock).process(session, product, history)
        ids.append(product.id)
    session.add(
        IntegrationEvent(platform=platform, operation="discover_offers", status="SUCCEEDED")
    )
    session.flush()
    return ids
