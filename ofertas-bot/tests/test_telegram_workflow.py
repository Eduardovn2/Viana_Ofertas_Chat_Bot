"""Full outbox + official publisher contract, exclusively using MockTransport."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import httpx
import pytest
import test_api as api_fixtures
import test_manual_offers as manual
from pydantic import SecretStr
from sqlalchemy import func, select

from app.integrations.errors import IntegrationError
from app.integrations.telegram import TelegramPublisher
from app.models import (
    CategoryMode,
    DestinationChannel,
    Product,
    PublicationDestination,
    PublicationLog,
    PublicationStatus,
    TelegramPublication,
)
from app.models import (
    TelegramPublicationStatus as State,
)
from app.services.telegram_destinations import prepare_test_group
from app.services.telegram_publication import TelegramPublicationService

application = api_fixtures.application
client = api_fixtures.client
context = manual.context
NOW = manual.NOW
HEADERS = api_fixtures.HEADERS
TOKEN = "123456789:SYNTHETIC_ONLY_NOT_A_LIVE_TOKEN_123456789"
CHAT = "-1009999999999"


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Somente MockTransport é permitido nos testes Telegram")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setenv("TELEGRAM_PUBLISHER_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_REAL_SENDS_AUTHORIZED", "false")
    monkeypatch.setenv("MERCADO_LIVRE_ENABLED", "false")
    monkeypatch.setenv("SHOPEE_ENABLED", "false")


def success(request):
    chat = json.loads(request.content)["chat_id"]
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {"message_id": 321, "chat": {"id": int(chat), "type": "group"}},
        },
    )


@pytest.fixture
def workflow(context):
    factory, settings, _, _ = context
    clock = [NOW]
    calls = []
    reply = [success]
    enabled = settings.model_copy(
        update={
            "telegram_publisher_enabled": True,
            "telegram_real_sends_authorized": True,
            "telegram_bot_token": SecretStr(TOKEN),
            "telegram_allowed_chat_ids": (CHAT,),
        }
    )

    def handler(request):
        with factory() as session:
            entry = session.scalar(
                select(TelegramPublication).where(TelegramPublication.status == State.SENDING)
            )
            assert entry is not None and entry.active_slot == 1
            assert (
                session.scalar(
                    select(PublicationLog).where(
                        PublicationLog.offer_id == entry.offer_id,
                        PublicationLog.attempt_number == entry.attempt_number,
                    )
                ).status
                == PublicationStatus.PENDING
            )
        calls.append(request)
        return reply[0](request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        publisher = TelegramPublisher(enabled, client=transport)
        service = TelegramPublicationService(enabled, publisher=publisher, clock=lambda: clock[0])
        yield service, calls, reply, clock


def approved(context, service, **changes):
    offer = manual.register(context, **changes)
    with context[0]() as session:
        entry = service.preview(session, offer, context[3])
        session.commit()
        entry_id, digest = entry.id, entry.preview_hash
    service.approve(context[0], entry_id, digest, "Auditor sintético")
    return entry_id, digest


@pytest.mark.parametrize("photo", [False, True])
def test_success_durable_claim_exact_preview_and_no_duplicate(context, workflow, photo):
    service, calls, _, _ = workflow
    entry_id, digest = approved(
        context, service, image_url="https://images.example.com/synthetic.jpg" if photo else None
    )
    result = service.publish(context[0], entry_id, digest)
    assert result.status == State.SENT and result.active_slot is None
    assert result.approved_by == "Auditor sintético"
    assert result.message_id == "321" and result.sent_at == NOW
    payload = json.loads(calls[0].content)
    assert calls[0].url.host == "api.telegram.org"
    assert calls[0].url.path.endswith("/sendPhoto" if photo else "/sendMessage")
    assert payload["caption" if photo else "text"] == result.preview_payload["message"]["text"]
    assert "allow_paid_broadcast" not in payload
    assert service.publish(context[0], entry_id).status == State.SENT
    assert len(calls) == 1
    with context[0]() as session:
        log = session.scalar(select(PublicationLog))
        assert log.status == PublicationStatus.SENT and log.message_id == "321"
        assert TOKEN not in log.message_text


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "TELEGRAM_BAD_REQUEST"),
        (401, "TELEGRAM_UNAUTHORIZED"),
        (403, "TELEGRAM_FORBIDDEN"),
        (429, "TELEGRAM_RATE_LIMITED"),
    ],
)
def test_definitive_failures_sanitized_and_no_automatic_retry(context, workflow, status, code):
    service, calls, reply, _ = workflow
    reply[0] = lambda request: httpx.Response(
        status,
        json={
            "ok": False,
            "error_code": status,
            "description": TOKEN,
            "parameters": {"retry_after": 123},
        },
    )
    entry_id, _ = approved(context, service)
    result = service.publish(context[0], entry_id)
    assert result.status == State.FAILED and result.active_slot is None
    assert result.last_error_code == code
    assert result.retry_at == (NOW + timedelta(seconds=123) if status == 429 else None)
    assert service.publish(context[0], entry_id).status == State.FAILED
    assert len(calls) == 1
    with context[0]() as session:
        log = session.scalar(select(PublicationLog))
        assert log.status == PublicationStatus.FAILED and log.error_message == code
        assert TOKEN not in str(result.preview_payload)


@pytest.mark.parametrize("kind", ["timeout", "network", "server", "malformed", "wrong_chat"])
def test_uncertain_response_retains_slot_no_retry_or_fallback(context, workflow, kind):
    service, calls, reply, _ = workflow

    def response(request):
        if kind == "timeout":
            raise httpx.ReadTimeout(TOKEN)
        if kind == "network":
            raise httpx.ConnectError(TOKEN)
        if kind == "server":
            return httpx.Response(503, text=TOKEN)
        if kind == "malformed":
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 321, "chat": {"id": -99999, "type": "group"}},
            },
        )

    reply[0] = response
    entry_id, _ = approved(context, service, image_url="https://images.example.com/synthetic.jpg")
    result = service.publish(context[0], entry_id)
    assert result.status == State.UNCERTAIN and result.active_slot == 1
    assert result.last_error_code == "DELIVERY_UNCERTAIN"
    assert result.message_id is result.sent_at is None
    assert service.publish(context[0], entry_id).status == State.UNCERTAIN
    assert len(calls) == 1 and calls[0].url.path.endswith("/sendPhoto")
    with context[0]() as session:
        log = session.scalar(select(PublicationLog))
        assert log.status == PublicationStatus.PENDING and log.sent_at is None
        with pytest.raises(IntegrationError) as blocked:
            service.preview(session, result.offer_id, context[3])
    assert blocked.value.code == "OUTBOX_BLOCKED"


def test_missing_result_is_also_uncertain(context, workflow, monkeypatch):
    service, _, _, _ = workflow
    entry_id, _ = approved(context, service)
    monkeypatch.setattr(service.publisher, "send", lambda *args: None)
    assert service.publish(context[0], entry_id).status == State.UNCERTAIN


def test_explicit_text_only_preview_requires_new_approval_before_sending(context, workflow):
    service, calls, _, _ = workflow
    entry_id, original_hash = approved(
        context, service, image_url="https://images.example.com/synthetic.jpg"
    )
    with context[0]() as session:
        entry = session.get(TelegramPublication, entry_id)
        updated = service.preview(session, entry.offer_id, context[3], include_image=False)
        session.commit()
        digest = updated.preview_hash
        assert digest != original_hash and updated.approved_by is None
        assert updated.preview_payload["message"]["photo_url"] is None
    with pytest.raises(IntegrationError):
        service.publish(context[0], entry_id)
    service.approve(context[0], entry_id, digest, "Auditor modo texto")
    assert service.publish(context[0], entry_id).status == State.SENT
    assert len(calls) == 1 and calls[0].url.path.endswith("/sendMessage")


@pytest.mark.parametrize(
    "gate,code",
    [
        ("telegram_publisher_enabled", "PUBLISHER_DISABLED"),
        ("telegram_real_sends_authorized", "SEND_NOT_AUTHORIZED"),
        ("telegram_allowed_chat_ids", "CHAT_NOT_ALLOWED"),
    ],
)
def test_every_dispatch_gate_blocks_before_http_and_attempt(context, workflow, gate, code):
    service, calls, _, _ = workflow
    entry_id, _ = approved(context, service)
    setattr(service.settings, gate, () if gate.endswith("ids") else False)
    with pytest.raises(IntegrationError) as error:
        service.publish(context[0], entry_id)
    assert error.value.code == code
    with context[0]() as session:
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0
    assert not calls


def test_unapproved_stale_and_changed_content_never_reach_http(context, workflow):
    service, calls, _, clock = workflow
    offer = manual.register(context)
    with context[0]() as session:
        entry = service.preview(session, offer, context[3])
        session.commit()
        entry_id, digest = entry.id, entry.preview_hash
    with pytest.raises(IntegrationError) as error:
        service.publish(context[0], entry_id)
    assert error.value.code == "OUTBOX_NOT_APPROVED"
    service.approve(context[0], entry_id, digest, "Auditor")
    with context[0]() as session:
        session.get(Product, context[2]).name = "Sofá sintético alterado"
        session.commit()
    with pytest.raises(IntegrationError) as changed:
        service.publish(context[0], entry_id)
    assert changed.value.code == "OUTBOX_REVIEW_REQUIRED"
    with context[0]() as session:
        entry = session.get(TelegramPublication, entry_id)
        digest = entry.preview_hash
    service.approve(context[0], entry_id, digest, "Auditor")
    clock[0] += timedelta(minutes=11)
    with pytest.raises(IntegrationError) as stale:
        service.publish(context[0], entry_id)
    assert stale.value.code == "MANUAL_EVIDENCE_STALE"
    assert not calls


def test_failed_attempt_requires_new_preview_approval_and_respects_retry_after(context, workflow):
    service, calls, reply, clock = workflow
    reply[0] = lambda request: httpx.Response(
        429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 123}}
    )
    entry_id, _ = approved(context, service)
    previous = service.publish(context[0], entry_id)
    with context[0]() as session:
        next_entry = service.preview(session, previous.offer_id, context[3])
        session.commit()
        next_id, digest = next_entry.id, next_entry.preview_hash
    assert next_id != entry_id
    with pytest.raises(IntegrationError) as unapproved:
        service.publish(context[0], next_id)
    assert unapproved.value.code == "OUTBOX_NOT_APPROVED"
    service.approve(context[0], next_id, digest, "Auditor retry")
    clock[0] += timedelta(seconds=122)
    with pytest.raises(IntegrationError) as limited:
        service.publish(context[0], next_id)
    assert limited.value.code == "OUTBOX_RATE_LIMITED"
    assert len(calls) == 1
    clock[0] += timedelta(seconds=1)
    reply[0] = success
    assert service.publish(context[0], next_id).status == State.SENT
    assert len(calls) == 2
    with context[0]() as session:
        logs = session.scalars(select(PublicationLog).order_by(PublicationLog.id)).all()
        assert [log.attempt_number for log in logs] == [1, 2]
        assert [log.status for log in logs] == [PublicationStatus.FAILED, PublicationStatus.SENT]


def test_republication_requires_three_hours_new_evidence_and_new_approval(context, workflow):
    service, calls, _, clock = workflow
    entry_id, _ = approved(context, service)
    service.publish(context[0], entry_id)
    clock[0] = NOW + timedelta(seconds=10799)
    offer = manual.register(context, now=clock[0])
    with context[0]() as session, pytest.raises(IntegrationError) as cooldown:
        service.preview(session, offer, context[3])
    assert cooldown.value.code == "OUTBOX_COOLDOWN"
    clock[0] += timedelta(seconds=1)
    # Merely waiting does not authorize a send. Original evidence is stale.
    with context[0]() as session, pytest.raises(IntegrationError):
        service.preview(session, 1, context[3])
    with context[0]() as session:
        new = service.preview(session, offer, context[3])
        session.commit()
        new_id, digest = new.id, new.preview_hash
    with pytest.raises(IntegrationError):
        service.publish(context[0], new_id)
    service.approve(context[0], new_id, digest, "Auditor nova verificação")
    assert service.publish(context[0], new_id).status == State.SENT
    assert len(calls) == 2


def test_concurrent_dispatch_claims_only_once(context, workflow):
    service, calls, reply, _ = workflow
    entered, release = Event(), Event()

    def delayed(request):
        entered.set()
        assert release.wait(timeout=10)
        return success(request)

    reply[0] = delayed
    entry_id, _ = approved(context, service)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.publish, context[0], entry_id)
        assert entered.wait(timeout=10)
        try:
            assert (
                executor.submit(service.publish, context[0], entry_id).result(timeout=10).status
                == State.SENDING
            )
        finally:
            release.set()
        assert first.result(timeout=10).status == State.SENT
    assert len(calls) == 1
    with context[0]() as session:
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 1


@pytest.mark.parametrize("outcome", ["SENT", "NOT_SENT"])
def test_crash_after_http_recover_disabled_and_resolve_with_evidence(
    context, workflow, monkeypatch, outcome
):
    service, calls, _, clock = workflow
    entry_id, _ = approved(context, service)

    def unavailable(*args, **kwargs):
        raise RuntimeError("simulated process failure")

    monkeypatch.setattr(service, "_finish", unavailable)
    with pytest.raises(RuntimeError):
        service.publish(context[0], entry_id)
    service.settings.telegram_publisher_enabled = (
        service.settings.telegram_real_sends_authorized
    ) = False
    assert service.recover(context[0], entry_id).status == State.SENDING
    clock[0] += timedelta(seconds=61)
    assert service.recover(context[0], entry_id).status == State.UNCERTAIN
    assert service.recover(context[0], entry_id).active_slot == 1
    for actor, evidence, message_id in (
        [("", "evidence", "321"), ("Auditor", "", "321"), ("Auditor", "evidence", None)]
        if outcome == "SENT"
        else [("", "evidence", None)]
    ):
        with pytest.raises(IntegrationError):
            service.resolve(context[0], entry_id, outcome, actor, evidence, message_id)
    resolved = service.resolve(
        context[0],
        entry_id,
        outcome,
        "Auditor de recuperação",
        "SYNTHETIC verificação manual do resultado",
        "321" if outcome == "SENT" else None,
    )
    assert resolved.status == (State.SENT if outcome == "SENT" else State.REJECTED)
    assert resolved.active_slot is None and resolved.resolution == outcome
    assert resolved.resolved_by == "Auditor de recuperação"
    assert resolved.resolution_evidence.startswith("SYNTHETIC")
    assert resolved.approved_by == "Auditor sintético"
    assert len(calls) == 1


@pytest.mark.parametrize("minimum,maximum,age", [(7200, 20, 3601), (1, 1, 10)])
def test_per_destination_limits_include_failed_attempts_and_long_intervals(
    context, workflow, minimum, maximum, age
):
    service, calls, _, _ = workflow
    with context[0]() as session:
        destination = session.get(PublicationDestination, context[3])
        destination.min_interval_seconds, destination.max_messages_per_hour = minimum, maximum
        session.commit()
    entry_id, _ = approved(context, service)
    with context[0]() as session:
        entry = session.get(TelegramPublication, entry_id)
        session.add(
            PublicationLog(
                offer_id=entry.offer_id,
                product_id=entry.product_id,
                destination_id=entry.destination_id,
                status=PublicationStatus.FAILED,
                attempt_number=1,
                created_at=NOW - timedelta(seconds=age),
                updated_at=NOW - timedelta(seconds=age),
            )
        )
        session.commit()
    with pytest.raises(IntegrationError) as limited:
        service.publish(context[0], entry_id)
    assert limited.value.code == "OUTBOX_RATE_LIMITED"
    assert not calls


def test_approval_for_one_destination_cannot_authorize_another(context, workflow):
    service, calls, _, _ = workflow
    first_id, _ = approved(context, service)
    with context[0]() as session:
        first = session.get(TelegramPublication, first_id)
        second_destination = PublicationDestination(
            name="Outro grupo sintético",
            external_id="-1009999999998",
            channel=DestinationChannel.TELEGRAM,
            category_mode=CategoryMode.ALL,
            enabled=True,
        )
        session.add(second_destination)
        session.flush()
        second = service.preview(session, first.offer_id, second_destination.id)
        session.commit()
        second_id = second.id
    with pytest.raises(IntegrationError) as unapproved:
        service.publish(context[0], second_id)
    assert unapproved.value.code == "OUTBOX_NOT_APPROVED"
    assert not calls


def test_generic_category_route_invalidates_telegram_approval(client, application):
    group, entry = api_preview(client, application)
    result = client.put(
        f"/api/v1/destinations/{group['id']}/categories",
        headers=HEADERS,
        json={"mode": "selected", "category_ids": []},
    )
    assert result.status_code == 200
    assert (
        client.get(f"/api/v1/telegram/publications/{entry['id']}", headers=HEADERS).json()["status"]
        == "EXPIRED"
    )


def test_prepare_group_is_paused_idempotent_and_has_no_publish_permission(client, application):
    url = "/api/v1/telegram/destinations/prepare-test-group"
    assert client.post(url).status_code == 401
    first = client.post(url, headers=HEADERS)
    assert first.status_code == 200, first.text
    group = first.json()
    assert group["name"] == "Viana Ofertas" and group["external_id"] == "-5309308689"
    assert group["enabled"] is False and group["manual_approval_required"] is True
    assert group["category_mode"] == "unconfigured" and group["category_ids"] == []
    assert client.post(url, headers=HEADERS).json() == group
    assert group in client.get("/api/v1/telegram/destinations", headers=HEADERS).json()
    assert (
        client.get(f"/api/v1/telegram/destinations/{group['id']}", headers=HEADERS).json() == group
    )
    assert (
        client.get("/api/v1/telegram/status", headers=HEADERS).json()["automatic_publication"]
        is False
    )
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"external_id": "@channel"},
        {"external_id": "-01"},
        {"external_id": "-9007199254740992"},
        {"manual_approval_required": False},
        {"manual_approval_required": 1},
        {"min_interval_seconds": 0},
        {"max_messages_per_hour": 0},
        {"token": TOKEN},
        {"category_ids": [99999]},
        {"name": "\x00fake"},
    ],
)
def test_invalid_destination_input_rejected(client, change):
    result = client.post(
        "/api/v1/telegram/destinations",
        headers=HEADERS,
        json={"name": "Sintético", "external_id": CHAT, **change},
    )
    assert result.status_code == 422, result.text


def api_preview(client, application):
    offer_response = manual.api_register(client)
    assert offer_response.status_code == 201, offer_response.text
    offer = offer_response.json()
    group = client.post(
        "/api/v1/telegram/destinations",
        headers=HEADERS,
        json={
            "name": "Grupo sintético",
            "external_id": CHAT,
            "enabled": True,
            "category_mode": "all",
        },
    ).json()
    entry = client.post(
        f"/api/v1/manual-offers/{offer['id']}/preview",
        headers=HEADERS,
        json={"destination_id": group["id"]},
    ).json()
    result = client.post(
        f"/api/v1/telegram/publications/{entry['id']}/approve",
        headers=HEADERS,
        json={"actor": "Auditor API", "confirmed": True, "preview_hash": entry["preview_hash"]},
    )
    assert result.status_code == 200, result.text
    return group, result.json()


def test_api_dispatch_disabled_and_destination_changes_revoke_review(client, application):
    group, entry = api_preview(client, application)
    base = f"/api/v1/telegram/publications/{entry['id']}"
    data = {"confirmed": True, "preview_hash": entry["preview_hash"]}
    assert client.post(base + "/dispatch", json=data).status_code == 401
    assert (
        client.post(
            base + "/dispatch", headers=HEADERS, json={**data, "confirmed": False}
        ).status_code
        == 422
    )
    result = client.post(base + "/dispatch", headers=HEADERS, json=data)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "PUBLISHER_DISABLED"
    assert client.get(base + "/logs", headers=HEADERS).json() == []
    assert (
        client.patch(
            f"/api/v1/destinations/{group['id']}",
            headers=HEADERS,
            json={"manual_approval_required": False},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/v1/telegram/destinations/{group['id']}", headers=HEADERS, json={"enabled": False}
        ).status_code
        == 200
    )
    invalidated = client.get(base, headers=HEADERS).json()
    assert invalidated["status"] == "EXPIRED" and invalidated["approved_by"] is None
    assert application.state.settings.telegram_publisher_enabled is False


@pytest.mark.parametrize("timeout", [False, True])
def test_api_dispatch_official_publisher_mocked_and_audit(client, application, timeout):
    _, entry = api_preview(client, application)
    calls = []

    def handler(request):
        calls.append(request)
        if timeout:
            raise httpx.ReadTimeout(TOKEN)
        return success(request)

    # Only this test's settings/client change; .env and runtime remain disabled.
    settings = application.state.settings
    settings.telegram_publisher_enabled = settings.telegram_real_sends_authorized = True
    settings.telegram_bot_token = SecretStr(TOKEN)
    settings.telegram_allowed_chat_ids = (CHAT,)
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        application.state.telegram_publisher._client = transport
        application.state.telegram_publisher._owns_client = False
        base = f"/api/v1/telegram/publications/{entry['id']}"
        data = {"confirmed": True, "preview_hash": entry["preview_hash"]}
        result = client.post(base + "/dispatch", headers=HEADERS, json=data)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == ("UNCERTAIN" if timeout else "SENT")
        assert len(client.get(base + "/logs", headers=HEADERS).json()) == 1
        assert TOKEN not in result.text
        client.post(base + "/dispatch", headers=HEADERS, json=data)
        assert len(calls) == 1
        if timeout:
            settings.telegram_publisher_enabled = settings.telegram_real_sends_authorized = False
            result = client.post(
                base + "/resolve",
                headers=HEADERS,
                json={
                    "confirmed": True,
                    "outcome": "NOT_SENT",
                    "actor": "Auditor",
                    "evidence": "SYNTHETIC conferência do grupo",
                },
            )
            assert result.status_code == 200 and result.json()["status"] == "REJECTED"


def test_category_preferences_and_limits_are_configured_without_enabling_sends(client, application):
    group, entry = api_preview(client, application)
    categories = client.get("/api/v1/categories", headers=HEADERS).json()
    selected = next(row["id"] for row in categories if row["slug"] == "casa-decoracao")
    result = client.patch(
        f"/api/v1/telegram/destinations/{group['id']}",
        headers=HEADERS,
        json={
            "category_mode": "selected",
            "category_ids": [selected],
            "min_interval_seconds": 120,
            "max_messages_per_hour": 5,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["category_ids"] == [selected]
    assert result.json()["min_interval_seconds"] == 120
    assert (
        client.get(f"/api/v1/telegram/publications/{entry['id']}", headers=HEADERS).json()["status"]
        == "EXPIRED"
    )
    assert (
        client.post(
            "/api/v1/telegram/destinations",
            headers=HEADERS,
            json={"name": "Duplicado", "external_id": CHAT},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/telegram/destinations/{group['id']}",
            headers=HEADERS,
            json={"external_id": "-123"},
        ).status_code
        == 422
    )


def test_preparing_existing_group_preserves_filter_configuration(context):
    factory = context[0]
    with factory() as session:
        group = prepare_test_group(session)
        group.category_mode = CategoryMode.ALL
        group.enabled = True
        session.commit()
    with factory() as session:
        group = prepare_test_group(session)
        session.commit()
        assert group.enabled is False and group.category_mode == CategoryMode.ALL
        assert group.manual_approval_required is True
