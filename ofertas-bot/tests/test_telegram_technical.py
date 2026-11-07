"""Technical outbox safety contracts; every Telegram request uses MockTransport."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import httpx
import pytest
import test_api as api
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.errors import IntegrationError
from app.integrations.telegram import TelegramPublisher
from app.models import (
    Base,
    DestinationChannel,
    Offer,
    Product,
    PublicationDestination,
    PublicationLog,
    PublicationQueueItem,
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
from app.services.telegram_destinations import configure_destination
from app.services.telegram_technical import TelegramTechnicalService, render_technical

application = api.application
client = api.client
CHAT = "-1009999999999"
TOKEN = "123456789:SYNTHETIC_ONLY_NOT_A_LIVE_TOKEN_123456789"
TEXT = "🧪 SIMULAÇÃO — TESTE\n\nMensagem técnica administrativa, sem oferta real."
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
ROOT = "/api/v1/telegram/technical-publications"


@pytest.fixture(autouse=True)
def no_live_telegram(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("API Telegram real proibida: use MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def setup_context(engine):
    factory = build_session_factory(engine)
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        telegram_bot_token=SecretStr(TOKEN),
        telegram_allowed_chat_ids=(CHAT,),
        telegram_publisher_enabled=True,
        telegram_real_sends_authorized=True,
    )
    with factory() as session:
        destination = PublicationDestination(
            name="SYNTHETIC técnico",
            external_id=CHAT,
            channel=DestinationChannel.TELEGRAM,
            enabled=False,
            manual_approval_required=True,
        )
        session.add(destination)
        session.commit()
        destination_id = destination.id
    return factory, settings, destination_id


@pytest.fixture
def context(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'technical.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield setup_context(engine)
    engine.dispose()


def success(request):
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {"message_id": 456, "chat": {"id": int(CHAT), "type": "group"}},
        },
    )


@contextmanager
def workflow(context):
    factory, settings, _ = context
    calls, response, clock = [], [success], [NOW]

    def handler(request):
        calls.append(request)
        with factory() as session:
            entry = session.scalar(select(Technical).where(Technical.status == State.SENDING))
            assert entry is not None and entry.attempt_number == 1
            assert (
                session.scalar(
                    select(Audit).where(
                        Audit.publication_id == entry.id, Audit.event == "ATTEMPT_RESERVED"
                    )
                )
                is not None
            )
        assert request.url.path.endswith("/sendMessage")
        return response[0](request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        service = TelegramTechnicalService(
            settings, TelegramPublisher(settings, client=transport), clock=lambda: clock[0]
        )
        yield service, calls, response, clock


def preview(context, service, text=TEXT):
    with context[0]() as session:
        entry = service.preview(session, context[2], text, "Auditor sintético")
        session.commit()
        return entry.id, entry.preview_hash


def approve(context, service, text=TEXT, enable=True):
    if enable:
        with context[0]() as session:
            configure_destination(
                session, destination=session.get(PublicationDestination, context[2]), enabled=True
            )
            session.commit()
    entry_id, digest = preview(context, service, text)
    service.approve(context[0], entry_id, context[2], digest, "Aprovador sintético")
    return entry_id, digest


def test_success_exact_content_durable_claim_and_audit(context):
    with workflow(context) as (service, calls, _, _):
        entry_id, digest = approve(context, service)
        result = service.publish(context[0], entry_id, context[2], digest)
        assert (
            result.status == State.SENT
            and result.message_id == "456"
            and result.attempt_number == 1
        )
        body = json.loads(calls[0].content)
        assert body["text"] == TEXT and body["chat_id"] == CHAT and body["parse_mode"] == "HTML"
        assert "photo" not in body and len(calls) == 1
        with pytest.raises(IntegrationError, match="tentativa única"):
            service.publish(context[0], entry_id, context[2], digest)
        with pytest.raises(IntegrationError):
            preview(context, service)
        with context[0]() as session:
            events = session.scalars(
                select(Audit).where(Audit.publication_id == entry_id).order_by(Audit.id)
            ).all()
            assert [event.event for event in events] == [
                "PREVIEW_CREATED",
                "APPROVED",
                "ATTEMPT_RESERVED",
                "SENT",
            ]
            assert events[1].actor == "Aprovador sintético"
            assert all(event.preview_hash == digest for event in events)
            for model in (
                Product,
                Offer,
                TelegramPublication,
                PublicationLog,
                PublicationQueueItem,
            ):
                assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "reply,expected",
    [
        (
            lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("sensitive diagnostic")),
            State.UNCERTAIN,
        ),
        (lambda r: httpx.Response(503), State.UNCERTAIN),
        (lambda r: httpx.Response(200, json={"ok": True}), State.UNCERTAIN),
        (
            lambda r: httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 1, "chat": {"id": -2, "type": "group"}}},
            ),
            State.UNCERTAIN,
        ),
        (
            lambda r: httpx.Response(
                403, json={"ok": False, "error_code": 403, "description": TOKEN}
            ),
            State.FAILED,
        ),
        (
            lambda r: httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 10}}
            ),
            State.FAILED,
        ),
    ],
)
def test_failure_timeout_no_retry_no_new_preview(context, reply, expected):
    with workflow(context) as (service, calls, response, clock):
        entry_id, digest = approve(context, service)
        response[0] = reply
        result = service.publish(context[0], entry_id, context[2], digest)
        assert result.status == expected and result.attempt_number == 1
        assert result.message_id is result.sent_at is None
        assert TOKEN not in (result.last_error_code or "")
        clock[0] += timedelta(days=30)
        with pytest.raises(IntegrationError):
            service.publish(context[0], entry_id, context[2], digest)
        with pytest.raises(IntegrationError):
            preview(context, service)
        assert len(calls) == 1
        with context[0]() as session:
            assert (
                session.scalars(select(Audit.event).order_by(Audit.id)).all()[-1] == expected.value
            )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("telegram_publisher_enabled", False, "PUBLISHER_DISABLED"),
        ("telegram_real_sends_authorized", False, "SEND_NOT_AUTHORIZED"),
        ("telegram_allowed_chat_ids", (), "CHAT_NOT_ALLOWED"),
    ],
)
def test_gates_fail_closed_without_consuming_attempt(context, field, value, code):
    with workflow(context) as (service, calls, _, _):
        entry_id, digest = approve(context, service)
        setattr(service.common.settings, field, value)
        with pytest.raises(IntegrationError) as error:
            service.publish(context[0], entry_id, context[2], digest)
        assert error.value.code == code and calls == []
        with context[0]() as session:
            assert session.get(Technical, entry_id).attempt_number == 0


def test_preview_approval_allowed_paused_but_dispatch_blocked(context):
    with workflow(context) as (service, calls, _, _):
        entry_id, digest = approve(context, service, enable=False)
        with pytest.raises(IntegrationError):
            service.publish(context[0], entry_id, context[2], digest)
        assert calls == []
        with context[0]() as session:
            assert session.get(PublicationDestination, context[2]).enabled is False


def test_approval_required_exact_hash_destination_and_preserves_original_actor(context):
    with workflow(context) as (service, calls, _, _):
        entry_id, digest = preview(context, service)
        with pytest.raises(IntegrationError) as error:
            service.publish(context[0], entry_id, context[2], digest)
        assert error.value.code == "TECHNICAL_NOT_APPROVED"
        for dest, hash_value in ((context[2] + 1, digest), (context[2], "a" * 64)):
            with pytest.raises(IntegrationError):
                service.approve(context[0], entry_id, dest, hash_value, "Auditor")
        first = service.approve(context[0], entry_id, context[2], digest, "Auditor original")
        replay = service.approve(context[0], entry_id, context[2], digest, "Outro auditor")
        assert replay.approved_by == first.approved_by == "Auditor original"
        assert calls == []


@pytest.mark.parametrize("change", ["configuration", "raw_text", "raw_payload"])
def test_mutation_invalidates_approval(context, change):
    with workflow(context) as (service, calls, _, _):
        entry_id, digest = approve(context, service)
        with context[0]() as session:
            if change == "configuration":
                configure_destination(
                    session,
                    destination=session.get(PublicationDestination, context[2]),
                    name="Novo nome",
                )
            else:
                entry = session.get(Technical, entry_id)
                if change == "raw_text":
                    entry.text += " modificado"
                else:
                    entry.preview_payload = {**entry.preview_payload, "chat_id": "-2"}
            session.commit()
        with pytest.raises(IntegrationError):
            service.publish(context[0], entry_id, context[2], digest)
        assert calls == []
        with context[0]() as session:
            entry = session.get(Technical, entry_id)
            assert entry.status == State.EXPIRED and entry.approved_by is None


@pytest.mark.parametrize(
    "text",
    [
        "Oferta comercial",
        "",
        "🧪 SIMULAÇÃO — x\nhttps://example.com",
        "🧪 SIMULAÇÃO — x\nR$ 10,00",
        "🧪 SIMULAÇÃO — x\n20%",
        "🧪 SIMULAÇÃO — x\nsenha=abc",
        "🧪 SIMULAÇÃO — x\n" + TOKEN,
        "🧪 SIMULAÇÃO — x\x00",
        "🧪 SIMULAÇÃO — " + "😀" * 2500,
        "🧪 SIMULAÇÃO — \ud800",
    ],
)
def test_technical_validation_rejects_unsafe_commercial_or_invalid_text(text):
    with pytest.raises(IntegrationError):
        render_technical(text)


def test_exact_requested_preview_and_plain_text_escaping():
    text = (
        (Path(__file__).resolve().parents[1] / "docs/PREVIA_TESTE_TELEGRAM.txt")
        .read_text(encoding="utf-8")
        .rstrip("\r\n")
    )
    assert render_technical(text).text == text
    assert (
        hashlib.sha256(text.encode()).hexdigest()
        == "2aa7bceb17fe76ccfcb202ce9106d0d44c76a9b12d785dda0cdb990d22b88963"
    )
    assert (
        render_technical("AVISO TÉCNICO — <b>texto & aviso</b>").text
        == "AVISO TÉCNICO — &lt;b&gt;texto &amp; aviso&lt;/b&gt;"
    )


def test_concurrent_dispatch_performs_one_publisher_call(context):
    with workflow(context) as (service, calls, response, _):
        entry_id, digest = approve(context, service)
        entered, release = Event(), Event()

        def delayed(request):
            entered.set()
            assert release.wait(10)
            return success(request)

        response[0] = delayed
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(service.publish, context[0], entry_id, context[2], digest)
            assert entered.wait(10)
            try:
                with pytest.raises(IntegrationError):
                    workers.submit(
                        service.publish, context[0], entry_id, context[2], digest
                    ).result(10)
            finally:
                release.set()
            assert first.result(10).status == State.SENT
        assert len(calls) == 1


def test_persistence_failure_after_http_reserves_uncertain(context, monkeypatch):
    with workflow(context) as (service, calls, _, _):
        entry_id, digest = approve(context, service)
        original = service.common._commit

        def fail_once(session, entry):
            if entry.status == State.SENT:
                raise OperationalError("synthetic persistence failure", {}, None)
            return original(session, entry)

        monkeypatch.setattr(service.common, "_commit", fail_once)
        result = service.publish(context[0], entry_id, context[2], digest)
        assert (
            result.status == State.UNCERTAIN
            and result.last_error_code == "RESULT_PERSISTENCE_FAILED"
        )
        assert result.message_id is None and len(calls) == 1
        with pytest.raises(IntegrationError):
            service.publish(context[0], entry_id, context[2], digest)


def test_persistent_database_failure_keeps_claim_for_recovery(context, monkeypatch):
    with workflow(context) as (service, calls, _, clock):
        entry_id, digest = approve(context, service)

        def unavailable(*args, **kwargs):
            raise OperationalError("synthetic persistence failure", {}, None)

        monkeypatch.setattr(service, "_finish", unavailable)
        monkeypatch.setattr(service, "_mark_uncertain", unavailable)
        with pytest.raises(IntegrationError) as error:
            service.publish(context[0], entry_id, context[2], digest)
        assert error.value.code == "TECHNICAL_UNCERTAIN" and len(calls) == 1
        with context[0]() as session:
            assert session.get(Technical, entry_id).status == State.SENDING
        clock[0] += timedelta(seconds=61)
        service.common.settings.telegram_publisher_enabled = False
        result = service.recover(context[0], entry_id)
        assert result.status == State.UNCERTAIN and result.attempt_number == 1


def test_ambiguous_result_commit_is_marked_uncertain_without_resending(context, monkeypatch):
    with workflow(context) as (service, calls, _, _):
        entry_id, digest = approve(context, service)
        original = service.common._commit

        def committed_then_lost(session, entry):
            result = original(session, entry)
            if result.status == State.SENT:
                raise OperationalError("synthetic post-commit failure", {}, None)
            return result

        monkeypatch.setattr(service.common, "_commit", committed_then_lost)
        result = service.publish(context[0], entry_id, context[2], digest)
        assert result.status == State.UNCERTAIN and len(calls) == 1
        assert result.last_error_code == "RESULT_PERSISTENCE_FAILED"
        with pytest.raises(IntegrationError):
            service.publish(context[0], entry_id, context[2], digest)


@pytest.mark.parametrize("interval,hourly,advance", [(120, 20, 61), (1, 1, 120)])
def test_destination_limits_also_count_technical_attempts(context, interval, hourly, advance):
    with workflow(context) as (service, calls, _, clock):
        with context[0]() as session:
            destination = session.get(PublicationDestination, context[2])
            configure_destination(
                session,
                destination=destination,
                min_interval_seconds=interval,
                max_messages_per_hour=hourly,
            )
            session.commit()
        entry_id, digest = approve(context, service)
        service.publish(context[0], entry_id, context[2], digest)
        clock[0] += timedelta(seconds=advance)
        another, new_hash = approve(context, service, TEXT + " Outro aviso.")
        with pytest.raises(IntegrationError) as error:
            service.publish(context[0], another, context[2], new_hash)
        assert error.value.code == "OUTBOX_RATE_LIMITED" and len(calls) == 1


def test_uncertain_reservation_blocks_different_content(context):
    with workflow(context) as (service, calls, response, clock):
        entry_id, digest = approve(context, service)
        response[0] = lambda r: httpx.Response(500)
        assert service.publish(context[0], entry_id, context[2], digest).status == State.UNCERTAIN
        clock[0] += timedelta(hours=4)
        another, new_hash = approve(context, service, TEXT + " Outro teste.")
        with pytest.raises(IntegrationError) as error:
            service.publish(context[0], another, context[2], new_hash)
        assert error.value.code == "TECHNICAL_BLOCKED" and len(calls) == 1


def test_database_enforces_unique_content_single_attempt_and_approval(context):
    with workflow(context) as (service, _, _, _):
        entry_id, _ = preview(context, service)
        with context[0]() as session:
            entry = session.get(Technical, entry_id)
            entry.attempt_number = 2
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            entry = session.get(Technical, entry_id)
            entry.status = State.APPROVED
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            entry = session.get(Technical, entry_id)
            session.add(
                Technical(
                    destination_id=entry.destination_id,
                    text=entry.text,
                    content_hash=entry.content_hash,
                    preview_payload=entry.preview_payload,
                    preview_hash=entry.preview_hash,
                    created_by="Outro",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()


def test_api_authentication_preview_approval_listing_no_implicit_send(application, client):
    assert client.get(ROOT).status_code == 401
    assert client.post(ROOT + "/preview", json={}).status_code == 401
    settings = application.state.settings
    settings.telegram_allowed_chat_ids = (CHAT,)
    with application.state.session_factory() as session:
        destination = configure_destination(session, name="SYNTHETIC", external_id=CHAT)
        session.commit()
        destination_id = destination.id
    payload = {"destination_id": destination_id, "text": TEXT, "actor": "Auditor"}
    created = client.post(ROOT + "/preview", json=payload, headers=api.HEADERS)
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["kind"] == "ADMINISTRATIVE_TECHNICAL" and "offer_id" not in entry
    endpoint = ROOT + f"/{entry['id']}"
    approval = {
        "destination_id": destination_id,
        "preview_hash": entry["preview_hash"],
        "actor": "Aprovador",
        "confirmed": True,
    }
    assert (
        client.post(
            endpoint + "/approve", json={**approval, "confirmed": False}, headers=api.HEADERS
        ).status_code
        == 422
    )
    assert (
        client.post(endpoint + "/approve", json=approval, headers=api.HEADERS).json()["status"]
        == "APPROVED"
    )
    assert client.get(endpoint, headers=api.HEADERS).json()["text"] == TEXT
    assert len(client.get(ROOT, headers=api.HEADERS).json()) == 1
    assert [r["event"] for r in client.get(endpoint + "/audit", headers=api.HEADERS).json()] == [
        "PREVIEW_CREATED",
        "APPROVED",
    ]
    dispatch = {
        "destination_id": destination_id,
        "preview_hash": entry["preview_hash"],
        "confirmed": True,
    }
    assert (
        client.post(endpoint + "/dispatch", json=dispatch, headers=api.HEADERS).status_code == 409
    )
    assert (
        client.post(
            ROOT + "/preview", json={**payload, "product_id": 1}, headers=api.HEADERS
        ).status_code
        == 422
    )
    assert client.get(ROOT + "/999999", headers=api.HEADERS).status_code == 404
    assert client.get("/api/v1/telegram/publications", headers=api.HEADERS).json() == []
    assert settings.telegram_publisher_enabled is settings.telegram_real_sends_authorized is False


def test_api_mocked_dispatch_requires_confirmed_hash_destination(application, client):
    settings = application.state.settings
    settings.telegram_allowed_chat_ids = (CHAT,)
    settings.telegram_bot_token = SecretStr(TOKEN)
    settings.telegram_publisher_enabled = settings.telegram_real_sends_authorized = True
    with application.state.session_factory() as session:
        destination = configure_destination(
            session, name="SYNTHETIC", external_id=CHAT, enabled=True
        )
        session.commit()
        destination_id = destination.id
    calls = []

    def handler(request):
        calls.append(request)
        return success(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        application.state.telegram_technical = TelegramTechnicalService(
            settings, TelegramPublisher(settings, client=transport)
        )
        entry = client.post(
            ROOT + "/preview",
            json={"destination_id": destination_id, "text": TEXT, "actor": "Auditor"},
            headers=api.HEADERS,
        ).json()
        endpoint = ROOT + f"/{entry['id']}"
        body = {
            "destination_id": destination_id,
            "preview_hash": entry["preview_hash"],
            "confirmed": True,
        }
        assert (
            client.post(endpoint + "/dispatch", json=body, headers=api.HEADERS).status_code == 409
        )
        client.post(
            endpoint + "/approve", json={**body, "actor": "Aprovador"}, headers=api.HEADERS
        ).raise_for_status()
        for changes in (
            {"confirmed": False},
            {"destination_id": destination_id + 1},
            {"preview_hash": "a" * 64},
        ):
            assert client.post(
                endpoint + "/dispatch", json={**body, **changes}, headers=api.HEADERS
            ).status_code in (409, 422)
        assert calls == []
        sent = client.post(endpoint + "/dispatch", json=body, headers=api.HEADERS)
        assert sent.status_code == 200 and sent.json()["status"] == "SENT"
        assert (
            client.post(endpoint + "/dispatch", json=body, headers=api.HEADERS).status_code == 409
        )
        assert len(calls) == 1
