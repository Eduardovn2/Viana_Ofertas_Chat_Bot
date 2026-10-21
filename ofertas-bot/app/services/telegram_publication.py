"""Explicit approval and durable Telegram outbox. Never called by the scheduler.

The claim and PENDING log commit before the injected publisher is invoked. An
ambiguous result keeps its unique product/destination slot until human resolution.
"""

import hashlib
import hmac
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.integrations.errors import IntegrationError
from app.models import (
    Category,
    DestinationCategory,
    DestinationChannel,
    Product,
    ProductCategory,
    PublicationDestination,
    PublicationLog,
    PublicationStatus,
    TelegramPublication,
)
from app.models import (
    TelegramPublicationStatus as State,
)
from app.services.categories import CategoryService
from app.services.category_routing import destination_accepts
from app.services.comparison import aware_utc

_MESSAGES = {
    "OUTBOX_NOT_FOUND": "Publicação não encontrada.",
    "OUTBOX_INVALID": "Oferta ou destino não atende às condições atuais de publicação.",
    "OUTBOX_REVIEW_REQUIRED": "O conteúdo mudou; revise a prévia e aprove novamente.",
    "OUTBOX_BLOCKED": "Existe publicação em envio ou com resultado incerto para este produto e destino.",
    "OUTBOX_NOT_APPROVED": "A publicação exige aprovação explícita da prévia atual.",
    "OUTBOX_RATE_LIMITED": "O destino ainda está no período de espera entre tentativas.",
    "OUTBOX_COOLDOWN": "Republicação exige três horas e uma nova observação manual após o envio anterior.",
    "OUTBOX_RESOLUTION_INVALID": "A resolução exige resultado, ator, evidência e identificador verificáveis.",
    "OUTBOX_ACTOR_INVALID": "Informe a identificação administrativa do responsável.",
    "PUBLISHER_DISABLED": "O publicador Telegram está desativado.",
    "SEND_NOT_AUTHORIZED": "Envios reais não estão autorizados.",
    "CHAT_NOT_ALLOWED": "O grupo Telegram não está autorizado na configuração local.",
    "PUBLISHER_REQUIRED": "O publicador explícito não foi configurado.",
}
_ACTIVE_REVIEW = (State.PENDING, State.APPROVED)
_TERMINAL = (State.SENT, State.FAILED, State.EXPIRED, State.REJECTED)
_PUBLISHER_CODES = {
    "PUBLISHER_DISABLED",
    "SEND_NOT_AUTHORIZED",
    "MISSING_BOT_TOKEN",
    "CHAT_NOT_ALLOWED",
    "INVALID_MESSAGE",
    "TELEGRAM_BAD_REQUEST",
    "TELEGRAM_UNAUTHORIZED",
    "TELEGRAM_FORBIDDEN",
    "TELEGRAM_RATE_LIMITED",
    "DELIVERY_UNCERTAIN",
}


def _error(code, *, retry_at=None):
    error = IntegrationError(code, _MESSAGES[code])
    error.retry_at = retry_at
    return error


def _text(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value.strip()) <= maximum
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _canonical(value):
    if isinstance(value, datetime):
        return aware_utc(value).isoformat()
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def _row(value):
    return _canonical(
        {column.key: getattr(value, column.key) for column in inspect(value).mapper.column_attrs}
    )


def _hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class TelegramPublicationService:
    def __init__(self, settings, publisher=None, clock=None):
        self.settings = settings
        self.publisher = publisher
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self):
        return aware_utc(self.clock())

    def _context(self, session, offer_id, destination_id, include_image=True):
        from app.services.manual_offers import read_manual_offer
        from app.services.message_templates import MessageData, render_message

        now = self._now()
        try:
            if type(include_image) is not bool:
                raise ValueError
            destination = session.scalar(
                select(PublicationDestination)
                .where(
                    PublicationDestination.id == destination_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                destination is None
                or destination.channel != DestinationChannel.TELEGRAM
                or not destination.enabled
            ):
                raise ValueError
            from app.services.telegram_destinations import validate_chat_id

            validate_chat_id(destination.external_id)
            bundle = read_manual_offer(session, offer_id, self.settings, now)
            CategoryService().classify(session, bundle.product)
            if not destination_accepts(session, bundle.product, destination):
                raise ValueError
            self._cooldown(session, bundle, destination, now)
            rendered = render_message(
                MessageData(
                    bundle.product.name,
                    bundle.offer.current_price,
                    bundle.product.affiliate_link.url,
                    previous_price=bundle.offer.previous_price,
                    coupon_text=bundle.coupon.code
                    if bundle.coupon_evaluation is not None
                    else None,
                    installments_text=bundle.history.installment_text,
                    image_url=bundle.details.image_url if include_image else None,
                )
            )
            categories = session.execute(
                select(ProductCategory, Category)
                .join(
                    Category,
                    Category.id == ProductCategory.category_id,
                )
                .where(ProductCategory.product_id == bundle.product.id)
                .order_by(ProductCategory.category_id)
            ).all()
            destinations = session.execute(
                select(DestinationCategory, Category)
                .join(
                    Category,
                    Category.id == DestinationCategory.category_id,
                )
                .where(DestinationCategory.destination_id == destination.id)
                .order_by(DestinationCategory.category_id)
            ).all()
            payload = {
                "version": 2,
                "include_image": include_image,
                "message": asdict(rendered),
                "chat_id": destination.external_id,
                "product": _row(bundle.product),
                "offer": _row(bundle.offer),
                "history": _row(bundle.history),
                "manual_details": _row(bundle.details),
                "affiliate_link": _row(bundle.product.affiliate_link),
                "destination": _row(destination),
                "product_categories": [
                    [_row(link), _row(category)] for link, category in categories
                ],
                "destination_categories": [
                    [_row(link), _row(category)] for link, category in destinations
                ],
                "coupon": _row(bundle.coupon) if bundle.coupon is not None else None,
                "coupon_products": sorted(product.id for product in bundle.coupon.eligible_products)
                if bundle.coupon is not None
                else [],
            }
            return bundle, destination, _canonical(payload)
        except IntegrationError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise _error("OUTBOX_INVALID") from None

    def _cooldown(self, session, bundle, destination, now):
        sent = session.scalar(
            select(PublicationLog)
            .where(
                PublicationLog.product_id == bundle.product.id,
                PublicationLog.destination_id == destination.id,
                PublicationLog.status == PublicationStatus.SENT,
            )
            .order_by(PublicationLog.sent_at.desc(), PublicationLog.id.desc())
            .limit(1)
        )
        if sent is None:
            return
        if sent.sent_at is None:
            raise _error("OUTBOX_COOLDOWN")
        until = aware_utc(sent.sent_at) + timedelta(
            seconds=max(10800, self.settings.queue_republish_seconds)
        )
        if now < until or aware_utc(bundle.history.observed_at) <= aware_utc(sent.sent_at):
            raise _error("OUTBOX_COOLDOWN", retry_at=until)

    @staticmethod
    def _clear_approval(entry):
        entry.approved_by = None
        entry.approved_at = None

    def preview(self, session, offer_id, destination_id, include_image=True):
        bundle, _, payload = self._context(session, offer_id, destination_id, include_image)
        digest = _hash(payload)
        query = (
            select(TelegramPublication)
            .where(
                TelegramPublication.product_id == bundle.product.id,
                TelegramPublication.destination_id == destination_id,
                TelegramPublication.active_slot == 1,
            )
            .with_for_update()
        )
        entry = session.scalar(query.execution_options(populate_existing=True))
        if entry is None:
            try:
                with session.begin_nested():
                    entry = TelegramPublication(
                        product_id=bundle.product.id,
                        offer_id=offer_id,
                        destination_id=destination_id,
                        status=State.PENDING,
                        active_slot=1,
                        preview_payload=payload,
                        preview_hash=digest,
                    )
                    session.add(entry)
                    session.flush()
            except IntegrityError:
                entry = session.scalar(query.execution_options(populate_existing=True))
                if entry is None:
                    raise _error("OUTBOX_BLOCKED") from None
        if entry.status not in _ACTIVE_REVIEW:
            raise _error("OUTBOX_BLOCKED")
        if entry.preview_hash != digest or entry.offer_id != offer_id:
            entry.offer_id = offer_id
            entry.preview_payload, entry.preview_hash = payload, digest
            entry.status = State.PENDING
            self._clear_approval(entry)
            entry.last_error_code = "PREVIEW_CHANGED"
        session.flush()
        return entry

    @staticmethod
    def _load(session, entry_id):
        # Same lock order as preview/configuration: destination -> product -> outbox.
        identity = session.execute(
            select(TelegramPublication.destination_id, TelegramPublication.product_id).where(
                TelegramPublication.id == entry_id
            )
        ).first()
        if identity is None:
            raise _error("OUTBOX_NOT_FOUND")
        session.scalar(
            select(PublicationDestination)
            .where(PublicationDestination.id == identity.destination_id)
            .with_for_update()
        )
        session.scalar(select(Product).where(Product.id == identity.product_id).with_for_update())
        entry = session.scalar(
            select(TelegramPublication)
            .where(TelegramPublication.id == entry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry is None:
            raise _error("OUTBOX_NOT_FOUND")
        return entry

    @staticmethod
    def _commit(session, entry):
        session.commit()
        session.refresh(entry)
        session.expunge(entry)
        return entry

    def _revalidate(self, session, entry):
        try:
            _, destination, payload = self._context(
                session,
                entry.offer_id,
                entry.destination_id,
                entry.preview_payload.get("include_image", True),
            )
        except IntegrationError as error:
            entry.status, entry.active_slot = State.EXPIRED, None
            entry.last_error_code = error.code
            self._clear_approval(entry)
            self._commit(session, entry)
            raise
        if not hmac.compare_digest(_hash(payload), entry.preview_hash):
            entry.status = State.PENDING
            entry.preview_payload, entry.preview_hash = payload, _hash(payload)
            entry.last_error_code = "PREVIEW_CHANGED"
            self._clear_approval(entry)
            self._commit(session, entry)
            raise _error("OUTBOX_REVIEW_REQUIRED")
        return destination

    def approve(self, factory, entry_id, preview_hash, actor):
        if not _text(actor, 200):
            raise _error("OUTBOX_ACTOR_INVALID")
        with factory() as session:
            entry = self._load(session, entry_id)
            if entry.status not in _ACTIVE_REVIEW:
                raise _error("OUTBOX_NOT_APPROVED")
            self._revalidate(session, entry)
            if (
                not isinstance(preview_hash, str)
                or not re.fullmatch(r"[a-f0-9]{64}", preview_hash)
                or not hmac.compare_digest(entry.preview_hash, preview_hash)
            ):
                raise _error("OUTBOX_REVIEW_REQUIRED")
            # Replaying an approval must not overwrite the original approver.
            if entry.status == State.APPROVED:
                return self._commit(session, entry)
            entry.status = State.APPROVED
            entry.approved_by, entry.approved_at = actor, self._now()
            entry.last_error_code = None
            return self._commit(session, entry)

    def _rate_limit(self, session, destination, now):
        deferred = session.scalar(
            select(func.max(TelegramPublication.retry_at)).where(
                TelegramPublication.destination_id == destination.id,
                TelegramPublication.retry_at > now,
            )
        )
        logs = session.scalars(
            select(PublicationLog)
            .where(
                PublicationLog.destination_id == destination.id,
                PublicationLog.created_at > now - timedelta(hours=1),
            )
            .order_by(PublicationLog.created_at)
        ).all()
        latest = session.scalar(
            select(PublicationLog)
            .where(PublicationLog.destination_id == destination.id)
            .order_by(PublicationLog.created_at.desc(), PublicationLog.id.desc())
            .limit(1)
        )
        until = deferred
        if latest is not None:
            minimum = aware_utc(latest.created_at) + timedelta(
                seconds=destination.min_interval_seconds
            )
            if now < minimum:
                until = max(aware_utc(until), minimum) if until is not None else minimum
        if len(logs) >= destination.max_messages_per_hour:
            hourly = aware_utc(logs[-destination.max_messages_per_hour].created_at) + timedelta(
                hours=1
            )
            until = max(aware_utc(until), hourly) if until is not None else hourly
        if until is not None and aware_utc(until) > now:
            raise _error("OUTBOX_RATE_LIMITED", retry_at=aware_utc(until))

    def _gates(self, chat_id=None):
        if not self.settings.telegram_publisher_enabled:
            raise _error("PUBLISHER_DISABLED")
        if not self.settings.telegram_real_sends_authorized:
            raise _error("SEND_NOT_AUTHORIZED")
        if chat_id is not None and chat_id not in self.settings.telegram_allowed_chat_ids:
            raise _error("CHAT_NOT_ALLOWED")
        if self.publisher is None:
            raise _error("PUBLISHER_REQUIRED")

    def publish(self, factory, entry_id, preview_hash=None):
        self._gates()
        from app.integrations.telegram import TelegramPublishError
        from app.services.message_templates import RenderedMessage

        now = self._now()
        with factory() as session:
            entry = self._load(session, entry_id)
            if preview_hash is not None and not hmac.compare_digest(
                entry.preview_hash, preview_hash
            ):
                raise _error("OUTBOX_REVIEW_REQUIRED")
            if entry.status in _TERMINAL or entry.status == State.UNCERTAIN:
                return self._commit(session, entry)
            if entry.status == State.SENDING:
                if entry.claimed_at is None or aware_utc(entry.claimed_at) <= now - timedelta(
                    seconds=60
                ):
                    entry.status, entry.last_error_code = State.UNCERTAIN, "ABANDONED_CLAIM"
                    log = self._log(session, entry)
                    if log is not None:
                        log.error_message = "ABANDONED_CLAIM"
                return self._commit(session, entry)
            if entry.status != State.APPROVED or not entry.approved_by or entry.approved_at is None:
                raise _error("OUTBOX_NOT_APPROVED")
            destination = self._revalidate(session, entry)
            self._gates(destination.external_id)
            self._rate_limit(session, destination, now)
            attempt = (
                session.scalar(
                    select(func.max(PublicationLog.attempt_number)).where(
                        PublicationLog.offer_id == entry.offer_id,
                        PublicationLog.destination_id == entry.destination_id,
                    )
                )
                or 0
            ) + 1
            claimed = session.execute(
                update(TelegramPublication)
                .where(
                    TelegramPublication.id == entry.id,
                    TelegramPublication.status == State.APPROVED,
                    TelegramPublication.active_slot == 1,
                    TelegramPublication.preview_hash == entry.preview_hash,
                )
                .values(
                    status=State.SENDING,
                    claimed_at=now,
                    attempt_number=attempt,
                    last_error_code=None,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise _error("OUTBOX_BLOCKED")
            log = PublicationLog(
                offer_id=entry.offer_id,
                product_id=entry.product_id,
                destination_id=entry.destination_id,
                attempt_number=attempt,
                status=PublicationStatus.PENDING,
                message_text=entry.preview_payload["message"]["text"],
                created_at=now,
                updated_at=now,
            )
            session.add(log)
            message = RenderedMessage(**entry.preview_payload["message"])
            chat_id = entry.preview_payload["chat_id"]
            session.commit()
        try:
            result = self.publisher.send(chat_id, message)
        except TelegramPublishError as error:
            code = error.code if error.code in _PUBLISHER_CODES else "DELIVERY_UNCERTAIN"
            uncertain = bool(error.uncertain) or code == "DELIVERY_UNCERTAIN"
            return self._finish(
                factory,
                entry_id,
                attempt,
                code=code,
                uncertain=uncertain,
                retry_after=error.retry_after,
            )
        except Exception:
            return self._finish(
                factory, entry_id, attempt, code="DELIVERY_UNCERTAIN", uncertain=True
            )
        return self._finish(factory, entry_id, attempt, result=result)

    def recover(self, factory, entry_id):
        """No HTTP, even with global publishing disabled. Never release a claim."""
        with factory() as session:
            entry = self._load(session, entry_id)
            if entry.status == State.SENDING and (
                entry.claimed_at is None
                or aware_utc(entry.claimed_at) <= self._now() - timedelta(seconds=60)
            ):
                entry.status, entry.last_error_code = State.UNCERTAIN, "ABANDONED_CLAIM"
                log = self._log(session, entry)
                if log is not None:
                    log.error_message = "ABANDONED_CLAIM"
            return self._commit(session, entry)

    @staticmethod
    def _log(session, entry):
        return session.scalar(
            select(PublicationLog)
            .where(
                PublicationLog.offer_id == entry.offer_id,
                PublicationLog.destination_id == entry.destination_id,
                PublicationLog.attempt_number == entry.attempt_number,
            )
            .with_for_update()
        )

    def _finish(
        self,
        factory,
        entry_id,
        attempt,
        *,
        result=None,
        code=None,
        uncertain=False,
        retry_after=None,
    ):
        with factory() as session:
            entry = self._load(session, entry_id)
            if entry.status != State.SENDING or entry.attempt_number != attempt:
                return self._commit(session, entry)
            log = self._log(session, entry)
            if log is None:
                entry.status, entry.last_error_code = State.UNCERTAIN, "MISSING_ATTEMPT_LOG"
                return self._commit(session, entry)
            if result is None and code is None:
                code, uncertain = "DELIVERY_UNCERTAIN", True
            if result is not None:
                message_id = getattr(result, "message_id", None)
                if (
                    type(message_id) is not int
                    or message_id <= 0
                    or str(getattr(result, "chat_id", "")) != entry.preview_payload["chat_id"]
                ):
                    result, code, uncertain = None, "DELIVERY_UNCERTAIN", True
            if result is not None:
                entry.status, entry.active_slot = State.SENT, None
                entry.message_id, entry.sent_at = str(result.message_id), self._now()
                log.status, log.message_id, log.sent_at = (
                    PublicationStatus.SENT,
                    entry.message_id,
                    entry.sent_at,
                )
            else:
                entry.status = State.UNCERTAIN if uncertain else State.FAILED
                entry.active_slot = 1 if uncertain else None
                entry.last_error_code = code
                log.status = PublicationStatus.PENDING if uncertain else PublicationStatus.FAILED
                log.error_message = code
                if type(retry_after) is int and retry_after > 0:
                    try:
                        entry.retry_at = self._now() + timedelta(seconds=retry_after)
                    except OverflowError:
                        entry.retry_at = datetime.max.replace(tzinfo=UTC)
            return self._commit(session, entry)

    def resolve(self, factory, entry_id, outcome, actor, evidence, message_id=None):
        if (
            not _text(actor, 200)
            or not _text(evidence, 2000)
            or outcome not in {"SENT", "NOT_SENT"}
        ):
            raise _error("OUTBOX_RESOLUTION_INVALID")
        if outcome == "SENT" and (
            not isinstance(message_id, str | int)
            or isinstance(message_id, bool)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", str(message_id))
        ):
            raise _error("OUTBOX_RESOLUTION_INVALID")
        with factory() as session:
            entry = self._load(session, entry_id)
            if entry.status != State.UNCERTAIN:
                raise _error("OUTBOX_RESOLUTION_INVALID")
            log = self._log(session, entry)
            if log is None:
                raise _error("OUTBOX_RESOLUTION_INVALID")
            entry.resolved_by, entry.resolved_at = actor, self._now()
            entry.resolution, entry.resolution_evidence = outcome, evidence
            entry.active_slot = None
            if outcome == "SENT":
                entry.status, entry.message_id, entry.sent_at = (
                    State.SENT,
                    str(message_id),
                    self._now(),
                )
                log.status, log.message_id, log.sent_at = (
                    PublicationStatus.SENT,
                    entry.message_id,
                    entry.sent_at,
                )
                log.error_message = None
            else:
                entry.status = State.REJECTED
                log.status, log.error_message = (
                    PublicationStatus.FAILED,
                    "MANUALLY_CONFIRMED_NOT_SENT",
                )
            return self._commit(session, entry)
