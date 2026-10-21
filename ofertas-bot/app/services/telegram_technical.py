"""One-shot administrative technical messages. No scheduler or commercial workflow."""

import hashlib
import hmac
import html
import re
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.integrations.errors import IntegrationError
from app.integrations.telegram import TelegramPublishError
from app.models import (
    PublicationLog,
    TelegramPublication,
)
from app.models import (
    TelegramPublicationStatus as State,
)
from app.models import (
    TelegramTechnicalAudit as Audit,
)
from app.models import (
    TelegramTechnicalPublication as Technical,
)
from app.services.message_templates import RenderedMessage, telegram_text_length
from app.services.telegram_destinations import destination_or_error, validate_chat_id
from app.services.telegram_publication import (
    _PUBLISHER_CODES,
    TelegramPublicationService,
    _hash,
    _row,
    _text,
)


def _error(code="TECHNICAL_INVALID"):
    return IntegrationError(
        code,
        {
            "TECHNICAL_INVALID": "Mensagem técnica, destino ou responsável inválido.",
            "TECHNICAL_REVIEW_REQUIRED": "Revise a prévia e aprove o conteúdo e destino exatos.",
            "TECHNICAL_NOT_APPROVED": "Aprovação explícita obrigatória antes do despacho técnico.",
            "TECHNICAL_ATTEMPT_USED": "Este conteúdo e destino já consumiram a tentativa única.",
            "TECHNICAL_BLOCKED": "Há envio ou resultado incerto reservado neste destino.",
            "TECHNICAL_UNCERTAIN": "Persistência do resultado incerta; reserva mantida, não reenviar.",
            "OUTBOX_NOT_FOUND": "Publicação técnica não encontrada.",
        }[code],
    )


def render_technical(text):
    """Plain administrative text only; escape HTML without changing visible content."""
    if (
        not isinstance(text, str)
        or len(text) > 4096
        or not text.startswith(("🧪 SIMULAÇÃO — ", "AVISO TÉCNICO — "))
        or text != text.strip()
        or any((ord(c) < 32 and c != "\n") or ord(c) == 127 for c in text)
        or re.search(
            r"(?i)(https?\s*:|www\.|t\.me/|meli\.la/|\b[a-z0-9-]+\.(?:com|br|net|org)\b|R\$\s*\d|\bBRL\s*\d|\d\s*%|\d+:\w{20,}|(?:token|secret|password|senha)\s*[:=])",
            text,
        )
    ):
        raise _error()
    rendered = RenderedMessage(html.escape(text, quote=False))
    try:
        if telegram_text_length(rendered.text) > 4096:
            raise ValueError
    except (ValueError, UnicodeError):
        raise _error() from None
    return rendered


class TelegramTechnicalService:
    def __init__(self, settings, publisher=None, clock=None):
        self.common = TelegramPublicationService(settings, publisher, clock)

    def _audit(self, session, entry, event, actor=None, code=None):
        session.add(
            Audit(
                publication_id=entry.id,
                event=event,
                actor=actor,
                preview_hash=entry.preview_hash,
                error_code=code,
                created_at=self.common._now(),
            )
        )

    def _payload(self, destination, text):
        try:
            validate_chat_id(destination.external_id)
        except ValueError:
            raise _error() from None
        if destination.manual_approval_required is not True:
            raise _error()
        if destination.external_id not in self.common.settings.telegram_allowed_chat_ids:
            raise IntegrationError("CHAT_NOT_ALLOWED", "Grupo Telegram fora da allowlist.")
        return {
            "kind": "ADMINISTRATIVE_TECHNICAL",
            "version": 1,
            "destination": _row(destination),
            "chat_id": destination.external_id,
            "text": text,
            "message": asdict(render_technical(text)),
        }

    @staticmethod
    def _actor(actor):
        if not _text(actor, 200) or len(actor) > 200:
            raise _error()

    def preview(self, session, destination_id, text, actor):
        self._actor(actor)
        destination = destination_or_error(session, destination_id)
        payload = self._payload(destination, text)
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        entry = session.scalar(
            select(Technical)
            .where(
                Technical.destination_id == destination_id,
                Technical.content_hash == content_hash,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry is not None and entry.attempt_number:
            raise _error("TECHNICAL_ATTEMPT_USED")
        if entry is None:
            entry = Technical(
                destination_id=destination_id,
                text=text,
                content_hash=content_hash,
                created_by=actor,
                preview_payload=payload,
                preview_hash=_hash(payload),
            )
            session.add(entry)
            session.flush()
            self._audit(session, entry, "PREVIEW_CREATED", actor)
        elif entry.preview_hash != _hash(payload) or entry.status == State.EXPIRED:
            entry.preview_payload, entry.preview_hash = payload, _hash(payload)
            entry.status, entry.approved_by, entry.approved_at = State.PENDING, None, None
            entry.last_error_code = None
            self._audit(session, entry, "PREVIEW_UPDATED", actor)
        session.flush()
        return entry

    @staticmethod
    def _load(session, entry_id):
        destination_id = session.scalar(
            select(Technical.destination_id).where(Technical.id == entry_id)
        )
        if destination_id is None:
            raise _error("OUTBOX_NOT_FOUND")
        destination = destination_or_error(session, destination_id)
        entry = session.scalar(
            select(Technical)
            .where(Technical.id == entry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return entry, destination

    def _revalidate(self, session, entry, destination, destination_id, digest):
        if (
            destination_id != entry.destination_id
            or not isinstance(digest, str)
            or not hmac.compare_digest(entry.preview_hash, digest)
        ):
            raise _error("TECHNICAL_REVIEW_REQUIRED")
        payload = self._payload(destination, entry.text)
        if (
            not hmac.compare_digest(_hash(payload), entry.preview_hash)
            or entry.preview_payload != payload
            or not hmac.compare_digest(
                hashlib.sha256(entry.text.encode()).hexdigest(), entry.content_hash
            )
        ):
            entry.status, entry.approved_by, entry.approved_at = State.EXPIRED, None, None
            entry.last_error_code = "PREVIEW_CHANGED"
            self._audit(session, entry, "REVIEW_INVALIDATED", code="PREVIEW_CHANGED")
            session.commit()
            raise _error("TECHNICAL_REVIEW_REQUIRED")

    def approve(self, factory, entry_id, destination_id, preview_hash, actor):
        self._actor(actor)
        with factory() as session:
            entry, destination = self._load(session, entry_id)
            if entry.attempt_number:
                raise _error("TECHNICAL_ATTEMPT_USED")
            if entry.status not in (State.PENDING, State.APPROVED):
                raise _error("TECHNICAL_REVIEW_REQUIRED")
            self._revalidate(session, entry, destination, destination_id, preview_hash)
            if entry.status != State.APPROVED:
                entry.status, entry.approved_by, entry.approved_at = (
                    State.APPROVED,
                    actor,
                    self.common._now(),
                )
                self._audit(session, entry, "APPROVED", actor)
            return self.common._commit(session, entry)

    def _limits(self, session, destination):
        self.common._rate_limit(session, destination, self.common._now())
        for model in (Technical, TelegramPublication):
            if (
                session.scalar(
                    select(model.id)
                    .where(
                        model.destination_id == destination.id,
                        model.status.in_((State.SENDING, State.UNCERTAIN)),
                    )
                    .limit(1)
                )
                is not None
            ):
                raise _error("TECHNICAL_BLOCKED")
        now = self.common._now()
        technical = list(
            session.scalars(
                select(Technical.claimed_at).where(
                    Technical.destination_id == destination.id,
                    Technical.claimed_at > now - timedelta(hours=1),
                )
            )
        )
        commercial = list(
            session.scalars(
                select(PublicationLog.created_at).where(
                    PublicationLog.destination_id == destination.id,
                    PublicationLog.created_at > now - timedelta(hours=1),
                )
            )
        )
        times = technical + commercial
        if times and (
            max(times) + timedelta(seconds=destination.min_interval_seconds) > now
            or len(times) >= destination.max_messages_per_hour
        ):
            raise IntegrationError("OUTBOX_RATE_LIMITED", "Limite de envio do destino atingido.")

    def publish(self, factory, entry_id, destination_id, preview_hash):
        self.common._gates()
        with factory() as session:
            entry, destination = self._load(session, entry_id)
            if entry.attempt_number:
                raise _error("TECHNICAL_ATTEMPT_USED")
            if entry.status != State.APPROVED or not entry.approved_by or entry.approved_at is None:
                raise _error("TECHNICAL_NOT_APPROVED")
            self._revalidate(session, entry, destination, destination_id, preview_hash)
            if not destination.enabled:
                raise _error()
            self.common._gates(destination.external_id)
            self._limits(session, destination)
            now = self.common._now()
            claimed = session.execute(
                update(Technical)
                .where(
                    Technical.id == entry.id,
                    Technical.status == State.APPROVED,
                    Technical.attempt_number == 0,
                    Technical.preview_hash == preview_hash,
                )
                .values(status=State.SENDING, attempt_number=1, claimed_at=now)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise _error("TECHNICAL_ATTEMPT_USED")
            self._audit(session, entry, "ATTEMPT_RESERVED", entry.approved_by)
            message = RenderedMessage(**entry.preview_payload["message"])
            chat_id = destination.external_id
            # Claim and audit must be durable before the ONLY publisher call.
            session.commit()
        result, code, uncertain = None, None, False
        try:
            result = self.common.publisher.send(chat_id, message)
        except TelegramPublishError as error:
            code = error.code if error.code in _PUBLISHER_CODES else "DELIVERY_UNCERTAIN"
            uncertain = bool(error.uncertain) or code == "DELIVERY_UNCERTAIN"
        except Exception:
            code, uncertain = "DELIVERY_UNCERTAIN", True
        try:
            return self._finish(factory, entry_id, result, code, uncertain)
        except SQLAlchemyError:
            # Never invoke the publisher again, even if recording its result failed.
            try:
                return self._mark_uncertain(factory, entry_id, "RESULT_PERSISTENCE_FAILED")
            except SQLAlchemyError:
                raise _error("TECHNICAL_UNCERTAIN") from None

    def _finish(self, factory, entry_id, result, code, uncertain):
        with factory() as session:
            entry, _ = self._load(session, entry_id)
            if entry.status != State.SENDING:
                return self.common._commit(session, entry)
            if result is not None and (
                type(getattr(result, "message_id", None)) is not int
                or result.message_id <= 0
                or str(getattr(result, "chat_id", "")) != entry.preview_payload["chat_id"]
            ):
                result, code, uncertain = None, "DELIVERY_UNCERTAIN", True
            if result is not None:
                entry.status, entry.message_id, entry.sent_at = (
                    State.SENT,
                    str(result.message_id),
                    self.common._now(),
                )
            else:
                code = code or "DELIVERY_UNCERTAIN"
                entry.status = (
                    State.UNCERTAIN if uncertain or code == "DELIVERY_UNCERTAIN" else State.FAILED
                )
                entry.last_error_code = code
            self._audit(session, entry, entry.status.value, code=entry.last_error_code)
            return self.common._commit(session, entry)

    def _mark_uncertain(self, factory, entry_id, code):
        with factory() as session:
            entry, _ = self._load(session, entry_id)
            if entry.status in (State.SENDING, State.SENT):
                entry.status, entry.last_error_code = State.UNCERTAIN, code
                self._audit(session, entry, "UNCERTAIN", code=code)
            return self.common._commit(session, entry)

    def recover(self, factory, entry_id):
        """No send, no reset and no retry, including when global flags are false."""
        with factory() as session:
            entry, _ = self._load(session, entry_id)
            if entry.status == State.SENDING and (
                entry.claimed_at is None
                or entry.claimed_at <= self.common._now() - timedelta(seconds=60)
            ):
                entry.status, entry.last_error_code = State.UNCERTAIN, "ABANDONED_CLAIM"
                self._audit(session, entry, "UNCERTAIN", code="ABANDONED_CLAIM")
            return self.common._commit(session, entry)
