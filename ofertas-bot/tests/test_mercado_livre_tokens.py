"""Synthetic HTTP and temporary, fake-protected files; no real Windows profile."""

import hashlib
import multiprocessing
import os
import sqlite3
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.mercado_livre_tokens import MercadoLivreTokenError, MercadoLivreTokens
from app.services.oauth_store import OAuthStore

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
CLIENT = "SYNTHETIC-CLIENT-ID"
SECRET = "SYNTHETIC-CLIENT-SECRET"
OLD_ACCESS = "SYNTHETIC-OLD-ACCESS"
OLD_REFRESH = "SYNTHETIC-OLD-REFRESH"
NEW_ACCESS = "SYNTHETIC-NEW-ACCESS"
NEW_REFRESH = "SYNTHETIC-NEW-REFRESH"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class TestOnlyPortableProtector:
    """Compression is NOT cryptography; this fake handles synthetic tests only."""

    __test__ = False

    def __init__(self):
        self.fail_protect = False
        self.fail_unprotect = False

    def protect(self, value):
        if self.fail_protect:
            raise RuntimeError(OLD_REFRESH)
        return b"TEST-ONLY:" + zlib.compress(value)

    def unprotect(self, value):
        if self.fail_unprotect:
            raise RuntimeError(OLD_ACCESS)
        if not value.startswith(b"TEST-ONLY:"):
            raise ValueError(OLD_REFRESH)
        return zlib.decompress(value[len(b"TEST-ONLY:") :])


def configuration(secret=SECRET):
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        mercadolivre_oauth_enabled=False,
        mercadolivre_client_id=SecretStr(CLIENT),
        mercadolivre_client_secret=SecretStr(secret),
    )


def stored_payload(*, expires=None, access=OLD_ACCESS, refresh=OLD_REFRESH, user="123456"):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_at": (expires or NOW - timedelta(minutes=1)).isoformat(),
        "scope": "read offline_access",
        "user_id": user,
        "client_id_fingerprint": digest(CLIENT),
    }


def refreshed_payload(**changes):
    return {
        "access_token": NEW_ACCESS,
        "refresh_token": NEW_REFRESH,
        "token_type": "bearer",
        "expires_in": 3600,
        "scope": "read offline_access",
        "user_id": 123456,
        **changes,
    }


@pytest.fixture
def store(tmp_path):
    return OAuthStore(tmp_path / "private" / "synthetic.sqlite3", TestOnlyPortableProtector())


@pytest.fixture
def manager_factory():
    clients = []

    def create(store, handler, *, settings=None, clock=None, **options):
        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        clients.append(client)
        return MercadoLivreTokens(
            settings or configuration(), store, client, clock=clock or (lambda: NOW), **options
        )

    yield create
    for client in clients:
        client.close()


def assert_error(code, function):
    with pytest.raises(MercadoLivreTokenError) as captured:
        function()
    assert captured.value.code == code
    diagnostic = str(captured.value) + repr(captured.value)
    assert all(
        secret not in diagnostic
        for secret in (SECRET, OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH)
    )
    return captured.value


def test_constructor_is_lazy_and_valid_token_works_with_new_consent_disabled(
    store, manager_factory
):
    accesses = []
    lazy = MercadoLivreTokens(configuration(), lambda: accesses.append(True))
    assert accesses == []
    lazy.close()
    store.save_tokens(stored_payload(expires=NOW + timedelta(hours=1)))
    requests = []
    manager = manager_factory(store, lambda request: requests.append(request))
    token = manager.get_access_token()
    assert isinstance(token, SecretStr)
    assert token.get_secret_value() == OLD_ACCESS
    assert OLD_ACCESS not in repr(token)
    assert requests == []


def test_status_and_configured_never_decrypt_or_refresh(store, manager_factory):
    manager = manager_factory(store, lambda request: pytest.fail("status made a request"))
    assert not manager.configured()
    store.save_tokens(stored_payload())
    store._protector.fail_unprotect = True
    assert manager.configured()
    assert manager.status() == {
        "configured": True,
        "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
        "refresh_state": None,
        "retry_at": None,
        "reauthorization_required": False,
    }


def test_expiry_posts_once_after_durable_claim_and_rotates_encrypted_tokens(store, manager_factory):
    store.save_tokens(stored_payload())
    before = store.load_token_snapshot()
    requests = []

    def handler(request):
        # A second connection sees a committed marker before the request is sent.
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("SELECT status FROM oauth_refresh_state").fetchone() == (
                "IN_PROGRESS",
            )
        requests.append(request)
        assert request.url == "https://api.mercadolibre.com/oauth/token"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"],
            "client_id": [CLIENT],
            "client_secret": [SECRET],
            "refresh_token": [OLD_REFRESH],
        }
        return httpx.Response(200, json=refreshed_payload())

    manager = manager_factory(store, handler)
    assert manager.get_access_token().get_secret_value() == NEW_ACCESS
    assert manager.get_access_token().get_secret_value() == NEW_ACCESS
    assert len(requests) == 1
    after = store.load_token_snapshot()
    assert after.version != before.version and after.refresh_state is None
    assert after.payload["refresh_token"] == NEW_REFRESH
    disk = b"".join(file.read_bytes() for file in store.path.parent.iterdir())
    assert all(
        value.encode() not in disk for value in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH)
    )


def test_rejected_current_access_forces_one_refresh_and_stale_rejection_reuses_new_token(
    store, manager_factory
):
    store.save_tokens(stored_payload(expires=NOW + timedelta(hours=1)))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=refreshed_payload())

    manager = manager_factory(store, handler)
    assert manager.refresh_after_rejection(SecretStr(OLD_ACCESS)).get_secret_value() == NEW_ACCESS
    assert manager.refresh_after_rejection(SecretStr(OLD_ACCESS)).get_secret_value() == NEW_ACCESS
    assert len(requests) == 1


def test_parallel_managers_share_os_lock_and_refresh_once(store, manager_factory):
    store.save_tokens(stored_payload())
    requests = []

    def handler(request):
        requests.append(request)
        time.sleep(0.08)
        return httpx.Response(200, json=refreshed_payload())

    managers = [manager_factory(store, handler) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(
            executor.map(lambda manager: manager.get_access_token().get_secret_value(), managers)
        )
    assert tokens == [NEW_ACCESS] * 8
    assert len(requests) == 1


def _process_refresh(path, request_log, start_file, result_file):
    store = OAuthStore(path, TestOnlyPortableProtector())

    def handler(request):
        with request_log.open("ab") as log:
            log.write(b"1\n")
        time.sleep(0.1)
        return httpx.Response(200, json=refreshed_payload())

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        manager = MercadoLivreTokens(configuration(), store, client, clock=lambda: NOW)
        deadline = time.monotonic() + 10
        while not start_file.exists():
            if time.monotonic() >= deadline:
                result_file.write_text("START_TIMEOUT", encoding="ascii")
                return
            time.sleep(0.01)
        try:
            result = digest(manager.get_access_token().get_secret_value())
        except MercadoLivreTokenError as error:
            result = error.code
        result_file.write_text(result, encoding="ascii")


def test_independent_processes_rotate_once(store):
    store.save_tokens(stored_payload())
    context = multiprocessing.get_context("spawn")
    scratch = store.path.parent.parent
    request_log, start_file = scratch / "requests.log", scratch / "start.txt"
    results = [scratch / f"result-{index}.txt" for index in range(3)]
    processes = [
        context.Process(target=_process_refresh, args=(store.path, request_log, start_file, result))
        for result in results
    ]
    try:
        for process in processes:
            process.start()
        start_file.touch()
        for process in processes:
            process.join(20)
        assert all(process.exitcode == 0 for process in processes)
        assert [file.read_text(encoding="ascii") for file in results] == [digest(NEW_ACCESS)] * 3
        assert request_log.read_bytes() == b"1\n"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)


def _crash_after_claim(path):
    store = OAuthStore(path, TestOnlyPortableProtector())
    with store.refresh_lock():
        snapshot = store.load_token_snapshot()
        store.begin_refresh(snapshot.version, digest("crash-attempt"), digest("credentials"), NOW)
        os._exit(0)  # Simulates termination without Python finally/cleanup.


def test_crash_releases_os_lock_but_durable_claim_forbids_reusing_refresh(store, manager_factory):
    store.save_tokens(stored_payload())
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_after_claim, args=(store.path,))
    process.start()
    process.join(15)
    try:
        assert process.exitcode == 0
        assert store.refresh_metadata()["refresh_state"] == "IN_PROGRESS"
        manager = manager_factory(store, lambda request: pytest.fail("crashed refresh was reused"))
        assert_error("OAUTH_REFRESH_UNCERTAIN", manager.get_access_token)
        assert store.refresh_metadata()["refresh_state"] == "UNCERTAIN"
        assert manager.status()["reauthorization_required"]
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "connection",
        "server",
        "redirect",
        "invalid-json",
        "invalid-shape",
        "wrong-user",
        "old-refresh",
    ],
)
def test_ambiguous_outcomes_are_durably_blocked_without_retry(store, manager_factory, failure):
    old = stored_payload()
    store.save_tokens(old)
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(OLD_REFRESH, request=request)
        if failure == "connection":
            raise httpx.ConnectError(SECRET, request=request)
        if failure == "server":
            return httpx.Response(503, text=SECRET)
        if failure == "redirect":
            return httpx.Response(
                302, headers={"Location": "https://example.invalid/" + OLD_REFRESH}
            )
        if failure == "invalid-json":
            return httpx.Response(200, text=OLD_ACCESS)
        if failure == "invalid-shape":
            return httpx.Response(200, json=[OLD_REFRESH])
        if failure == "wrong-user":
            return httpx.Response(200, json=refreshed_payload(user_id=987654))
        return httpx.Response(200, json=refreshed_payload(refresh_token=OLD_REFRESH))

    manager = manager_factory(store, handler)
    assert_error("OAUTH_REFRESH_UNCERTAIN", manager.get_access_token)
    reopened = OAuthStore(store.path, TestOnlyPortableProtector())
    next_manager = manager_factory(reopened, handler)
    assert_error("OAUTH_REFRESH_UNCERTAIN", next_manager.get_access_token)
    assert len(requests) == 1
    assert reopened.load_tokens() == old
    assert reopened.refresh_metadata()["refresh_state"] == "UNCERTAIN"


def test_invalid_grant_is_revoked_and_only_new_consent_clears_block(store, manager_factory):
    store.save_tokens(stored_payload())
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": OLD_REFRESH}
        )

    manager = manager_factory(store, handler)
    assert_error("OAUTH_REAUTHORIZATION_REQUIRED", manager.get_access_token)
    assert_error("OAUTH_REAUTHORIZATION_REQUIRED", manager.get_access_token)
    assert len(requests) == 1
    assert manager.configured()  # Presence retains store priority even after revocation.
    assert manager.status()["refresh_state"] == "REVOKED"
    store.save_tokens(
        stored_payload(expires=NOW + timedelta(hours=1), access=NEW_ACCESS, refresh=NEW_REFRESH)
    )
    assert manager.get_access_token().get_secret_value() == NEW_ACCESS
    assert manager.status()["refresh_state"] is None


def test_invalid_client_blocks_until_secret_is_corrected(store, manager_factory):
    store.save_tokens(stored_payload())
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401, json={"error": "invalid_client", "raw_secret": SECRET})
        return httpx.Response(200, json=refreshed_payload())

    manager = manager_factory(store, handler)
    assert_error("OAUTH_CONFIGURATION", manager.get_access_token)
    assert_error("OAUTH_CONFIGURATION", manager.get_access_token)
    corrected = manager_factory(
        store, handler, settings=configuration("SYNTHETIC-CORRECTED-SECRET")
    )
    assert corrected.get_access_token().get_secret_value() == NEW_ACCESS
    assert len(requests) == 2


@pytest.mark.parametrize("retry_after", ["7200", "Sun, 18 Oct 2026 14:00:00 GMT"])
def test_rate_limit_preserves_long_retry_after_across_instances(
    store, manager_factory, retry_after
):
    store.save_tokens(stored_payload())
    instant = [NOW]
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429, headers={"Retry-After": retry_after}, json={"error": "too_many_requests"}
            )
        return httpx.Response(200, json=refreshed_payload())

    manager = manager_factory(store, handler, clock=lambda: instant[0])
    error = assert_error("OAUTH_RATE_LIMITED", manager.get_access_token)
    assert error.retry_at == NOW + timedelta(seconds=7200)
    instant[0] += timedelta(seconds=4000)
    other = manager_factory(store, handler, clock=lambda: instant[0])
    assert_error("OAUTH_RATE_LIMITED", other.get_access_token)
    assert len(requests) == 1
    instant[0] = NOW + timedelta(seconds=7200)
    assert other.get_access_token().get_secret_value() == NEW_ACCESS
    assert len(requests) == 2


def test_overflowing_retry_after_fails_closed_without_early_retry(store, manager_factory):
    store.save_tokens(stored_payload())
    manager = manager_factory(
        store, lambda request: httpx.Response(429, headers={"Retry-After": "9" * 100})
    )
    error = assert_error("OAUTH_RATE_LIMITED", manager.get_access_token)
    assert error.retry_at.year == 9999
    assert_error("OAUTH_RATE_LIMITED", manager.get_access_token)


def test_new_consent_wins_over_inflight_rotation(store, manager_factory):
    store.save_tokens(stored_payload())
    entered, proceed = Event(), Event()

    def handler(request):
        entered.set()
        assert proceed.wait(5)
        return httpx.Response(200, json=refreshed_payload())

    manager = manager_factory(store, handler)
    replacement = stored_payload(
        expires=NOW + timedelta(hours=2),
        access="SYNTHETIC-NEW-CONSENT-ACCESS",
        refresh="SYNTHETIC-NEW-CONSENT-REFRESH",
        user="987654",
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.get_access_token)
        assert entered.wait(5)
        store.save_tokens(replacement)
        proceed.set()
        assert future.result(timeout=5).get_secret_value() == replacement["access_token"]
    assert store.load_tokens() == replacement


def test_save_failure_after_post_keeps_old_encrypted_payload_and_blocks_refresh(
    store, manager_factory
):
    old = stored_payload()
    store.save_tokens(old)
    requests = []

    def handler(request):
        requests.append(request)
        store._protector.fail_protect = True
        return httpx.Response(200, json=refreshed_payload())

    manager = manager_factory(store, handler)
    assert_error("OAUTH_REFRESH_UNCERTAIN", manager.get_access_token)
    assert store.load_tokens() == old
    assert_error("OAUTH_REFRESH_UNCERTAIN", manager.get_access_token)
    assert len(requests) == 1


def test_wrong_client_and_missing_credentials_never_post(store, manager_factory):
    store.save_tokens({**stored_payload(), "client_id_fingerprint": digest("OTHER-CLIENT")})
    manager = manager_factory(store, lambda request: pytest.fail("mismatched client made request"))
    assert_error("OAUTH_CONFIGURATION", manager.get_access_token)
    missing = configuration().model_copy(update={"mercadolivre_client_secret": None})
    other = manager_factory(
        store, lambda request: pytest.fail("missing configuration made request"), settings=missing
    )
    assert_error("OAUTH_CONFIGURATION", other.get_access_token)
    assert store.refresh_metadata()["refresh_state"] is None


def test_busy_lock_is_typed_and_later_released(store, manager_factory):
    store.save_tokens(stored_payload())
    manager = manager_factory(
        store,
        lambda request: httpx.Response(200, json=refreshed_payload()),
        lock_timeout_seconds=0.01,
    )
    with store.refresh_lock():
        assert_error("OAUTH_REFRESH_BUSY", manager.get_access_token)
    assert manager.get_access_token().get_secret_value() == NEW_ACCESS
