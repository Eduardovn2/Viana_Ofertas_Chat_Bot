"""A transactional simulation queue. No external publisher is invoked here."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.models import (
    CategoryMode,
    DestinationChannel,
    Offer,
    OfferStatus,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    PublicationQueueItem,
    PublicationStatus,
    QueueStatus,
)
from app.services.categories import CategoryService
from app.services.category_routing import destination_accepts, invalidate_ineligible_queue
from app.services.comparison import aware_utc
from app.services.offers import assess_observation, has_own_affiliate_link, latest_observation


class SimulatedQueueService:
    def __init__(self, settings: Settings, clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))

    def get_or_create_destination(self, session: Session) -> PublicationDestination:
        query = select(PublicationDestination).where(
            PublicationDestination.channel == DestinationChannel.SIMULATED,
            PublicationDestination.external_id == "simulation",
        )
        destination = session.scalar(query)
        if destination is not None:
            return destination
        try:
            with session.begin_nested():
                destination = PublicationDestination(
                    name="Simulação local",
                    channel=DestinationChannel.SIMULATED,
                    external_id="simulation",
                    enabled=True,
                    manual_approval_required=False,
                    category_mode=CategoryMode.UNCONFIGURED,
                )
                session.add(destination)
                session.flush()
        except IntegrityError:
            destination = session.scalar(query.execution_options(populate_existing=True))
            if destination is None:
                raise
        return destination

    def _offer_issue(
        self, session: Session, offer: Offer, now: datetime
    ) -> tuple[Product | None, str | None]:
        product = session.scalar(
            select(Product)
            .where(Product.id == offer.product_id)
            .options(selectinload(Product.affiliate_link))
            .execution_options(populate_existing=True)
        )
        if product is None or not product.monitoring_enabled:
            return product, "O produto está ausente ou com monitoramento desativado."
        if offer.status in (OfferStatus.EXPIRED, OfferStatus.FAILED, OfferStatus.REJECTED):
            return product, "A oferta não está ativa."
        if offer.expires_at is None or aware_utc(offer.expires_at) <= now:
            return product, "A oferta expirou."
        observation = session.get(
            ProductPriceHistory, offer.price_history_id, populate_existing=True
        )
        latest = latest_observation(session, product.id)
        if observation is None or latest is None or latest.id != observation.id:
            return product, "Existe uma observação mais recente; a oferta precisa ser recalculada."
        assessment = assess_observation(
            session, product, observation, self.settings, now, coupon_id=offer.coupon_id
        )
        if assessment is None:
            return product, "O preço ou cupom deixou de ser público, aplicável e atual."
        for field in (
            "current_price",
            "previous_price",
            "historical_price",
            "discount_amount",
            "discount_percentage",
            "final_price",
            "coupon_id",
        ):
            if getattr(offer, field) != getattr(assessment, field):
                return product, "As condições da oferta mudaram; a oferta precisa ser recalculada."
        return product, None

    def _cooldown_issue(
        self, session: Session, offer: Offer, destination_id: int, now: datetime
    ) -> str | None:
        published = session.scalar(
            select(PublicationQueueItem)
            .where(
                PublicationQueueItem.product_id == offer.product_id,
                PublicationQueueItem.destination_id == destination_id,
                PublicationQueueItem.status == QueueStatus.PUBLISHED,
            )
            .order_by(PublicationQueueItem.published_at.desc(), PublicationQueueItem.id.desc())
            .limit(1)
        )
        if published is None:
            return None
        if published.published_at is None:
            return "Publicação anterior sem horário verificável."
        published_at = aware_utc(published.published_at)
        if now < published_at + timedelta(seconds=self.settings.queue_republish_seconds):
            return "A janela mínima de três horas entre publicações ainda não terminou."
        observation = session.get(ProductPriceHistory, offer.price_history_id)
        if observation is None or aware_utc(observation.observed_at) <= published_at:
            return "Republicação exige uma observação posterior à última publicação."
        return None

    def enqueue(
        self, session: Session, offer: Offer, destination: PublicationDestination | None = None
    ) -> PublicationQueueItem | None:
        now = aware_utc(self.clock())
        session.flush()
        product, issue = self._offer_issue(session, offer, now)
        if issue is not None or offer.status == OfferStatus.PUBLISHED:
            return None
        destination = destination or self.get_or_create_destination(session)
        if not destination.enabled or destination.channel != DestinationChannel.SIMULATED:
            return None
        if not destination_accepts(session, product, destination):
            invalidate_ineligible_queue(
                session, product_id=product.id, destination_id=destination.id
            )
            return None
        if self._cooldown_issue(session, offer, destination.id, now) is not None:
            return None
        ready = has_own_affiliate_link(product) and not destination.manual_approval_required
        target_status = QueueStatus.READY if ready else QueueStatus.PENDING
        query = select(PublicationQueueItem).where(
            PublicationQueueItem.product_id == offer.product_id,
            PublicationQueueItem.destination_id == destination.id,
            PublicationQueueItem.active_slot == 1,
        )
        item = session.scalar(query)
        if item is None:
            try:
                with session.begin_nested():
                    item = PublicationQueueItem(
                        product_id=offer.product_id,
                        offer_id=offer.id,
                        destination_id=destination.id,
                        status=target_status,
                        active_slot=1,
                        is_simulated=True,
                        validated_at=now if ready else None,
                    )
                    session.add(item)
                    session.flush()
            except IntegrityError:
                item = session.scalar(query.execution_options(populate_existing=True))
                if item is None:
                    raise
        if item.offer_id != offer.id:
            item.offer_id = offer.id
            item.attempts = 0
        item.status = target_status
        item.validated_at = now if ready else None
        item.last_error = None
        session.flush()
        return item

    def _finish(
        self,
        session: Session,
        item: PublicationQueueItem,
        offer: Offer | None,
        status: QueueStatus,
        reason: str,
    ) -> PublicationQueueItem:
        finished = session.execute(
            update(PublicationQueueItem)
            .where(
                PublicationQueueItem.id == item.id,
                PublicationQueueItem.status.in_((QueueStatus.PENDING, QueueStatus.READY)),
                PublicationQueueItem.active_slot == 1,
            )
            .values(
                status=status,
                active_slot=None,
                last_error=reason,
                validated_at=aware_utc(self.clock()),
            )
            .execution_options(synchronize_session=False)
        )
        session.refresh(item)
        if finished.rowcount == 1 and offer is not None and offer.status != OfferStatus.PUBLISHED:
            offer.status = (
                OfferStatus.EXPIRED if status == QueueStatus.EXPIRED else OfferStatus.FAILED
            )
        session.flush()
        return item

    def publish(self, session: Session, item_id: int) -> PublicationQueueItem:
        """Call within the product lease, then commit the returned state outside.

        Row locks serialize PostgreSQL readers; the conditional UPDATE remains the
        final claim guard on both PostgreSQL and SQLite. Terminal calls are idempotent.
        """
        now = aware_utc(self.clock())
        session.flush()
        # Classification edits lock Product before expiring its queue entries;
        # use the same lock order to avoid a queue/product deadlock on PostgreSQL.
        product_id = session.scalar(
            select(PublicationQueueItem.product_id).where(PublicationQueueItem.id == item_id)
        )
        if product_id is not None:
            session.scalar(select(Product.id).where(Product.id == product_id).with_for_update())
        item = session.scalar(
            select(PublicationQueueItem)
            .where(PublicationQueueItem.id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise ValueError("Item da fila não encontrado.")
        if item.status not in (QueueStatus.PENDING, QueueStatus.READY) or item.active_slot != 1:
            return item
        offer = session.get(Offer, item.offer_id, populate_existing=True)
        destination = session.get(
            PublicationDestination, item.destination_id, populate_existing=True
        )
        if (
            destination is None
            or destination.channel != DestinationChannel.SIMULATED
            or not destination.enabled
            or not item.is_simulated
        ):
            return self._finish(
                session,
                item,
                offer,
                QueueStatus.FAILED,
                "Destino de simulação desativado ou inválido.",
            )
        if offer is None or offer.product_id != item.product_id:
            return self._finish(
                session, item, offer, QueueStatus.FAILED, "Oferta da fila inválida."
            )
        if item.attempts >= self.settings.max_queue_attempts:
            return self._finish(
                session, item, offer, QueueStatus.FAILED, "Limite de tentativas atingido."
            )
        product, issue = self._offer_issue(session, offer, now)
        if product is not None and issue is None:
            try:
                CategoryService().classify(session, product)
            except (ValueError, OSError):
                return self._finish(
                    session,
                    item,
                    None,
                    QueueStatus.EXPIRED,
                    "A classificação atual não pôde ser validada com as regras locais.",
                )
            if not destination_accepts(session, product, destination):
                return self._finish(
                    session,
                    item,
                    None,
                    QueueStatus.EXPIRED,
                    "As categorias atuais não atendem às preferências do destino.",
                )
        issue = issue or self._cooldown_issue(session, offer, destination.id, now)
        if issue is not None:
            return self._finish(session, item, offer, QueueStatus.EXPIRED, issue)
        if not has_own_affiliate_link(product):
            waiting = session.execute(
                update(PublicationQueueItem)
                .where(
                    PublicationQueueItem.id == item.id,
                    PublicationQueueItem.status.in_((QueueStatus.PENDING, QueueStatus.READY)),
                    PublicationQueueItem.active_slot == 1,
                )
                .values(
                    status=QueueStatus.PENDING,
                    validated_at=None,
                    last_error="Cadastre um link de afiliado próprio antes da simulação.",
                )
                .execution_options(synchronize_session=False)
            )
            session.refresh(item)
            if waiting.rowcount == 1 and offer.status != OfferStatus.PUBLISHED:
                offer.status = OfferStatus.PENDING
            session.flush()
            return item
        session.execute(
            update(PublicationQueueItem)
            .where(
                PublicationQueueItem.id == item.id,
                PublicationQueueItem.status == QueueStatus.PENDING,
                PublicationQueueItem.active_slot == 1,
            )
            .values(status=QueueStatus.READY, validated_at=now, last_error=None)
            .execution_options(synchronize_session=False)
        )
        claimed = session.execute(
            update(PublicationQueueItem)
            .where(
                PublicationQueueItem.id == item.id,
                PublicationQueueItem.status == QueueStatus.READY,
                PublicationQueueItem.active_slot == 1,
            )
            .values(
                status=QueueStatus.PUBLISHED,
                active_slot=None,
                published_at=now,
                validated_at=now,
                attempts=PublicationQueueItem.attempts + 1,
                is_simulated=True,
                last_error=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.refresh(item)
            return item
        session.refresh(item)
        offer.status = OfferStatus.PUBLISHED
        session.add(
            PublicationLog(
                offer_id=offer.id,
                product_id=product.id,
                destination_id=destination.id,
                status=PublicationStatus.SENT,
                attempt_number=item.attempts,
                message_id=f"simulated:{uuid4()}",
                message_text=f"[SIMULATED] Oferta {offer.id}; nenhum envio externo foi realizado.",
                sent_at=now,
            )
        )
        session.flush()
        return item
