"""Feed official normalized observations into the existing pipeline, without publishing."""

from sqlalchemy import select

from app.models import IntegrationEvent, Product
from app.services.leases import conflict_insert
from app.services.monitoring import apply_product_snapshot, record_price
from app.services.offers import OfferEngine


def upsert_discovered_product(session, snapshot):
    statement = (
        conflict_insert(session, Product)
        .values(
            platform=snapshot.platform,
            external_id=snapshot.external_item_id,
            variant_id=snapshot.variant_id,
            original_url=snapshot.canonical_url,
            name=snapshot.name,
        )
        .on_conflict_do_nothing(
            index_elements=[Product.platform, Product.external_id, Product.variant_id]
        )
    )
    session.execute(statement)
    return session.scalar(
        select(Product)
        .where(
            Product.platform == snapshot.platform,
            Product.external_id == snapshot.external_item_id,
            Product.variant_id == snapshot.variant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def ingest_discovery(session, page, platform, settings, *, clock=None, evaluate_offers=True):
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
            product = upsert_discovered_product(session, snapshot)
        if not product.monitoring_enabled:
            continue
        apply_product_snapshot(product, snapshot)
        from app.models import ProductPriceHistory

        history = session.scalar(
            select(ProductPriceHistory).where(
                ProductPriceHistory.product_id == product.id,
                ProductPriceHistory.source == snapshot.source,
                ProductPriceHistory.raw_fingerprint == snapshot.raw_fingerprint,
                ProductPriceHistory.observed_at == snapshot.observed_at,
            )
        )
        if history is None:
            history = record_price(session, product, snapshot, now=clock() if clock else None)
        if evaluate_offers:
            OfferEngine(settings, clock=clock).process(session, product, history)
        ids.append(product.id)
    session.add(
        IntegrationEvent(platform=platform, operation="discover_offers", status="SUCCEEDED")
    )
    session.flush()
    return ids
