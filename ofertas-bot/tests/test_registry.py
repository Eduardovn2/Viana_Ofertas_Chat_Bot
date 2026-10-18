"""SYNTHETIC credentials and offline multi-process-equivalent registry checks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings
from app.db import build_engine, build_session_factory
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
