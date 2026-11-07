"""SYNTHETIC credentials and offline multi-process-equivalent registry checks."""

import hashlib
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.contracts import ProductRef
from app.integrations.errors import AuthorizationDenied, IntegrationError
from app.integrations.http import JsonTransport
from app.integrations.registry import ConnectorRegistry
from app.models import AppSetting, Base, MonitoringLease, Platform, Product

APP_A = "SYNTHETIC_APP_A"
APP_B = "SYNTHETIC_APP_B"
SECRET_A = "SYNTHETIC_SECRET_A_NOT_LIVE"
TOKEN_A = "SYNTHETIC_ML_TOKEN_A_NOT_LIVE"
SHOPEE = Platform.SHOPEE
ML = Platform.MERCADO_LIVRE
API_URL = "https://open-api.affiliate.shopee.com.br/graphql"


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def __call__(self):
        return datetime(2026, 10, 18, 12, tzinfo=UTC) + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds


@pytest.fixture
def database(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'registry.db').as_posix()}")
    Base.metadata.create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def clock():
    return Clock()


def configured_settings(**updates):
    return Settings(
        _env_file=None,
        **{
            "app_env": "test",
            "price_provider": "simulated",
            "scheduler_enabled": False,
            "mercado_livre_enabled": True,
            "mercado_livre_access_token": SecretStr(TOKEN_A),
            "shopee_enabled": True,
            "shopee_app_id": SecretStr(APP_A),
            "shopee_secret": SecretStr(SECRET_A),
            "integration_min_interval_seconds": 1,
            "integration_max_retries": 0,
            "integration_cache_seconds": 0,
            "integration_max_retry_wait_seconds": 5,
            **updates,
        },
    )


def registry(database, clock, **updates):
    return ConnectorRegistry(configured_settings(**updates), session_factory=database, clock=clock)


def offline_http(source_registry, clock, handler, *, max_retries=0):
    return JsonTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval=1,
        max_retries=max_retries,
        max_retry_wait=5,
        cache_seconds=0,
        clock=clock,
        monotonic=clock.monotonic,
        sleep=clock.advance,
        jitter=lambda: 0,
        rate_gate=lambda: source_registry._rate_gate(SHOPEE),
        rate_defer=lambda delay: source_registry._defer_rate(SHOPEE, delay),
        on_auth_failure=lambda: source_registry.block(SHOPEE),
    )


def test_rate_limit_is_shared_by_independent_registries(database, clock):
    first, second = registry(database, clock), registry(database, clock)
    first._rate_gate(SHOPEE)
    with pytest.raises(IntegrationError) as error:
        second._rate_gate(SHOPEE)
    assert error.value.code == "RATE_LIMITED"
    clock.advance(1)
    second._rate_gate(SHOPEE)


def test_simultaneous_registries_only_claim_one_shared_request_slot(database, clock):
    sources = [registry(database, clock), registry(database, clock)]
    barrier = Barrier(2)

    def claim(source):
        barrier.wait(timeout=5)
        try:
            source._rate_gate(SHOPEE)
        except IntegrationError as error:
            return error.code
        return "CLAIMED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, sources))
    assert sorted(results) == ["CLAIMED", "RATE_LIMITED"]


def test_distinct_accounts_and_platforms_have_independent_rate_slots(database, clock):
    first = registry(database, clock)
    another_account = registry(database, clock, shopee_app_id=SecretStr(APP_B))
    first._rate_gate(SHOPEE)
    another_account._rate_gate(SHOPEE)
    first._rate_gate(ML)


def test_secret_rotation_does_not_bypass_the_same_shopee_app_quota(database, clock):
    first = registry(database, clock)
    rotated = registry(database, clock, shopee_secret=SecretStr("SYNTHETIC_ROTATED_SECRET"))
    first._rate_gate(SHOPEE)
    with pytest.raises(IntegrationError) as error:
        rotated._rate_gate(SHOPEE)
    assert error.value.code == "RATE_LIMITED"


def test_authorization_block_survives_a_new_registry_and_stops_provider_lookup(database, clock):
    first = registry(database, clock)
    first.block(SHOPEE)
    restarted = registry(database, clock)
    assert restarted.is_blocked(SHOPEE) is True
    assert restarted.enabled(SHOPEE) is False
    product = Product(
        platform=SHOPEE,
        external_id="101:202",
        original_url="https://shopee.com.br/product/101/202",
        name="SYNTHETIC",
    )
    with pytest.raises(AuthorizationDenied):
        restarted.provider(product)
    assert restarted._connectors == {}


def test_explicit_unblock_propagates_back_to_an_existing_registry(database, clock):
    first, second = registry(database, clock), registry(database, clock)
    first.block(SHOPEE)
    assert second.is_blocked(SHOPEE)
    second.unblock(SHOPEE)
    assert not first.is_blocked(SHOPEE)
    assert first.enabled(SHOPEE)
    assert second.enabled(SHOPEE)


def test_authorization_block_is_scoped_to_platform_and_account(database, clock):
    first = registry(database, clock)
    first.block(SHOPEE)
    another_account = registry(database, clock, shopee_app_id=SecretStr(APP_B))
    assert not another_account.is_blocked(SHOPEE)
    assert another_account.enabled(SHOPEE)
    assert not first.is_blocked(ML)
    assert first.enabled(ML)


def test_block_and_rate_storage_never_contain_raw_credentials(database, clock):
    source = registry(database, clock)
    source.block(SHOPEE)
    source._rate_gate(SHOPEE)
    with database() as session:
        stored = str([(row.key, row.value) for row in session.scalars(select(AppSetting))])
        stored += str([(row.name, row.owner) for row in session.scalars(select(MonitoringLease))])
    assert APP_A not in stored
    assert SECRET_A not in stored
    assert TOKEN_A not in stored


def test_long_retry_after_blocks_other_process_before_another_network_attempt(database, clock):
    first, second = registry(database, clock), registry(database, clock)
    first_calls, second_calls = [], []
    initial = offline_http(
        first,
        clock,
        lambda request: (
            first_calls.append(request) or httpx.Response(429, headers={"Retry-After": "120"})
        ),
    )
    retrying = offline_http(
        second,
        clock,
        lambda request: (
            second_calls.append(request) or httpx.Response(200, json={"SYNTHETIC": True})
        ),
    )
    try:
        with pytest.raises(IntegrationError) as error:
            initial.request("POST", API_URL, body=b'{"query":"SYNTHETIC"}')
        assert error.value.code == "RATE_LIMITED"
        clock.advance(2)
        with pytest.raises(IntegrationError) as error:
            retrying.request("POST", API_URL, body=b'{"query":"SYNTHETIC"}')
        assert error.value.code == "RATE_LIMITED"
        assert len(first_calls) == 1 and second_calls == []
        clock.advance(118)
        assert retrying.request("POST", API_URL, body=b'{"query":"SYNTHETIC"}').data == {
            "SYNTHETIC": True
        }
        assert len(second_calls) == 1
    finally:
        initial.close()
        retrying.close()


def test_retry_after_does_not_block_a_different_credential(database, clock):
    first = registry(database, clock)
    other = registry(database, clock, shopee_app_id=SecretStr(APP_B))
    first._defer_rate(SHOPEE, 120)
    other._rate_gate(SHOPEE)
    with pytest.raises(IntegrationError):
        first._rate_gate(SHOPEE)


def test_shorter_backoff_cannot_shorten_an_existing_shared_cooldown(database, clock):
    first, second = registry(database, clock), registry(database, clock)
    first._defer_rate(SHOPEE, 120)
    second._defer_rate(SHOPEE, 10)
    clock.advance(11)
    with pytest.raises(IntegrationError) as error:
        second._rate_gate(SHOPEE)
    assert error.value.code == "RATE_LIMITED"
    clock.advance(109)
    second._rate_gate(SHOPEE)


def test_denial_from_http_can_stop_future_requests_in_other_registries(database, clock):
    first = registry(database, clock)
    client = offline_http(first, clock, lambda _: httpx.Response(403), max_retries=2)
    try:
        with pytest.raises(AuthorizationDenied):
            client.request("POST", API_URL, body=b'{"query":"SYNTHETIC"}')
        another = registry(database, clock)
        assert another.is_blocked(SHOPEE)
        assert another.enabled(SHOPEE) is False
    finally:
        client.close()


def test_default_disabled_status_does_not_connect_to_database_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Default disabled must not contact infrastructure")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    settings = configured_settings(
        mercado_livre_enabled=False,
        mercado_livre_access_token=None,
        shopee_enabled=False,
        shopee_app_id=None,
        shopee_secret=None,
    )
    source = ConnectorRegistry(settings, session_factory=forbidden)
    try:
        for platform in (SHOPEE, ML):
            assert not source.configured(platform)
            assert not source.enabled(platform)
            assert source.get(platform).capabilities()
    finally:
        source.close()


class MetadataTokenManager:
    def __init__(self):
        self.present = True
        self.token = SecretStr("SYNTHETIC_OAUTH_OLD")
        self.get_calls = 0
        self.refresh_calls = 0
        self.failure = None

    def configured(self):
        return self.present

    def close(self):
        pass

    def get_access_token(self):
        self.get_calls += 1
        if self.failure:
            raise self.failure
        return self.token

    def refresh_after_rejection(self, rejected_token):
        assert rejected_token.get_secret_value() == self.token.get_secret_value()
        self.refresh_calls += 1
        self.token = SecretStr("SYNTHETIC_OAUTH_NEW")
        return self.token


def oauth_settings(**updates):
    return configured_settings(
        **{
            "mercadolivre_client_id": SecretStr("SYNTHETIC_CLIENT_A"),
            "mercadolivre_client_secret": SecretStr("SYNTHETIC_CLIENT_SECRET"),
            "mercadolivre_oauth_enabled": False,
            **updates,
        }
    )


def test_oauth_status_uses_injected_metadata_not_token_or_network(monkeypatch):
    manager = MetadataTokenManager()
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **kw: pytest.fail("Network prohibited"))
    source = ConnectorRegistry(
        oauth_settings(mercado_livre_enabled=False), mercado_livre_tokens=manager
    )
    try:
        assert source.configured(ML) and not source.enabled(ML)
        assert source.get(ML).capabilities()
        assert source.get(ML).token_provider is manager
        assert manager.get_calls == manager.refresh_calls == 0
    finally:
        source.close()


def test_test_environment_never_probes_real_windows_oauth_store(monkeypatch):
    def forbidden():
        pytest.fail("Test environment touched the real Windows profile")

    monkeypatch.setattr("app.services.oauth_context.default_store_path", forbidden)
    source = ConnectorRegistry(oauth_settings(mercado_livre_access_token=None))
    try:
        assert not source.configured(ML)
        assert not source.enabled(ML)
        assert source.get(ML).token_provider is None
    finally:
        source.close()


def test_oauth_requires_app_credentials_even_when_store_is_present():
    source = ConnectorRegistry(
        oauth_settings(mercadolivre_client_secret=None),
        mercado_livre_tokens=MetadataTokenManager(),
    )
    assert not source.configured(ML)
    assert not source.enabled(ML)


def test_revoked_store_never_falls_back_to_configured_static_token():
    manager = MetadataTokenManager()
    manager.failure = IntegrationError(
        "OAUTH_REAUTHORIZATION_REQUIRED", "Nova autorização necessária."
    )
    source = ConnectorRegistry(oauth_settings(), mercado_livre_tokens=manager)
    connector = source.get(ML)
    connector.transport.client.close()
    connector.transport.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Revoked token must not fall back to a static credential"),
        )
    )
    try:
        with pytest.raises(AuthorizationDenied):
            connector.fetch_product(
                ProductRef(ML, "MLB123", "https://produto.mercadolivre.com.br/MLB-123-synthetic_JM")
            )
        assert source.is_blocked(ML)
        assert manager.get_calls == 1 and manager.refresh_calls == 0
    finally:
        source.close()


def test_oauth_token_rotation_keeps_shared_quota_and_authorization_block(database, clock):
    first = ConnectorRegistry(
        oauth_settings(),
        session_factory=database,
        clock=clock,
        mercado_livre_tokens=MetadataTokenManager(),
    )
    second = ConnectorRegistry(
        oauth_settings(
            mercado_livre_access_token=SecretStr("SYNTHETIC_ROTATED_LEGACY"),
            mercadolivre_client_secret=SecretStr("SYNTHETIC_ROTATED_CLIENT_SECRET"),
        ),
        session_factory=database,
        clock=clock,
        mercado_livre_tokens=MetadataTokenManager(),
    )
    assert first.credential_scope(ML) == second.credential_scope(ML)
    first._rate_gate(ML)
    with pytest.raises(IntegrationError) as raised:
        second._rate_gate(ML)
    assert raised.value.code == "RATE_LIMITED"
    first.block(ML)
    assert second.is_blocked(ML)
    assert not second.enabled(ML)


def test_recovered_401_does_not_create_a_durable_block(database, clock):
    manager = MetadataTokenManager()
    source = ConnectorRegistry(
        oauth_settings(), session_factory=database, clock=clock, mercado_livre_tokens=manager
    )
    connector = source.get(ML)
    calls = []

    def response(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "id": "MLB123",
                "site_id": "MLB",
                "title": "SYNTHETIC metadata",
                "status": "closed",
                "available_quantity": 0,
                "variations": [],
            },
        )

    connector.transport.client.close()
    connector.transport.client = httpx.Client(transport=httpx.MockTransport(response))
    connector.transport.clock = clock
    connector.transport.monotonic = clock.monotonic
    connector.transport.sleep = clock.advance
    try:
        snapshot = connector.fetch_product(
            ProductRef(ML, "MLB123", "https://produto.mercadolivre.com.br/MLB-123-synthetic_JM")
        )
        assert snapshot.name == "SYNTHETIC metadata"
        assert len(calls) == 2 and manager.refresh_calls == 1
        assert clock.elapsed >= 1
        assert not source.is_blocked(ML)
        with database() as session:
            assert session.scalar(select(AppSetting.id)) is None
            assert session.scalar(select(MonitoringLease.name)).endswith(
                source.credential_scope(ML)
            )
    finally:
        source.close()


def test_oauth_store_selection_remains_sticky_after_store_disappears(monkeypatch):
    manager = MetadataTokenManager()
    present = [True]
    monkeypatch.setattr(
        "app.services.mercado_livre_tokens.MercadoLivreTokens", lambda *a, **kw: manager
    )
    source = ConnectorRegistry(oauth_settings(), mercado_livre_store_present=lambda: present[0])
    try:
        assert source.configured(ML)
        present[0] = manager.present = False
        assert not source.configured(ML)
        assert source.get(ML).token_provider is manager
    finally:
        source.close()


def test_oauth_grant_appearing_after_materialized_static_connector_takes_priority(monkeypatch):
    manager = MetadataTokenManager()
    present = [False]
    monkeypatch.setattr(
        "app.services.mercado_livre_tokens.MercadoLivreTokens", lambda *a, **kw: manager
    )
    source = ConnectorRegistry(oauth_settings(), mercado_livre_store_present=lambda: present[0])
    try:
        legacy = source.get(ML)
        assert legacy.token_provider is None
        present[0] = True
        oauth = source.get(ML)
        assert oauth is not legacy and oauth.token_provider is manager
        assert oauth.transport is not legacy.transport
        assert source.get(ML) is oauth
        assert manager.get_calls == manager.refresh_calls == 0
    finally:
        source.close()


@pytest.mark.parametrize("expired", [False, True])
def test_real_token_manager_temp_store_registry_and_transport_rotate_once(
    database,
    clock,
    tmp_path,
    expired,
):
    from app.services.mercado_livre_tokens import MercadoLivreTokens
    from app.services.oauth_store import OAuthStore

    class SyntheticProtector:
        """Compression is a fixture, never real protection or a Windows-profile access."""

        def protect(self, value):
            return b"SYNTHETIC:" + zlib.compress(value)

        def unprotect(self, value):
            return zlib.decompress(value[len(b"SYNTHETIC:") :])

    settings = oauth_settings()
    store = OAuthStore(tmp_path / "synthetic-private" / "oauth.db", SyntheticProtector())
    store.save_tokens(
        {
            "access_token": "SYNTHETIC_MANAGER_OLD",
            "refresh_token": "SYNTHETIC_REFRESH_OLD",
            "token_type": "bearer",
            "expires_at": (clock() + timedelta(seconds=-60 if expired else 3600)).isoformat(),
            "scope": "read offline_access",
            "user_id": "123456",
            "client_id_fingerprint": hashlib.sha256(
                settings.mercadolivre_client_id.get_secret_value().encode()
            ).hexdigest(),
        }
    )
    refresh_requests = []

    def refresh(request):
        refresh_requests.append(request)
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["SYNTHETIC_REFRESH_OLD"]
        return httpx.Response(
            200,
            json={
                "access_token": "SYNTHETIC_MANAGER_NEW",
                "refresh_token": "SYNTHETIC_REFRESH_NEW",
                "token_type": "bearer",
                "expires_in": 3600,
                "scope": "read offline_access",
                "user_id": 123456,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(refresh), trust_env=False) as oauth_http:
        manager = MercadoLivreTokens(settings, store, oauth_http, clock=clock)
        source = ConnectorRegistry(
            settings, session_factory=database, clock=clock, mercado_livre_tokens=manager
        )
        scope = source.credential_scope(ML)
        assert source.configured(ML)
        assert refresh_requests == []
        connector = source.get(ML)
        connector.transport.client.close()
        item_requests = []

        def item(request):
            item_requests.append(request)
            if request.headers["authorization"] == "Bearer SYNTHETIC_MANAGER_OLD":
                return httpx.Response(401)
            assert request.headers["authorization"] == "Bearer SYNTHETIC_MANAGER_NEW"
            return httpx.Response(
                200,
                json={
                    "id": "MLB123",
                    "site_id": "MLB",
                    "title": "SYNTHETIC account item",
                    "status": "closed",
                    "available_quantity": 0,
                    "variations": [],
                },
            )

        connector.transport.client = httpx.Client(transport=httpx.MockTransport(item))
        connector.transport.monotonic = clock.monotonic
        connector.transport.sleep = clock.advance
        try:
            result = connector.fetch_product(
                ProductRef(
                    ML,
                    "MLB123",
                    "https://produto.mercadolivre.com.br/MLB-123-synthetic_JM",
                )
            )
            assert result.name == "SYNTHETIC account item"
            assert len(item_requests) == (1 if expired else 2)
            assert len(refresh_requests) == 1
            assert source.credential_scope(ML) == scope and not source.is_blocked(ML)
            assert store.load_token_snapshot().payload["refresh_token"] == "SYNTHETIC_REFRESH_NEW"
            assert not source.settings.mercadolivre_oauth_enabled
        finally:
            source.close()
            manager.close()
