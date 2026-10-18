"""SYNTHETIC OAuth fixtures only: no accounts, real secrets or external HTTP calls."""

import base64
import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.integrations.mercado_livre_oauth import (
    AUTHORIZATION_URL,
    TOKEN_URL,
    OAuthService,
    OAuthTokens,
)
from app.services.oauth_store import OAuthAttempt, OAuthStateError, OAuthStorageError

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
CLIENT_ID = "123456789012345-SYNTHETIC"
CLIENT_SECRET = "SYNTHETIC-CLIENT-SECRET"
AUTH_CODE = "TG-SYNTHETIC-AUTHORIZATION-CODE"
ACCESS_TOKEN = "SYNTHETIC-ACCESS-TOKEN"
REFRESH_TOKEN = "SYNTHETIC-REFRESH-TOKEN"
BINDING = hashlib.sha256(b"SYNTHETIC-ADMIN-BROWSER-COOKIE").hexdigest()
REDIRECT_URI = "https://oauth.example.test/oauth/mercadolivre/callback"


class MemoryStore:
    """Models the store's atomic matching consume; not a production implementation."""

    def __init__(self):
        self.attempts = {}
        self.tokens = []
        self.saved_hashes = []
        self.lock = threading.Lock()
        self.save_error = None
        self.consume_error = None
        self.token_error = None

    def save_attempt(self, state_hash, **values):
        if self.save_error:
            raise self.save_error
        with self.lock:
            self.saved_hashes.append(state_hash)
            self.attempts[state_hash] = OAuthAttempt(**values)

    def consume_attempt(self, state_hash, binding_hash, now):
        if self.consume_error:
            raise self.consume_error
        with self.lock:
            attempt = self.attempts.get(state_hash)
            if attempt is None:
                raise OAuthStateError()
            if attempt.expires_at <= now:
                del self.attempts[state_hash]
                raise OAuthStateError()
            if not hmac.compare_digest(attempt.binding_hash, binding_hash):
                raise OAuthStateError()
            del self.attempts[state_hash]
            return attempt

    def save_tokens(self, payload):
        if self.token_error:
            raise self.token_error
        self.tokens.append(payload)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def payload():
    return {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "token_type": "bearer",
        "expires_in": 21600,
        "scope": "offline_access read write",
        "user_id": 123456789,
        "private_unexpected_data": "SYNTHETIC-IGNORED-PRIVATE-FIELD",
    }


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        mercadolivre_oauth_enabled=True,
        mercadolivre_client_id=SecretStr(CLIENT_ID),
        mercadolivre_client_secret=SecretStr(CLIENT_SECRET),
        mercadolivre_redirect_uri=REDIRECT_URI,
    )


def setup_service(settings, payload, *, handler=None, store=None):
    store = store or MemoryStore()
    clock = Clock()
    requests = []

    def transport(request):
        requests.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(transport))
    return OAuthService(settings, store, client, clock), store, clock, requests, client


def start(service):
    url = service.begin(BINDING)
    return parse_qs(urlsplit(url).query)["state"][0]


def assert_error(code, operation):
    with pytest.raises(IntegrationError) as raised:
        operation()
    assert raised.value.code == code
    diagnostic = f"{raised.value!s} {raised.value!r}"
    for secret in (AUTH_CODE, ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in diagnostic
    assert raised.value.__suppress_context__ or raised.value.__context__ is None
    return raised.value


@pytest.mark.parametrize("pkce", [True, False])
def test_authorization_then_exchange_is_bound_and_uses_exact_official_form(settings, payload, pkce):
    settings.mercadolivre_pkce_enabled = pkce
    service, store, _, requests, client = setup_service(settings, payload)
    url = service.begin(BINDING)
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    state = query["state"][0]
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    attempt = store.attempts[state_hash]
    assert parsed.scheme + "://" + parsed.netloc + parsed.path == AUTHORIZATION_URL
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert CLIENT_SECRET not in url
    assert len(state) == 43 and state not in store.attempts
    assert attempt.binding_hash == BINDING
    assert attempt.expires_at == NOW + timedelta(seconds=600)
    assert attempt.redirect_uri == REDIRECT_URI
    assert attempt.client_id_fingerprint == hashlib.sha256(CLIENT_ID.encode()).hexdigest()
    assert requests == []
    if pkce:
        assert 43 <= len(attempt.verifier) <= 128
        assert query["code_challenge_method"] == ["S256"]
        expected = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest())
        assert query["code_challenge"] == [expected.rstrip(b"=").decode()]
        assert attempt.verifier not in url
    else:
        assert attempt.verifier is None
        assert "code_challenge" not in query and "code_challenge_method" not in query
    assert service.callback(state, AUTH_CODE, binding_hash=BINDING) is None
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == TOKEN_URL
    assert request.url.query == b""
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert "authorization" not in request.headers
    assert request.extensions["timeout"] == {"connect": 5, "read": 5, "write": 5, "pool": 5}
    form = parse_qs(request.content.decode())
    expected = {
        "grant_type": ["authorization_code"],
        "client_id": [CLIENT_ID],
        "client_secret": [CLIENT_SECRET],
        "redirect_uri": [REDIRECT_URI],
        "code": [AUTH_CODE],
    }
    if pkce:
        expected["code_verifier"] = [attempt.verifier]
    assert form == expected
    assert state_hash not in store.attempts
    assert store.tokens == [
        {
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "token_type": "bearer",
            "expires_at": (NOW + timedelta(hours=6)).isoformat(),
            "scope": "offline_access read write",
            "user_id": "123456789",
            "client_id_fingerprint": hashlib.sha256(CLIENT_ID.encode()).hexdigest(),
        }
    ]
    assert settings.mercado_livre_enabled is False and settings.price_provider == "simulated"
    service.close()
    assert not client.is_closed
    client.close()


def test_each_begin_has_independent_unpredictable_state_and_verifier(settings, payload):
    service, store, _, requests, _ = setup_service(settings, payload)
    states = {start(service) for _ in range(20)}
    assert len(states) == len(store.attempts) == 20
    assert len({attempt.verifier for attempt in store.attempts.values()}) == 20
    assert requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("mercadolivre_oauth_enabled", False),
        ("mercadolivre_client_id", None),
        ("mercadolivre_client_secret", None),
        ("mercadolivre_redirect_uri", None),
        ("mercadolivre_client_id", SecretStr("")),
        ("mercadolivre_client_secret", SecretStr("bad\nsecret")),
        ("mercadolivre_redirect_uri", "http://oauth.example.test/oauth/mercadolivre/callback"),
        ("mercadolivre_redirect_uri", REDIRECT_URI + "?secret=query"),
        ("mercadolivre_redirect_uri", REDIRECT_URI + "#fragment"),
        (
            "mercadolivre_redirect_uri",
            "https://user:secret@oauth.example.test/oauth/mercadolivre/callback",
        ),
        (
            "mercadolivre_redirect_uri",
            "https://oauth.example.test:8443/oauth/mercadolivre/callback",
        ),
        ("mercadolivre_redirect_uri", "https://oauth.example.test/arbitrary"),
        ("mercadolivre_redirect_uri", "https://127.0.0.1/oauth/mercadolivre/callback"),
        ("mercadolivre_redirect_uri", "https://localhost.localdomain/oauth/mercadolivre/callback"),
        ("mercadolivre_redirect_uri", "https://oauth.example.test./oauth/mercadolivre/callback"),
        ("mercadolivre_oauth_state_ttl_seconds", 0),
    ],
)
def test_disabled_or_partial_configuration_never_reads_store_or_calls_http(
    settings,
    payload,
    field,
    value,
):
    settings = settings.model_copy(update={field: value})
    service, store, _, requests, _ = setup_service(settings, payload)
    expected = "OAUTH_DISABLED" if field == "mercadolivre_oauth_enabled" else "OAUTH_CONFIGURATION"
    assert_error(expected, lambda: service.begin(BINDING))
    assert_error(expected, lambda: service.callback("a" * 43, AUTH_CODE, binding_hash=BINDING))
    assert store.attempts == {} and store.tokens == [] and requests == []


@pytest.mark.parametrize("binding", [None, "", "a" * 63, "A" * 64, "not-a-hash", 12])
def test_invalid_binding_is_rejected_without_consuming_attempt(settings, payload, binding):
    service, store, _, requests, _ = setup_service(settings, payload)
    state = start(service)
    assert_error("OAUTH_INVALID_STATE", lambda: service.begin(binding))
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash=binding)
    )
    assert len(store.attempts) == 1 and requests == []


def test_wrong_browser_cannot_consume_the_intended_browser_attempt(settings, payload):
    service, store, _, requests, _ = setup_service(settings, payload)
    state = start(service)
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash="0" * 64)
    )
    assert len(store.attempts) == 1 and requests == []
    service.callback(state, AUTH_CODE, binding_hash=BINDING)
    assert len(store.tokens) == len(requests) == 1


@pytest.mark.parametrize("state", [None, "", "a" * 42, "a" * 44, "a" * 42 + "!", "b" * 43])
def test_missing_malformed_or_unknown_state_has_no_http(settings, payload, state):
    service, store, _, requests, _ = setup_service(settings, payload)
    start(service)
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )
    assert store.tokens == [] and requests == []


@pytest.mark.parametrize("seconds", [600, 601])
def test_state_expiration_includes_exact_boundary(settings, payload, seconds):
    service, store, clock, requests, _ = setup_service(settings, payload)
    state = start(service)
    clock.now += timedelta(seconds=seconds)
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )
    assert store.attempts == {} and store.tokens == [] and requests == []


def test_successful_state_is_single_use_even_with_concurrent_callbacks(settings, payload):
    service, store, _, requests, _ = setup_service(settings, payload)
    state = start(service)

    def callback():
        try:
            service.callback(state, AUTH_CODE, binding_hash=BINDING)
            return "OK"
        except IntegrationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: callback(), range(2)))
    assert sorted(results) == ["OAUTH_INVALID_STATE", "OK"]
    assert len(requests) == len(store.tokens) == 1
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )


@pytest.mark.parametrize(
    "code,error,expected",
    [
        (None, None, "OAUTH_INVALID_CODE"),
        ("", None, "OAUTH_INVALID_CODE"),
        ("bad code", None, "OAUTH_INVALID_CODE"),
        ("bad\ncode", None, "OAUTH_INVALID_CODE"),
        (True, None, "OAUTH_INVALID_CODE"),
        ("a" * 4097, None, "OAUTH_INVALID_CODE"),
        (AUTH_CODE, "access_denied-SYNTHETIC-SECRET", "OAUTH_DENIED"),
        (AUTH_CODE, "", "OAUTH_DENIED"),
    ],
)
def test_callback_error_or_bad_code_burns_attempt(settings, payload, code, error, expected):
    service, store, _, requests, _ = setup_service(settings, payload)
    state = start(service)
    assert_error(expected, lambda: service.callback(state, code, error, binding_hash=BINDING))
    assert store.attempts == {} and store.tokens == [] and requests == []
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("mercadolivre_client_id", SecretStr("SYNTHETIC-REPLACEMENT")),
        ("mercadolivre_redirect_uri", "https://new.example.test/oauth/mercadolivre/callback"),
        ("mercadolivre_pkce_enabled", False),
    ],
)
def test_changed_application_config_invalidates_inflight_attempt(settings, payload, field, value):
    service, store, _, requests, _ = setup_service(settings, payload)
    state = start(service)
    setattr(settings, field, value)
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )
    assert store.attempts == {} and requests == []


@pytest.mark.parametrize(
    "alteration",
    [
        {"binding_hash": "0" * 64},
        {"client_id_fingerprint": "0" * 64},
        {"expires_at": NOW},
        {"expires_at": NOW.replace(tzinfo=None)},
        {"verifier": "short"},
    ],
)
def test_corrupted_store_attempt_is_rechecked_before_http(settings, payload, alteration):
    service, store, _, requests, _ = setup_service(settings, payload)
    state = start(service)
    key = next(iter(store.attempts))
    original = store.attempts[key]
    # Bypass the fake's own validation to exercise service defense independently.
    store.consume_attempt = lambda *_: replace(original, **alteration)
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )
    assert store.tokens == [] and requests == []


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 403, 429, 500, 503])
def test_http_failures_never_retry_follow_redirects_or_expose_body(settings, payload, status):
    def handler(request):
        return httpx.Response(
            status,
            headers={"Location": "https://evil.example.test/collect"},
            text=f"{CLIENT_SECRET} {AUTH_CODE} {ACCESS_TOKEN}",
        )

    service, store, _, requests, _ = setup_service(settings, payload, handler=handler)
    state = start(service)
    assert_error(
        "OAUTH_TOKEN_EXCHANGE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )
    assert len(requests) == 1 and store.tokens == [] and store.attempts == {}
    assert_error(
        "OAUTH_INVALID_STATE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )
    assert len(requests) == 1


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout, RuntimeError])
def test_transport_exception_is_safe_and_not_retried(settings, payload, exception):
    def handler(request):
        raise exception(f"{CLIENT_SECRET} {AUTH_CODE} {ACCESS_TOKEN}")

    service, store, _, requests, _ = setup_service(settings, payload, handler=handler)
    state = start(service)
    assert_error(
        "OAUTH_TOKEN_EXCHANGE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )
    assert len(requests) == 1 and store.tokens == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("access_token", None),
        ("access_token", ""),
        ("access_token", 123),
        ("access_token", "secret\nvalue"),
        ("access_token", "secret value"),
        ("refresh_token", None),
        ("refresh_token", ""),
        ("refresh_token", True),
        ("token_type", "basic"),
        ("token_type", None),
        ("token_type", 1),
        ("expires_in", 0),
        ("expires_in", -1),
        ("expires_in", True),
        ("expires_in", "21600"),
        ("expires_in", 1.5),
        ("expires_in", 604801),
        ("expires_in", None),
        ("scope", None),
        ("scope", ["read"]),
        ("scope", ""),
        ("scope", "read\nwrite"),
        ("scope", " read"),
        ("scope", "read  write"),
        ("user_id", None),
        ("user_id", True),
        ("user_id", 0),
        ("user_id", -1),
        ("user_id", "not-an-id"),
        ("user_id", "0012"),
        ("user_id", 1.1),
    ],
)
def test_invalid_token_schema_is_not_persisted(settings, payload, field, value):
    payload[field] = value
    service, store, _, requests, _ = setup_service(settings, payload)
    state = start(service)
    assert_error(
        "OAUTH_INVALID_TOKEN_RESPONSE",
        lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING),
    )
    assert len(requests) == 1 and store.tokens == []
    assert store.attempts == {}


@pytest.mark.parametrize(
    "body",
    [b"not-json", b"[]", b"null", b"1", b"x" * 262145],
    ids=["not-json", "list", "null", "number", "oversized"],
)
def test_invalid_or_oversized_json_is_safe(settings, payload, body):
    service, store, _, _, _ = setup_service(
        settings,
        payload,
        handler=lambda _: httpx.Response(200, content=body),
    )
    state = start(service)
    assert_error(
        "OAUTH_INVALID_TOKEN_RESPONSE",
        lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING),
    )
    assert store.tokens == []


def test_response_receipt_time_and_reduced_scope_are_used(settings, payload):
    payload.update(token_type="Bearer", scope="read", user_id="987654321")

    def response(request):
        clock.now += timedelta(seconds=30)
        return httpx.Response(200, json=payload)

    service, store, clock, _, _ = setup_service(settings, payload, handler=response)
    state = start(service)
    service.callback(state, AUTH_CODE, binding_hash=BINDING)
    assert store.tokens[0]["expires_at"] == (clock.now + timedelta(hours=6)).isoformat()
    assert store.tokens[0]["scope"] == "read" and store.tokens[0]["token_type"] == "bearer"
    assert store.tokens[0]["user_id"] == "987654321"


@pytest.mark.parametrize("operation", ["save", "consume", "tokens"])
@pytest.mark.parametrize("failure", [OAuthStorageError(), RuntimeError(CLIENT_SECRET)])
def test_store_failures_are_safe(settings, payload, operation, failure):
    service, store, _, requests, _ = setup_service(settings, payload)
    if operation == "save":
        store.save_error = failure
        assert_error("OAUTH_STORAGE_ERROR", lambda: start(service))
        assert requests == []
    else:
        state = start(service)
        if operation == "consume":
            store.consume_error = failure
        else:
            store.token_error = failure
        assert_error(
            "OAUTH_STORAGE_ERROR", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
        )
        assert len(requests) == (1 if operation == "tokens" else 0)
    assert store.tokens == []


def test_token_objects_and_attempt_repr_never_reveal_secrets():
    tokens = OAuthTokens(SecretStr(ACCESS_TOKEN), SecretStr(REFRESH_TOKEN), NOW, "read", "1")
    attempt = OAuthAttempt("SYNTHETIC-VERIFIER", BINDING, REDIRECT_URI, "0" * 64, NOW)
    for value in (tokens, attempt):
        diagnostic = f"{value!r} {value!s}"
        assert ACCESS_TOKEN not in diagnostic and REFRESH_TOKEN not in diagnostic
        assert "SYNTHETIC-VERIFIER" not in diagnostic


def test_owned_client_explicitly_disables_environment_and_redirects(settings, monkeypatch):
    options = []
    requests = []
    closed = []

    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)

        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(401, text=ACCESS_TOKEN)

        def close(self):
            closed.append(True)

    monkeypatch.setattr("app.integrations.mercado_livre_oauth.httpx.Client", Client)
    service = OAuthService(settings, MemoryStore(), clock=lambda: NOW)
    state = start(service)
    assert options == []
    assert_error(
        "OAUTH_TOKEN_EXCHANGE", lambda: service.callback(state, AUTH_CODE, binding_hash=BINDING)
    )
    assert options == [{"timeout": 5, "follow_redirects": False, "trust_env": False}]
    assert requests[0][0] == TOKEN_URL and requests[0][1]["follow_redirects"] is False
    service.close()
    service.close()
    assert closed == [True]


def test_logs_contain_no_credentials_authorization_code_or_tokens(settings, payload, caplog):
    service, _, _, _, _ = setup_service(settings, payload)
    with caplog.at_level("DEBUG"):
        service.callback(start(service), AUTH_CODE, binding_hash=BINDING)
    for secret in (CLIENT_SECRET, AUTH_CODE, ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in caplog.text
