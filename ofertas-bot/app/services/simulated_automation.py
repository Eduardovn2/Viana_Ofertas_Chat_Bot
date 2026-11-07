"""Bridge the existing simulated price/offer queue to an exclusively mocked outbox."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.integrations.telegram import TelegramPublishError
from app.integrations.telegram_simulated import MOCK_CHAT, MockTelegramDelivery
from app.models import (
    DestinationChannel,
    Offer,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationQueueItem,
    QueueStatus,
    SimulationAudit,
    SimulationPolicy,
    SimulationProduct,
    TelegramSimulation,
)
from app.services.categories import CategoryService
from app.services.category_routing import destination_accepts
from app.services.comparison import aware_utc
from app.services.leases import acquire_lease, release_lease, renew_lease
from app.services.message_templates import MessageData, RenderedMessage, render_message
from app.services.offers import has_own_affiliate_link, latest_observation
from app.services.product_filters import matches_product
from app.services.publication_queue import SimulatedQueueService
from app.services.simulation_safety import require_simulation_scope
from app.services.telegram_publication import _hash, _row, _text


class SimulatedAutomation:
    def __init__(self, factory, settings, clock=None):
        self.factory, self.settings = factory, settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.queue = SimulatedQueueService(settings, self.clock)
        self.delivery = None

    def _now(self):
        return aware_utc(self.clock())

    def close(self):
        if self.delivery is not None:
            self.delivery.close()

    def _event(self, session, product_id, destination_id, event, *, entry=None, reason=None):
        session.add(
            SimulationAudit(
                product_id=product_id,
                destination_id=destination_id,
                event=event,
                publication_id=entry.id if entry else None,
                preview_hash=entry.preview_hash if entry else None,
                reason=reason,
                created_at=self._now(),
            )
        )

    def _lock(self, session, queue_id):
        require_simulation_scope(session, self.settings)
        identity = session.execute(
            select(PublicationQueueItem.destination_id, PublicationQueueItem.product_id).where(
                PublicationQueueItem.id == queue_id
            )
        ).first()
        if identity is None:
            raise ValueError("Fila simulada inexistente.")
        destination = session.scalar(
            select(PublicationDestination)
            .where(PublicationDestination.id == identity.destination_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        product = session.scalar(
            select(Product)
            .where(Product.id == identity.product_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        item = session.scalar(
            select(PublicationQueueItem)
            .where(PublicationQueueItem.id == queue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return item, product, destination

    def _context(self, session, item, product, destination):
        if not item.is_simulated or destination.channel != DestinationChannel.SIMULATED:
            return None, "NOT_SIMULATED"
        if item.active_slot != 1 or item.status not in (QueueStatus.PENDING, QueueStatus.READY):
            return None, "QUEUE_INACTIVE"
        if not destination.enabled:
            return None, "DESTINATION_PAUSED"
        profile = session.get(SimulationProduct, product.id)
        if (
            profile is None
            or not product.name.startswith("[SIMULATED] ")
            or (product.classification_metadata or {}).get("simulation_only") is not True
        ):
            return None, "NOT_ENROLLED_SIMULATION"
        policy = session.get(SimulationPolicy, destination.id, populate_existing=True)
        if policy is None or not policy.enabled:
            return None, "POLICY_DISABLED"
        offer = session.get(Offer, item.offer_id, populate_existing=True)
        if offer is None or offer.product_id != product.id:
            return None, "OFFER_MISMATCH"
        _, issue = self.queue._offer_issue(session, offer, self._now())
        if issue:
            return None, "REVALIDATION_FAILED"
        observation = session.get(ProductPriceHistory, offer.price_history_id)
        if not observation.is_simulated or observation.source != "simulated":
            return None, "NON_SIMULATED_HISTORY"
        if offer.historical_price is None or offer.historical_price <= offer.current_price:
            return None, "NO_HISTORICAL_DROP"
        if offer.coupon_id is not None:
            return None, "COUPON_NOT_IN_SIMULATION_SCOPE"
        CategoryService().classify(session, product)
        if not destination_accepts(session, product, destination):
            return None, "CATEGORY_FILTER"
        if not matches_product(product.id, product.name, policy.product_ids, policy.terms):
            return None, "PRODUCT_TERM_FILTER"
        if policy.max_price is not None and offer.current_price > policy.max_price:
            return None, "MAX_PRICE_FILTER"
        if policy.min_discount is not None and offer.discount_percentage < policy.min_discount:
            return None, "MIN_DISCOUNT_FILTER"
        if self.queue._cooldown_issue(session, offer, destination.id, self._now()):
            return None, "THREE_HOUR_COOLDOWN"
        if not has_own_affiliate_link(product):
            return None, "AFFILIATE_PENDING"
        message = render_message(
            MessageData(product.name, offer.current_price, product.affiliate_link.url)
        )
        rendered = RenderedMessage("🧪 SIMULAÇÃO — NENHUM ENVIO REAL\n\n" + message.text)
        session.flush()
        return {
            "mode": "SIMULATED",
            "message": asdict(rendered),
            "offer": _row(offer),
            "history": _row(observation),
            "product": _row(product),
            "destination": _row(destination),
            "policy": _row(policy),
            "affiliate_link": _row(product.affiliate_link),
        }, None

    def _entry(self, session, item):
        return session.scalar(
            select(TelegramSimulation)
            .where(
                TelegramSimulation.product_id == item.product_id,
                TelegramSimulation.destination_id == item.destination_id,
                TelegramSimulation.active_slot == 1,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _expire(self, session, item, entry, reason):
        if entry is not None and entry.status in ("PENDING", "READY"):
            entry.status, entry.active_slot, entry.error_code = "EXPIRED", None, reason
            self._event(
                session, item.product_id, item.destination_id, "EXPIRED", entry=entry, reason=reason
            )
        if item.active_slot == 1:
            self.queue._finish(session, item, None, QueueStatus.EXPIRED, reason)

    def _stage(self, session, queue_id):
        item, product, destination = self._lock(session, queue_id)
        entry = self._entry(session, item)
        if entry is not None and entry.status in ("DISPATCHING", "UNCERTAIN"):
            return "deduplicated"
        payload, reason = self._context(session, item, product, destination)
        if reason:
            self._event(session, product.id, destination.id, "FILTERED", reason=reason)
            self._expire(session, item, entry, reason)
            return "filtered"
        digest = _hash(payload)
        if entry is not None and entry.preview_hash == digest:
            self._event(session, product.id, destination.id, "DEDUPLICATED", entry=entry)
            return "deduplicated"
        if entry is None:
            entry = TelegramSimulation(
                queue_item_id=item.id,
                product_id=product.id,
                offer_id=item.offer_id,
                destination_id=destination.id,
                preview_payload=payload,
                preview_hash=digest,
            )
            session.add(entry)
            session.flush()
            self._event(session, product.id, destination.id, "ENQUEUED", entry=entry)
        else:
            entry.offer_id, entry.queue_item_id = item.offer_id, item.id
            entry.preview_payload, entry.preview_hash = payload, digest
            self._event(session, product.id, destination.id, "REVALIDATED", entry=entry)
        entry.status, entry.approved_by, entry.approved_at = "PENDING", None, None
        if payload["policy"]["automatic_approval"]:
            entry.status, entry.approved_by, entry.approved_at = (
                "READY",
                "SIMULATED_AUTO_APPROVAL",
                self._now(),
            )
            self._event(session, product.id, destination.id, "AUTO_APPROVED_SIMULATED", entry=entry)
        session.flush()
        return "enqueued"

    def _lease(self, queue_id):
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
            product_id = session.scalar(
                select(PublicationQueueItem.product_id).where(PublicationQueueItem.id == queue_id)
            )
        name = f"monitor:product:{product_id}"
        return name, acquire_lease(
            self.factory, name, self.settings.monitoring_lease_seconds, self.clock
        )

    def capture(self):
        result = {"enqueued": 0, "filtered": 0, "deduplicated": 0, "deferred": 0}
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
            # The existing queue filters categories before inserting rows. Record
            # those exclusions as well, without recreating or weakening its routing.
            offers = session.execute(
                select(Offer, Product)
                .join(Product, Product.id == Offer.product_id)
                .join(SimulationProduct, SimulationProduct.product_id == Product.id)
                .where(Offer.historical_price > Offer.current_price)
                .order_by(Offer.id.desc())
                .limit(100)
            ).all()
            destinations = list(
                session.scalars(
                    select(PublicationDestination)
                    .join(
                        SimulationPolicy,
                        SimulationPolicy.destination_id == PublicationDestination.id,
                    )
                    .where(
                        PublicationDestination.channel == DestinationChannel.SIMULATED,
                        PublicationDestination.enabled.is_(True),
                        SimulationPolicy.enabled.is_(True),
                    )
                )
            )
            for offer, product in offers:
                history = latest_observation(session, product.id)
                if (
                    history is None
                    or history.id != offer.price_history_id
                    or not history.is_simulated
                ):
                    continue
                for destination in destinations:
                    if (
                        not destination_accepts(session, product, destination)
                        and session.scalar(
                            select(SimulationAudit.id)
                            .where(
                                SimulationAudit.product_id == product.id,
                                SimulationAudit.destination_id == destination.id,
                                SimulationAudit.reason == "CATEGORY_FILTER",
                            )
                            .limit(1)
                        )
                        is None
                    ):
                        self._event(
                            session,
                            product.id,
                            destination.id,
                            "FILTERED",
                            reason="CATEGORY_FILTER",
                        )
                        result["filtered"] += 1
            session.commit()
            ids = list(
                session.scalars(
                    select(PublicationQueueItem.id)
                    .join(
                        SimulationProduct,
                        SimulationProduct.product_id == PublicationQueueItem.product_id,
                    )
                    .where(PublicationQueueItem.active_slot == 1)
                    .order_by(PublicationQueueItem.id)
                    .limit(100)
                )
            )
        for queue_id in ids:
            name, owner = self._lease(queue_id)
            if owner is None:
                result["deferred"] += 1
                continue
            try:
                with self.factory() as session:
                    outcome = self._stage(session, queue_id)
                    if not renew_lease(
                        session, name, owner, self.settings.monitoring_lease_seconds, self.clock
                    ):
                        raise ValueError("Reserva de simulação expirada.")
                    session.commit()
                    result[outcome] += 1
            finally:
                release_lease(self.factory, name, owner)
        return result

    def approve(self, entry_id, digest, actor):
        if not _text(actor, 200):
            raise ValueError("Informe o responsável pela aprovação simulada.")
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
            identity = session.get(TelegramSimulation, entry_id)
            if identity is None:
                raise ValueError("Publicação simulada inexistente.")
            item, product, destination = self._lock(session, identity.queue_item_id)
            entry = self._entry(session, item)
            if entry is None or entry.id != entry_id or entry.status not in ("PENDING", "READY"):
                raise ValueError("A simulação não aceita nova aprovação.")
            payload, reason = self._context(session, item, product, destination)
            if reason or digest != entry.preview_hash or _hash(payload) != digest:
                raise ValueError("Prévia simulada mudou; revalide antes de aprovar.")
            if entry.approved_by is None:
                entry.status, entry.approved_by, entry.approved_at = "READY", actor, self._now()
                self._event(
                    session, product.id, destination.id, "MANUALLY_APPROVED_SIMULATED", entry=entry
                )
            session.commit()

    def _limited(self, session, destination):
        reserved = session.scalar(
            select(TelegramSimulation.id)
            .where(
                TelegramSimulation.destination_id == destination.id,
                TelegramSimulation.status.in_(("DISPATCHING", "UNCERTAIN")),
            )
            .limit(1)
        )
        recent = list(
            session.scalars(
                select(TelegramSimulation.claimed_at).where(
                    TelegramSimulation.destination_id == destination.id,
                    TelegramSimulation.claimed_at > self._now() - timedelta(hours=1),
                )
            )
        )
        return reserved is not None or (
            recent
            and (
                len(recent) >= min(5, destination.max_messages_per_hour)
                or max(recent) + timedelta(seconds=destination.min_interval_seconds) > self._now()
            )
        )

    def dispatch(self, entry_id):
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
            identity = session.get(TelegramSimulation, entry_id)
            if identity is None:
                raise ValueError("Publicação simulada inexistente.")
            queue_id = identity.queue_item_id
        name, owner = self._lease(queue_id)
        if owner is None:
            return "DEFERRED"
        try:
            with self.factory() as session:
                item, product, destination = self._lock(session, queue_id)
                entry = session.scalar(
                    select(TelegramSimulation)
                    .where(TelegramSimulation.id == entry_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    entry.status != "READY"
                    or entry.attempt_number
                    or not entry.approved_by
                    or entry.approved_at is None
                ):
                    if entry.attempt_number:
                        self._event(
                            session,
                            entry.product_id,
                            entry.destination_id,
                            "DEDUPLICATED",
                            entry=entry,
                            reason="ATTEMPT_ALREADY_RESERVED",
                        )
                        session.commit()
                    return entry.status
                payload, reason = self._context(session, item, product, destination)
                if reason:
                    self._expire(session, item, entry, reason)
                    session.commit()
                    return "EXPIRED"
                if _hash(payload) != entry.preview_hash or payload != entry.preview_payload:
                    entry.status, entry.approved_by, entry.approved_at = "PENDING", None, None
                    self._event(session, product.id, destination.id, "REVIEW_REQUIRED", entry=entry)
                    session.commit()
                    return "PENDING"
                if self._limited(session, destination):
                    return "DEFERRED"
                if self.delivery is None:
                    self.delivery = MockTelegramDelivery()
                if type(self.delivery) is not MockTelegramDelivery:
                    raise ValueError(
                        "Somente publisher oficial com transporte mockado é permitido."
                    )
                if not renew_lease(
                    session, name, owner, self.settings.monitoring_lease_seconds, self.clock
                ):
                    return "DEFERRED"
                claim = session.execute(
                    update(TelegramSimulation)
                    .where(
                        TelegramSimulation.id == entry_id,
                        TelegramSimulation.status == "READY",
                        TelegramSimulation.attempt_number == 0,
                        TelegramSimulation.preview_hash == entry.preview_hash,
                    )
                    .values(status="DISPATCHING", attempt_number=1, claimed_at=self._now())
                    .execution_options(synchronize_session=False)
                )
                if claim.rowcount != 1:
                    return "DEFERRED"
                self._event(
                    session, product.id, destination.id, "MOCK_ATTEMPT_RESERVED", entry=entry
                )
                message = RenderedMessage(**payload["message"])
                session.commit()
            response, code, uncertain = None, None, False
            try:
                response = self.delivery.send(message)
            except TelegramPublishError as error:
                code, uncertain = error.code, error.uncertain
            except Exception:
                code, uncertain = "MOCK_DELIVERY_UNCERTAIN", True
            try:
                return self._finish(entry_id, response, code, uncertain, name, owner)
            except SQLAlchemyError:
                return self._uncertain(entry_id, "RESULT_PERSISTENCE_FAILED")
        finally:
            release_lease(self.factory, name, owner)

    def _finish(self, entry_id, response, code, uncertain, name, owner):
        with self.factory() as session:
            identity = session.get(TelegramSimulation, entry_id)
            item, _, _ = self._lock(session, identity.queue_item_id)
            entry = session.scalar(
                select(TelegramSimulation)
                .where(TelegramSimulation.id == entry_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if entry.status != "DISPATCHING":
                return entry.status
            if not renew_lease(
                session, name, owner, self.settings.monitoring_lease_seconds, self.clock
            ):
                response, code, uncertain = None, "MOCK_LEASE_EXPIRED", True
            if item.offer_id != entry.offer_id:
                response, code, uncertain = None, "RESERVED_OFFER_CHANGED", True
            if response is None and code is None:
                code, uncertain = "MOCK_RESPONSE_MISSING", True
            if response is not None and (
                type(response.message_id) is not int
                or response.message_id <= 0
                or response.chat_id != MOCK_CHAT
            ):
                response, code, uncertain = None, "MOCK_RESPONSE_INVALID", True
            if response is not None:
                entry.status, entry.active_slot, entry.simulated_at = "SIMULATED", None, self._now()
                entry.mock_message_id = "simulated:" + str(response.message_id)
                item.status, item.active_slot, item.published_at = (
                    QueueStatus.PUBLISHED,
                    None,
                    self._now(),
                )
                item.attempts += 1
                item.next_eligible_at = self._now() + timedelta(
                    seconds=max(10800, self.settings.queue_republish_seconds)
                )
                # Delivery is per destination. Do not change the shared offer/hash
                # while other destinations still hold approved simulated previews.
            else:
                entry.status, entry.error_code = (
                    ("UNCERTAIN" if uncertain else "FAILED"),
                    code or "MOCK_DELIVERY_UNCERTAIN",
                )
                if not uncertain:
                    entry.active_slot = None
                    self.queue._finish(session, item, None, QueueStatus.FAILED, entry.error_code)
            self._event(
                session,
                entry.product_id,
                entry.destination_id,
                entry.status,
                entry=entry,
                reason=entry.error_code,
            )
            session.commit()
            return entry.status

    def _uncertain(self, entry_id, code):
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
            entry = session.scalar(
                select(TelegramSimulation)
                .where(TelegramSimulation.id == entry_id)
                .with_for_update()
            )
            if entry.status in ("DISPATCHING", "SIMULATED"):
                entry.status, entry.active_slot, entry.error_code = "UNCERTAIN", 1, code
                self._event(
                    session,
                    entry.product_id,
                    entry.destination_id,
                    "UNCERTAIN",
                    entry=entry,
                    reason=code,
                )
            session.commit()
            return entry.status

    def recover(self):
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
            ids = list(
                session.scalars(
                    select(TelegramSimulation.id).where(
                        TelegramSimulation.status == "DISPATCHING",
                        TelegramSimulation.claimed_at
                        <= self._now() - timedelta(seconds=self.settings.monitoring_lease_seconds),
                    )
                )
            )
        for entry_id in ids:
            self._uncertain(entry_id, "ABANDONED_MOCK_CLAIM")
        return len(ids)

    def run_pending(self):
        self.recover()
        result = self.capture()
        result.update(simulated=0, failed=0, uncertain=0)
        with self.factory() as session:
            ids = list(
                session.scalars(
                    select(TelegramSimulation.id)
                    .where(TelegramSimulation.status == "READY")
                    .order_by(TelegramSimulation.id)
                    .limit(100)
                )
            )
        for entry_id in ids:
            outcome = self.dispatch(entry_id).lower()
            if outcome in result:
                result[outcome] += 1
            if (
                result["simulated"] + result["failed"] + result["uncertain"]
                >= self.settings.simulation_batch_limit
            ):
                break
        return result

    def metrics(self):
        with self.factory() as session:
            require_simulation_scope(session, self.settings)

            def count(event):
                return session.scalar(
                    select(func.count(func.distinct(SimulationAudit.product_id))).where(
                        SimulationAudit.event == event
                    )
                )

            return {
                "mode": "SIMULATED",
                "opportunities": session.scalar(
                    select(func.count(func.distinct(Offer.product_id)))
                    .join(ProductPriceHistory, Offer.price_history_id == ProductPriceHistory.id)
                    .join(SimulationProduct, SimulationProduct.product_id == Offer.product_id)
                    .where(
                        ProductPriceHistory.is_simulated.is_(True),
                        Offer.historical_price > Offer.current_price,
                    )
                ),
                "filtered_products": count("FILTERED"),
                "enqueued_products": count("ENQUEUED"),
                "simulated_products": count("SIMULATED"),
                "automatic_approvals": session.scalar(
                    select(func.count())
                    .select_from(SimulationAudit)
                    .where(SimulationAudit.event == "AUTO_APPROVED_SIMULATED")
                ),
                "deduplications": session.scalar(
                    select(func.count())
                    .select_from(SimulationAudit)
                    .where(SimulationAudit.event == "DEDUPLICATED")
                ),
                "mock_calls": self.delivery.calls if self.delivery else 0,
                "real_telegram_calls": 0,
            }
