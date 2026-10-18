"""Synthetic HTTP responses only; never contact a marketplace."""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.integrations.errors import AuthorizationDenied, IntegrationError, InvalidPayload
from app.integrations.http import JsonTransport

URL = "https://api.mercadolibre.com/items/MLB123"


def transport(handler, **kwargs):
    return JsonTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
        min_interval=0,
        jitter=lambda: 0,
        **kwargs,
    )


@pytest.mark.parametrize("status", [401, 403])
def test_auth_refusal_never_retries_or_leaks_body(status):
    calls = []
    client = transport(
        lambda request: calls.append(request) or httpx.Response(status, text="private-secret")
    )
    with pytest.raises(AuthorizationDenied) as error:
        client.request("GET", URL, headers={"Authorization": "Bearer private-secret"})
    assert len(calls) == 1
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_bounded_retries_then_success(status):
    responses = iter(
        [
            httpx.Response(status, headers={"Retry-After": "0"}),
            httpx.Response(200, content=b'{"amount":12.34}'),
        ]
    )
    client = transport(lambda _: next(responses), max_retries=1)
    result = client.request("GET", URL)
    assert result.data["amount"] == Decimal("12.34")


def test_retry_after_longer_than_budget_does_not_retry_early():
    calls = []
    client = transport(
        lambda request: (
            calls.append(request) or httpx.Response(429, headers={"Retry-After": "120"})
        ),
        max_retry_wait=5,
    )
    with pytest.raises(IntegrationError, match="espera"):
        client.request("GET", URL)
    with pytest.raises(IntegrationError, match="espera"):
        client.request("GET", URL)
    assert len(calls) == 1


def test_timeout_is_bounded_and_safe():
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-token", request=request)

    client = transport(fail, max_retries=1)
    with pytest.raises(IntegrationError) as error:
        client.request("GET", URL)
    assert error.value.code == "TIMEOUT"
    assert "private-token" not in str(error.value)
    assert len(calls) == 2


def test_mutation_retry_can_be_disabled():
    calls = []
    client = transport(lambda request: calls.append(request) or httpx.Response(500), max_retries=2)
    with pytest.raises(IntegrationError):
        client.request("POST", URL, body=b"{}", retry=False)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://api.mercadolibre.com/items/x",
        "https://127.0.0.1/x",
        "https://api.mercadolibre.com.evil.test/x",
        "https://user:pass@api.mercadolibre.com/x",
        "https://api.mercadolibre.com:8443/x",
        "https://localhost/x",
    ],
)
def test_ssrf_requests_rejected_without_network(url):
    client = transport(lambda _: pytest.fail("network not allowed"))
    with pytest.raises(IntegrationError) as error:
        client.request("GET", url)
    assert error.value.code == "UNSAFE_REQUEST"


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b"null"])
def test_invalid_schema(payload):
    client = transport(lambda _: httpx.Response(200, content=payload))
    with pytest.raises(InvalidPayload):
        client.request("GET", URL)


def test_redirect_is_not_followed():
    calls = []
    client = transport(
        lambda request: (
            calls.append(request)
            or httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"})
        )
    )
    with pytest.raises(IntegrationError):
        client.request("GET", URL)
    assert len(calls) == 1


def test_cached_observation_keeps_time_and_revalidation_after_ttl():
    calls = []
    now = [0.0]
    clock = [datetime(2026, 10, 18, 12, tzinfo=UTC)]
    client = transport(
        lambda request: calls.append(request) or httpx.Response(200, json={"amount": "10.00"}),
        cache_seconds=30,
        monotonic=lambda: now[0],
        clock=lambda: clock[0],
    )
    first = client.request("GET", URL, cache_key="item")
    now[0] += 10
    clock[0] = datetime(2026, 10, 18, 13, tzinfo=UTC)
    assert client.request("GET", URL, cache_key="item") is first
    now[0] += 21
    second = client.request("GET", URL, cache_key="item")
    assert second.fetched_at > first.fetched_at
    assert len(calls) == 2
