"""Synthetic browser/HTTP contracts only; no real OAuth credentials or external calls."""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import test_api as api_fixtures
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_api import HEADERS
from test_oauth_store import FakeProtector

from app.core.config import Settings
from app.core.logging import OAuthAccessLogFilter
from app.integrations.mercado_livre_oauth import OAuthService
from app.oauth_gateway import create_callback_app
from app.services.oauth_context import CALLBACK_PATH, OAuthContext
from app.services.oauth_store import OAuthStore

application = api_fixtures.application
client = api_fixtures.client
BASE = "https://oauth-callback.example.test"  # Reserved synthetic fixture, not a real tunnel.
REDIRECT = BASE + CALLBACK_PATH
CLIENT_SECRET = "SYNTHETIC_CLIENT_SECRET_NEVER_LIVE"
ACCESS = "SYNTHETIC_ACCESS_NEVER_LIVE"
REFRESH = "SYNTHETIC_REFRESH_NEVER_LIVE"


@pytest.fixture
def flow(tmp_path, application):
    now = [datetime(2026, 10, 18, 20, tzinfo=UTC)]
    settings = Settings(
        _env_file=None,
        mercadolivre_oauth_enabled=True,
        mercadolivre_client_id=SecretStr("123456789"),
        mercadolivre_client_secret=SecretStr(CLIENT_SECRET),
        mercadolivre_redirect_uri=REDIRECT,
        app_env="test",
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "token_type": "bearer",
                "expires_in": 21600,
                "scope": "read offline_access",
                "user_id": 123,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    store = OAuthStore(tmp_path / "oauth.sqlite3", protector=FakeProtector())

    def service_factory(config, storage, clock):
        return OAuthService(config, storage, http_client=http_client, clock=clock)

    context = OAuthContext(
        settings, store=store, service_factory=service_factory, clock=lambda: now[0]
    )
    application.state.mercadolivre_oauth = context
    gateway = create_callback_app(oauth_context=context)
    with TestClient(
        gateway, base_url=BASE, client=("198.51.100.4", 50000), follow_redirects=False
    ) as browser:
        yield context, browser, calls, now
    http_client.close()


def begin(client, browser):
    launched = client.post("/api/v1/oauth/mercadolivre/authorize", headers=HEADERS)
    assert launched.status_code == 200, launched.text
    launch_url = launched.json()["launch_url"]
    response = browser.get(launch_url)
    assert response.status_code == 303
    parameters = parse_qs(urlsplit(response.headers["location"]).query)
    return parameters["state"][0], launch_url, response, parameters


def test_oauth_start_requires_local_admin_and_explicit_enablement(client, application):
    assert client.post("/api/v1/oauth/mercadolivre/authorize").status_code == 401
    result = client.post("/api/v1/oauth/mercadolivre/authorize", headers=HEADERS)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "OAUTH_DISABLED"
    result = client.get("/api/v1/oauth/mercadolivre/status", headers=HEADERS)
    assert result.status_code == 200 and result.json()["oauth_enabled"] is False
    assert result.json()["commercial_integration_activated"] is False


def test_synthetic_oauth_browser_flow_pkce_exact_redirect_and_secret_storage(client, flow):
    context, browser, calls, _ = flow
    state, _, launched, parameters = begin(client, browser)
    assert parameters["redirect_uri"] == [REDIRECT]
    assert parameters["code_challenge_method"] == ["S256"]
    cookie = launched.headers["set-cookie"]
    assert all(
        part in cookie for part in ("Secure", "HttpOnly", "SameSite=lax", "Path=" + CALLBACK_PATH)
    )
    assert launched.headers["Cache-Control"] == "no-store"
    result = browser.get(CALLBACK_PATH, params={"state": state, "code": "SYNTHETIC_ONE_USE_CODE"})
    assert result.status_code == 200 and "OAuth concluído" in result.text
    assert len(calls) == 1 and calls[0].url == "https://api.mercadolibre.com/oauth/token"
    form = parse_qs(calls[0].content.decode())
    assert form["redirect_uri"] == [REDIRECT]
    assert form["client_secret"] == [CLIENT_SECRET] and "code_verifier" in form
    assert context.store.load_tokens()["access_token"] == ACCESS
    for value in (state, "SYNTHETIC_ONE_USE_CODE", ACCESS, REFRESH, CLIENT_SECRET):
        assert value not in result.text
    status = client.get("/api/v1/oauth/mercadolivre/status", headers=HEADERS)
    assert status.json()["tokens_stored"] is True
    assert (
        ACCESS not in status.text
        and REFRESH not in status.text
        and CLIENT_SECRET not in status.text
    )
    assert context.settings.mercado_livre_enabled is False
    assert browser.get(CALLBACK_PATH, params={"state": state, "code": "REPLAY"}).status_code == 400
    assert len(calls) == 1


def test_ticket_is_one_use_and_public_callback_cannot_issue_its_own_state(client, flow):
    _, browser, calls, _ = flow
    _, launch_url, _, _ = begin(client, browser)
    assert browser.get(launch_url).status_code == 400
    assert browser.get(CALLBACK_PATH).status_code == 400
    assert browser.get(CALLBACK_PATH + "?launch=untrusted").status_code == 400
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/docs",
        "/redoc",
        "/openapi.json",
        "/health",
        "/api/v1/oauth/mercadolivre/authorize",
        "/api/v1/products",
    ],
)
def test_public_gateway_exposes_only_callback(flow, path):
    _, browser, calls, _ = flow
    assert browser.get(path).status_code == 404
    assert calls == []


def test_wrong_browser_cannot_complete_or_consume_legitimate_state(client, flow):
    context, browser, calls, _ = flow
    state, _, _, _ = begin(client, browser)
    with TestClient(create_callback_app(oauth_context=context), base_url=BASE) as attacker:
        assert (
            attacker.get(CALLBACK_PATH, params={"state": state, "code": "SYNTHETIC"}).status_code
            == 400
        )
    assert calls == []
    assert (
        browser.get(CALLBACK_PATH, params={"state": state, "code": "SYNTHETIC"}).status_code == 200
    )
    assert len(calls) == 1


def test_invalid_callback_does_not_destroy_valid_binding_cookie(client, flow):
    _, browser, calls, _ = flow
    state, _, _, _ = begin(client, browser)
    invalid = browser.get(CALLBACK_PATH, params={"state": "invalid", "code": "SYNTHETIC"})
    assert invalid.status_code == 400
    assert "Max-Age=0" not in invalid.headers.get("set-cookie", "")
    assert (
        browser.get(CALLBACK_PATH, params={"state": state, "code": "SYNTHETIC"}).status_code == 200
    )
    assert len(calls) == 1


def test_expired_ticket_and_attempt_never_exchange_code(client, flow):
    _, browser, calls, now = flow
    state, _, _, _ = begin(client, browser)
    now[0] += timedelta(minutes=11)
    assert (
        browser.get(CALLBACK_PATH, params={"state": state, "code": "SYNTHETIC"}).status_code == 400
    )
    assert calls == []


def test_denial_is_sanitized_and_never_exchanges_code(client, flow):
    _, browser, calls, _ = flow
    state, _, _, _ = begin(client, browser)
    result = browser.get(
        CALLBACK_PATH,
        params={"state": state, "error": "access_denied", "error_description": CLIENT_SECRET},
    )
    assert result.status_code == 400 and CLIENT_SECRET not in result.text
    assert calls == []


def test_repeated_query_parameters_and_mixed_launch_callback_are_rejected(client, flow):
    _, browser, calls, _ = flow
    state, _, _, _ = begin(client, browser)
    result = browser.get(
        CALLBACK_PATH, params=[("state", state), ("state", state), ("code", "SYNTHETIC")]
    )
    assert result.status_code == 400
    result = browser.get(
        CALLBACK_PATH, params={"state": state, "code": "SYNTHETIC", "launch": "bad"}
    )
    assert result.status_code == 400 and calls == []


def test_wrong_public_host_is_rejected_without_network(flow):
    _, browser, calls, _ = flow
    assert browser.get(CALLBACK_PATH, headers={"Host": "untrusted.example.test"}).status_code == 404
    assert calls == []


@pytest.mark.parametrize(
    "host",
    [
        "127.1",
        "127.0.0.1",
        "oauth..example.test",
        "%65xample.test",
        "[v1.oauth.example]",
        "internal.local",
        "example.localhost",
    ],
)
def test_redirect_rejects_invalid_and_local_hosts(host):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mercadolivre_redirect_uri="https://" + host + CALLBACK_PATH)


def test_oauth_access_log_filter_removes_query_without_changing_other_logs():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", CALLBACK_PATH + "?code=SYNTHETIC_SECRET&state=SECRET", "1.1", 200),
        None,
    )
    OAuthAccessLogFilter().filter(record)
    assert "SECRET" not in record.getMessage()
    assert CALLBACK_PATH in record.getMessage()


def test_test_environment_never_uses_the_real_windows_token_profile(monkeypatch):
    import app.services.oauth_context as module

    monkeypatch.setattr(
        module, "default_store_path", lambda: pytest.fail("real profile must remain untouched")
    )
    context = OAuthContext(Settings(_env_file=None, app_env="test"))
    assert context.status()["tokens_stored"] is False
    from app.services.oauth_store import OAuthStorageError

    with pytest.raises(OAuthStorageError):
        _ = context.store


def test_oauth_log_formatter_records_only_allowlisted_reason_codes():
    from app.core.logging import JsonFormatter

    record = logging.LogRecord(
        "ofertas_bot", logging.WARNING, __file__, 1, "oauth_callback_failed", (), None
    )
    record.oauth_error = CLIENT_SECRET
    assert CLIENT_SECRET not in JsonFormatter().format(record)
    assert "unclassified" in JsonFormatter().format(record)
    record.oauth_error = "invalid_client"
    assert "invalid_client" in JsonFormatter().format(record)
