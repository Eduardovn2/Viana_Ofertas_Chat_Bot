"""Conservative category selection shared by queue creation and revalidation."""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    Category,
    CategoryMode,
    DestinationCategory,
    Product,
    ProductCategory,
    PublicationDestination,
    PublicationQueueItem,
    QueueStatus,
)


def destination_accepts(
    session: Session, product: Product, destination: PublicationDestination
) -> bool:
    """Gerais is an ordinary category, never an implicit permission to publish."""
    if product.id is None or destination.id is None or not destination.enabled:
        return False
    if destination.category_mode not in (CategoryMode.ALL, CategoryMode.SELECTED):
        return False
    session.flush()
    eligible = (
        select(ProductCategory.id)
        .join(Category, Category.id == ProductCategory.category_id)
        .where(ProductCategory.product_id == product.id, Category.active.is_(True))
    )
    if destination.category_mode == CategoryMode.SELECTED:
        eligible = eligible.where(
            ProductCategory.category_id.in_(
                select(DestinationCategory.category_id).where(
                    DestinationCategory.destination_id == destination.id
                )
            )
        )
    return session.scalar(eligible.limit(1)) is not None


def invalidate_ineligible_queue(
    session: Session, *, product_id: int | None = None, destination_id: int | None = None
) -> int:
    """Expire only active slots; published records keep the three-hour history."""
    session.flush()
    query = (
        select(PublicationQueueItem, Product, PublicationDestination)
        .join(Product, Product.id == PublicationQueueItem.product_id)
        .join(
            PublicationDestination, PublicationDestination.id == PublicationQueueItem.destination_id
        )
        .where(PublicationQueueItem.active_slot == 1)
    )
    if product_id is not None:
        query = query.where(PublicationQueueItem.product_id == product_id)
    if destination_id is not None:
        query = query.where(PublicationQueueItem.destination_id == destination_id)
    count = 0
    for item, product, destination in session.execute(query):
        if destination_accepts(session, product, destination):
            continue
        result = session.execute(
            update(PublicationQueueItem)
            .where(
                PublicationQueueItem.id == item.id,
                PublicationQueueItem.active_slot == 1,
                PublicationQueueItem.status.in_((QueueStatus.PENDING, QueueStatus.READY)),
            )
            .values(
                status=QueueStatus.EXPIRED,
                active_slot=None,
                last_error="As categorias atuais não atendem às preferências do destino.",
                validated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        count += result.rowcount
        session.refresh(item)
    session.flush()
    return count
