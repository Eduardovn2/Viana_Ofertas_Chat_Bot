"""Telegram Bot API contracts with SYNTHETIC tokens and httpx.MockTransport only."""

import json
import logging
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.integrations.telegram import TelegramPublisher, TelegramPublishError
from app.services.message_templates import MessageData, RenderedMessage, render_message

TOKEN = "123456789:SYNTHETIC_ONLY_NOT_A_LIVE_TOKEN_123456789"
CHAT = "-100123456789"
MESSAGE = render_message(
    MessageData("SYNTHETIC produto", Decimal("12.50"), "https://meli.la/SYNTHETIC")
)


def settings(**kwargs):
    return SimpleNamespace(
        **{
            "telegram_publisher_enabled": True,
            "telegram_real_sends_authorized": True,
            "telegram_bot_token": SecretStr(TOKEN),
            "telegram_allowed_chat_ids": [CHAT],
        }
        | kwargs
    )


def success(chat=CHAT, message_id=321, chat_type="supergroup"):
    return {
        "ok": True,
        "result": {"message_id": message_id, "chat": {"id": int(chat), "type": chat_type}},
    }


@pytest.fixture
def make_publisher():
    clients = []

    def factory(response=None, *, config=None, fault=None, handler=None):
        calls = []

        def route(request):
            calls.append(request)
            if handler:
                handler(request)
            if fault:
                raise fault(request)
            return response if response is not None else httpx.Response(200, json=success())

        client = httpx.Client(transport=httpx.MockTransport(route))
        clients.append(client)
        return TelegramPublisher(config or settings(), client=client), calls

    yield factory
    for client in clients:
        client.close()


def test_constructor_and_disabled_send_do_not_even_create_http_client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("HTTP client should not be created")

    monkeypatch.setattr(httpx, "Client", forbidden)
    publisher = TelegramPublisher(settings(telegram_publisher_enabled=False))
    publisher.close()
    with pytest.raises(TelegramPublishError) as caught:
        publisher.send(CHAT, MESSAGE)
    assert caught.value.code == "PUBLISHER_DISABLED"
    assert caught.value.uncertain is False


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"telegram_publisher_enabled": False}, "PUBLISHER_DISABLED"),
        ({"telegram_real_sends_authorized": False}, "SEND_NOT_AUTHORIZED"),
        ({"telegram_allowed_chat_ids": []}, "CHAT_NOT_ALLOWED"),
        ({"telegram_bot_token": None}, "MISSING_BOT_TOKEN"),
        ({"telegram_bot_token": TOKEN}, "MISSING_BOT_TOKEN"),
        ({"telegram_bot_token": SecretStr("123:unsafe/secret")}, "MISSING_BOT_TOKEN"),
    ],
)
def test_all_gates_reject_before_any_post(make_publisher, changes, code):
    publisher, calls = make_publisher(config=settings(**changes))
    with pytest.raises(TelegramPublishError) as caught:
        publisher.send(CHAT, MESSAGE)
    assert caught.value.code == code
    assert not caught.value.uncertain
    assert calls == []


@pytest.mark.parametrize(
    "chat",
    [
        "@channel",
        "123",
        "0",
        "-0",
        "-01",
        " -123",
        "-123 ",
        True,
        0,
        None,
        "-9999999999999999999999",
    ],
)
def test_invalid_or_non_group_ids_never_reach_api(make_publisher, chat):
    publisher, calls = make_publisher()
    with pytest.raises(TelegramPublishError, match="Grupo"):
        publisher.send(chat, MESSAGE)
    assert calls == []


def test_send_message_exact_approved_payload_and_fixed_origin(make_publisher):
    publisher, calls = make_publisher()
    result = publisher.send(CHAT, MESSAGE)
    assert result.message_id == 321 and result.chat_id == CHAT
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert request.url.scheme == "https" and request.url.host == "api.telegram.org"
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    assert not request.url.query
    assert json.loads(request.content) == {
        "chat_id": CHAT,
        "text": MESSAGE.text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }


def test_send_photo_exact_preview_without_fallback(make_publisher):
    photo = replace(MESSAGE, photo_url="https://http2.mlstatic.com/SYNTHETIC.jpg")
    publisher, calls = make_publisher()
    publisher.send(CHAT, photo)
    assert len(calls) == 1 and calls[0].url.path.endswith("/sendPhoto")
    assert json.loads(calls[0].content) == {
        "chat_id": CHAT,
        "photo": photo.photo_url,
        "caption": photo.text,
        "parse_mode": "HTML",
    }


@pytest.mark.parametrize(
    "message",
    [
        None,
        RenderedMessage(""),
        RenderedMessage("<script>x</script>"),
        RenderedMessage("x", parse_mode="Markdown"),
        RenderedMessage("x" * 4097),
        RenderedMessage("x" * 1025, photo_url="https://http2.mlstatic.com/x"),
        RenderedMessage("x", photo_url="http://127.0.0.1/x"),
        RenderedMessage("x", photo_url="https://http2.mlstatic.com/x", image_omitted=True),
    ],
)
def test_bad_preview_cannot_be_posted(make_publisher, message):
    publisher, calls = make_publisher()
    with pytest.raises(TelegramPublishError) as caught:
        publisher.send(CHAT, message)
    assert caught.value.code == "INVALID_MESSAGE"
    assert not caught.value.uncertain
    assert calls == []


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "TELEGRAM_BAD_REQUEST"),
        (401, "TELEGRAM_UNAUTHORIZED"),
        (403, "TELEGRAM_FORBIDDEN"),
        (429, "TELEGRAM_RATE_LIMITED"),
    ],
)
@pytest.mark.parametrize("http_status", [200, None])
def test_known_api_rejection_is_definitive_and_sanitized(make_publisher, status, code, http_status):
    response = httpx.Response(
        http_status or status,
        json={
            "ok": False,
            "error_code": status,
            "description": f"private {TOKEN}",
            "parameters": {"retry_after": 3600},
        },
    )
    publisher, calls = make_publisher(response)
    with pytest.raises(TelegramPublishError) as caught:
        publisher.send(CHAT, MESSAGE)
    assert caught.value.code == code
    assert caught.value.status == status
    assert caught.value.retry_after == (3600 if status == 429 else None)
    assert caught.value.uncertain is False
    assert TOKEN not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"ok": False, "error_code": 500}),
        httpx.Response(503, json=success()),
        httpx.Response(200, text="invalid JSON secret"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"ok": "true", "result": {}}),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, json={"ok": False, "error_code": 500}),
        httpx.Response(403, json={"ok": False, "error_code": 400}),
        httpx.Response(302, headers={"Location": "https://example.com/bot-secret"}),
        httpx.Response(200, json=success(chat="-100987654321")),
        httpx.Response(200, json=success(message_id=True)),
        httpx.Response(200, json=success(message_id=0)),
        httpx.Response(200, json=success(chat_type="channel")),
        httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 321, "chat": {"id": CHAT, "type": "supergroup"}},
            },
        ),
    ],
)
def test_ambiguous_delivery_never_retries_or_follows_redirects(make_publisher, response):
    publisher, calls = make_publisher(response)
    with pytest.raises(TelegramPublishError) as caught:
        publisher.send(CHAT, MESSAGE)
    assert caught.value.code == "DELIVERY_UNCERTAIN"
    assert caught.value.uncertain is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError, RuntimeError]
)
def test_network_or_client_exception_is_uncertain_and_does_not_leak_raw_error(
    make_publisher, error
):
    def fault(request):
        return error(f"private {TOKEN} {request.url}")

    publisher, calls = make_publisher(fault=fault)
    with pytest.raises(TelegramPublishError) as caught:
        publisher.send(CHAT, MESSAGE)
    assert caught.value.uncertain is True
    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert len(calls) == 1


def test_photo_rejection_does_not_send_second_text_request(make_publisher):
    publisher, calls = make_publisher(httpx.Response(400, json={"ok": False, "error_code": 400}))
    with pytest.raises(TelegramPublishError):
        publisher.send(CHAT, replace(MESSAGE, photo_url="https://http2.mlstatic.com/SYNTHETIC.jpg"))
    assert len(calls) == 1 and calls[0].url.path.endswith("/sendPhoto")


def test_each_send_rechecks_changed_authorization(make_publisher):
    config = settings()
    publisher, calls = make_publisher(config=config)
    publisher.send(CHAT, MESSAGE)
    config.telegram_real_sends_authorized = False
    with pytest.raises(TelegramPublishError, match="não autorizados"):
        publisher.send(CHAT, MESSAGE)
    assert len(calls) == 1


def test_httpx_and_httpcore_logs_redact_mandatory_token_url(caplog, make_publisher):
    def log_request(request):
        logging.getLogger("httpcore.http11").debug("request path %s", request.url.path)

    publisher, calls = make_publisher(handler=log_request)
    with caplog.at_level(logging.DEBUG):
        publisher.send(CHAT, MESSAGE)
    assert len(calls) == 1
    assert TOKEN not in caplog.text
    assert "TELEGRAM_TOKEN_REDACTED" in caplog.text
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
