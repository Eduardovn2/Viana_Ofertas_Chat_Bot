"""SYNTHETIC offline contracts, constructed from the official public schema.

These fixtures are not captured Shopee responses and assert no account access.
Every HTTP request uses a fake transport or httpx.MockTransport.
"""

import copy
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.integrations.contracts import CapabilityStatus, ProductRef
from app.integrations.errors import (
    AuthorizationDenied,
    ConnectorDisabled,
    IntegrationError,
    InvalidPayload,
    UnsupportedCapability,
)
from app.integrations.http import JsonResponse, JsonTransport
from app.integrations.shopee import ENDPOINT, ShopeeConnector
from app.models import Availability, Platform, PriceAccess, SourceType

NOW = datetime(2026, 10, 18, 12, 0, 0, 100000, tzinfo=UTC)
APP_ID = "SYNTHETIC_APP"
SECRET = "synthetic-secret-not-a-live-credential"
URL = "https://shopee.com.br/product/101/202"
REF = ProductRef(Platform.SHOPEE, "101:202", URL)
SYNTHETIC_NODE = {
    "itemId": 202,
    "shopId": 101,
    "productName": "SYNTHETIC - produto de teste sem consulta comercial",
    "productLink": URL,
    "imageUrl": "https://down-br.img.susercontent.com/file/synthetic-image",
    "price": "99.90",
    "priceMin": "99.90",
    "priceMax": "99.90",
    "productCatIds": [1, 2, 3],
    "commissionRate": "0.1234",
    "commission": "12.32766",
}


def payload(node=None, *, page=1, has_next=False):
    return {
        "data": {
            "productOfferV2": {
                "nodes": [copy.deepcopy(SYNTHETIC_NODE if node is None else node)],
                "pageInfo": {"page": page, "limit": 20, "hasNextPage": has_next},
            }
        }
    }


class FakeTransport:
    def __init__(self, data=None, *, fetched_at=NOW):
        self.data = payload() if data is None else data
        self.fetched_at = fetched_at
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return JsonResponse(copy.deepcopy(self.data), self.fetched_at)


def connector(transport=None, **kwargs):
    return ShopeeConnector(
        FakeTransport() if transport is None else transport,
        app_id=SecretStr(APP_ID),
        secret=SecretStr(SECRET),
        enabled=True,
        clock=lambda: NOW,
        **kwargs,
    )


@pytest.mark.parametrize(
    "enabled,app_id,secret",
    [
        (False, APP_ID, SECRET),
        (True, None, SECRET),
        (True, APP_ID, None),
        (True, "", ""),
    ],
)
def test_disabled_or_unconfigured_connector_never_calls_transport(enabled, app_id, secret):
    transport = FakeTransport()
    source = ShopeeConnector(
        transport,
        app_id=SecretStr(app_id) if app_id is not None else None,
        secret=SecretStr(secret) if secret is not None else None,
        enabled=enabled,
        short_links_enabled=True,
    )
    with pytest.raises(ConnectorDisabled):
        source.fetch_product(REF)
    with pytest.raises(ConnectorDisabled):
        source.discover_offers("SYNTHETIC")
    with pytest.raises(ConnectorDisabled):
        source.generate_affiliate_link(URL)
    assert transport.calls == []


def test_signature_matches_known_vector_over_the_exact_utf8_payload():
    transport = FakeTransport()
    connector(transport).fetch_product(REF)
    request = transport.calls[0]
    expected = (
        b'{"query":"query { productOfferV2(shopId:101,itemId:202,page:1,limit:1) '
        b"{ nodes { itemId shopId productName productLink imageUrl price priceMin priceMax "
        b'productCatIds commissionRate commission } pageInfo { page limit hasNextPage } } }"}'
    )
    assert request["method"] == "POST" and request["url"] == ENDPOINT
    assert request["body"] == expected
    assert request["headers"]["Authorization"] == (
        "SHA256 Credential=SYNTHETIC_APP, Timestamp=1792324801, "
        "Signature=08b462e76ecd6f10620c9613cc9753cb420691a9227d8097c86dc817021ca678"
    )
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["retry"] is True
    assert APP_ID not in request["cache_key"] and SECRET not in request["cache_key"]


def test_snapshot_preserves_decimal_provenance_and_unknown_commercial_conditions():
    source = connector()
    before = {cap.name: cap.status for cap in source.capabilities()}
    assert before["fetch_product"] == CapabilityStatus.REQUIRES_CREDENTIALS
    snapshot = source.fetch_product(REF)
    assert snapshot.current_price == Decimal("99.90")
    assert snapshot.previous_price is None
    assert snapshot.availability == Availability.UNKNOWN
    assert snapshot.price_access == PriceAccess.UNKNOWN
    assert snapshot.currency == "BRL"
    assert snapshot.external_item_id == "101:202" and snapshot.canonical_url == URL
    assert snapshot.source == "shopee:official_api"
    assert snapshot.source_type == SourceType.OFFICIAL_API
    assert snapshot.source_reference == "productOfferV2:101:202"
    assert snapshot.is_simulated is False and snapshot.observed_at == NOW
    assert snapshot.category_external_id == "3" and snapshot.category_external_name is None
    assert snapshot.image_url == SYNTHETIC_NODE["imageUrl"]
    assert snapshot.seller_id is None
    assert snapshot.evidence_metadata == {
        "category_external_ids": ["1", "2", "3"],
        "commission_rate": "0.1234",
        "commission_amount": "12.32766",
    }
    assert len(snapshot.raw_fingerprint) == 64
    after = {cap.name: cap.status for cap in source.capabilities()}
    assert after["fetch_product"] == CapabilityStatus.SUPPORTED
    assert after["fetch_coupons"] == CapabilityStatus.UNVERIFIED
    assert after["discover_offers"] == CapabilityStatus.REQUIRES_CREDENTIALS


@pytest.mark.parametrize(
    "url",
    [
        "https://shopee.com.br/Produto-SYNTHETIC-i.101.202",
        "https://www.shopee.com.br/product/101/202/",
    ],
)
def test_supported_product_url_formats(url):
    snapshot = connector().fetch_product(ProductRef(Platform.SHOPEE, "101:202", url))
    assert snapshot.canonical_url == url


@pytest.mark.parametrize(
    "ref",
    [
        ProductRef(Platform.MERCADO_LIVRE, "101:202", URL),
        ProductRef(Platform.SHOPEE, "202", URL),
        ProductRef(Platform.SHOPEE, "101:203", URL),
        ProductRef(Platform.SHOPEE, "101:202", "http://shopee.com.br/product/101/202"),
        ProductRef(Platform.SHOPEE, "101:202", "https://127.0.0.1/product/101/202"),
        ProductRef(Platform.SHOPEE, "101:202", "https://shopee.com.br.evil.test/product/101/202"),
        ProductRef(
            Platform.SHOPEE, "101:202", "https://user:password@shopee.com.br/product/101/202"
        ),
        ProductRef(Platform.SHOPEE, "101:202", URL + "#context"),
        ProductRef(Platform.SHOPEE, "101:202", "https://shopee.com.br:8443/product/101/202"),
        ProductRef(Platform.SHOPEE, "101:202", URL, variant_id="invalid\nvariant"),
    ],
)
def test_references_and_unsafe_urls_fail_before_network(ref):
    transport = FakeTransport()
    with pytest.raises(InvalidPayload):
        connector(transport).fetch_product(ref)
    assert transport.calls == []


def test_unresolved_variant_and_url_context_do_not_gain_a_price():
    variant = ProductRef(Platform.SHOPEE, "101:202", URL, variant_id="SYNTHETIC-VARIANT")
    assert connector().fetch_product(variant).current_price is None
    context = ProductRef(Platform.SHOPEE, "101:202", URL + "?variation=SYNTHETIC")
    snapshot = connector().fetch_product(context)
    assert snapshot.current_price is None and snapshot.canonical_url == context.canonical_url


@pytest.mark.parametrize(
    "changes",
    [
        {"priceMin": "79.90", "priceMax": "129.90"},
        {"price": None},
        {"priceMin": None},
        {"priceMax": None},
    ],
)
def test_range_or_missing_price_is_unknown_and_never_reconstructed(changes):
    node = {**SYNTHETIC_NODE, **changes, "priceDiscountRate": 37, "originalPrice": "999.90"}
    snapshot = connector(FakeTransport(payload(node))).fetch_product(REF)
    assert snapshot.current_price is None
    assert snapshot.previous_price is None


@pytest.mark.parametrize(
    "price", ["0", "-1.00", "NaN", "Infinity", "1.001", "abc", 99.9, True, "10000000000.00"]
)
def test_invalid_prices_are_rejected(price):
    node = {**SYNTHETIC_NODE, "price": price}
    with pytest.raises(InvalidPayload):
        connector(FakeTransport(payload(node))).fetch_product(REF)


@pytest.mark.parametrize(
    "changes",
    [
        {"priceMin": "101.00", "priceMax": "99.90"},
        {"priceMin": "80.00", "priceMax": "80.00"},
        {"shopId": 102},
        {"itemId": True},
        {"productName": ""},
        {"productLink": "https://shopee.com.br/product/101/303"},
        {"productCatIds": {"id": 1}},
        {"productCatIds": [2147483648]},
    ],
)
def test_payload_identity_range_and_schema_mismatches_are_rejected(changes):
    with pytest.raises(InvalidPayload):
        connector(FakeTransport(payload({**SYNTHETIC_NODE, **changes}))).fetch_product(REF)


def test_only_safe_selected_evidence_is_kept_and_unknown_images_are_omitted():
    node = {
        **SYNTHETIC_NODE,
        "imageUrl": "https://127.0.0.1/private",
        "commissionRate": "NaN",
        "commission": "invalid",
        "secret": SECRET,
        "Authorization": APP_ID,
        "debug": {"password": SECRET},
    }
    snapshot = connector(FakeTransport(payload(node))).fetch_product(REF)
    assert snapshot.image_url is None
    assert snapshot.evidence_metadata == {"category_external_ids": ["1", "2", "3"]}
    serialized = str(snapshot)
    assert SECRET not in serialized and APP_ID not in serialized
    clean = connector().fetch_product(REF)
    assert snapshot.raw_fingerprint == clean.raw_fingerprint


def test_product_not_found_is_explicit_instead_of_an_empty_success():
    data = payload()
    data["data"]["productOfferV2"]["nodes"] = []
    with pytest.raises(IntegrationError) as error:
        connector(FakeTransport(data)).fetch_product(REF)
    assert error.value.code == "PRODUCT_NOT_FOUND"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"data": None},
        {"data": {"productOfferV2": None}},
        {"data": {"productOfferV2": {"nodes": {}, "pageInfo": {}}}},
    ],
)
def test_unexpected_graphql_shapes_are_rejected(data):
    with pytest.raises(InvalidPayload):
        connector(FakeTransport(data)).fetch_product(REF)


def test_upstream_graphql_errors_do_not_leak_credentials_or_claim_success(caplog):
    data = {"errors": [{"message": f"SYNTHETIC {APP_ID} {SECRET}"}], "data": payload()["data"]}
    source = connector(FakeTransport(data))
    with pytest.raises(IntegrationError) as error:
        source.fetch_product(REF)
    assert error.value.code == "UPSTREAM_GRAPHQL_ERROR"
    assert SECRET not in str(error.value) and APP_ID not in str(error.value)
    assert SECRET not in caplog.text and APP_ID not in caplog.text
    assert source.capabilities()[0].status == CapabilityStatus.REQUIRES_CREDENTIALS


def test_discovery_uses_documented_pagination_filter_and_safe_graphql_string():
    transport = FakeTransport(payload(page=2, has_next=True))
    source = connector(transport)
    search = 'SYNTHETIC caf\u00e9 ") { __schema }'
    result = source.discover_offers(search, page=2, limit=10, category="3")
    assert result.page == 2 and result.has_next is True and len(result.items) == 1
    request = transport.calls[0]
    query = json.loads(request["body"])["query"]
    assert "productOfferV2(" in query and "shopeeOfferV2" not in query
    assert "page:2,limit:10,productCatId:3" in query
    assert f"keyword:{json.dumps(search, ensure_ascii=False)}" in query
    expected = hashlib.sha256(
        APP_ID.encode() + b"1792324801" + request["body"] + SECRET.encode()
    ).hexdigest()
    assert request["headers"]["Authorization"].endswith(expected)
    assert {cap.name: cap.status for cap in source.capabilities()}[
        "discover_offers"
    ] == CapabilityStatus.SUPPORTED


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page": 0},
        {"page": True},
        {"limit": 101},
        {"limit": 0},
        {"category": "casa-decoracao"},
        {"category": "2147483648"},
    ],
)
def test_invalid_discovery_input_never_calls_network(kwargs):
    transport = FakeTransport()
    with pytest.raises(InvalidPayload):
        connector(transport).discover_offers("SYNTHETIC", **kwargs)
    assert transport.calls == []


def test_discovery_does_not_accept_wrong_page_boolean_shape_or_duplicate_items():
    for page, has_next in [(2, False), (1, "false")]:
        with pytest.raises(InvalidPayload):
            connector(FakeTransport(payload(page=page, has_next=has_next))).discover_offers(
                "SYNTHETIC"
            )
    data = payload()
    data["data"]["productOfferV2"]["nodes"] *= 2
    with pytest.raises(InvalidPayload):
        connector(FakeTransport(data)).discover_offers("SYNTHETIC")


def test_coupons_remain_unsupported_without_any_network_call():
    transport = FakeTransport()
    with pytest.raises(UnsupportedCapability):
        connector(transport).fetch_coupons(REF)
    assert transport.calls == []


def test_short_link_requires_an_independent_explicit_flag():
    transport = FakeTransport()
    with pytest.raises(ConnectorDisabled):
        connector(transport).generate_affiliate_link(URL)
    assert transport.calls == []


def test_authenticated_short_link_uses_only_the_official_mutation_without_cache():
    transport = FakeTransport(
        {
            "data": {
                "generateShortLink": {
                    "shortLink": "https://s.shopee.com.br/SYNTHETIC-LINK",
                    "longLink": "https://shopee.com.br/product/101/202?synthetic-tracking=1",
                }
            }
        }
    )
    source = connector(transport, short_links_enabled=True)
    link = source.generate_affiliate_link(URL)
    assert link.url == "https://s.shopee.com.br/SYNTHETIC-LINK"
    assert link.source == "SHOPEE_AFFILIATE_API" and link.account_verified is True
    request = transport.calls[0]
    query = json.loads(request["body"])["query"]
    assert query.startswith("mutation { generateShortLink(")
    assert 'originUrl:"https://shopee.com.br/product/101/202"' in query
    assert request["cache_key"] is None
    assert request["retry"] is False


@pytest.mark.parametrize(
    "url",
    [
        "https://s.shopee.com.br/someone-elses-link",
        "https://shope.ee/someone-elses-link",
        URL + "?affiliate_id=SYNTHETIC-OTHER",
        "https://evil.test/product/101/202",
    ],
)
def test_existing_affiliate_or_tracking_links_are_not_accepted_as_conversion_input(url):
    transport = FakeTransport()
    with pytest.raises(InvalidPayload):
        connector(transport, short_links_enabled=True).generate_affiliate_link(url)
    assert transport.calls == []


@pytest.mark.parametrize(
    "link_data",
    [
        {"shortLink": "https://evil.test/SYNTHETIC", "longLink": URL},
        {"shortLink": URL, "longLink": URL},
        {"shortLink": "https://s.shopee.com.br/SYNTHETIC?token=secret", "longLink": URL},
        {"shortLink": "https://s.shopee.com.br/SYNTHETIC", "longLink": ""},
        {"shortLink": None, "longLink": URL},
        {"shortLink": "https://s.shopee.com.br", "longLink": URL},
        {"shortLink": "https://shope.ee/", "longLink": URL},
        {"shortLink": "https://s.shopee.com.br/a/b", "longLink": URL},
        {
            "shortLink": "https://s.shopee.com.br/SYNTHETIC",
            "longLink": "https://shopee.com.br/product/999/888",
        },
        {
            "shortLink": "https://s.shopee.com.br/SYNTHETIC",
            "longLink": "https://evil.test/product/101/202",
        },
        {
            "shortLink": "https://s.shopee.com.br/SYNTHETIC",
            "longLink": "http://shopee.com.br/product/101/202",
        },
    ],
)
def test_invalid_short_link_results_never_gain_verified_ownership(link_data):
    transport = FakeTransport({"data": {"generateShortLink": link_data}})
    with pytest.raises(InvalidPayload):
        connector(transport, short_links_enabled=True).generate_affiliate_link(URL)


@pytest.mark.parametrize(
    "long_url",
    [
        "https://shopee.com.br/universal-link/product/101/202?synthetic-tracking=1",
        "https://shopee.com.br/unknown-deep-link",
        URL + "?variation_id=SYNTHETIC-VARIANT",
        URL + "#context",
    ],
)
def test_ambiguous_long_link_keeps_account_product_association_unverified(long_url):
    transport = FakeTransport(
        {
            "data": {
                "generateShortLink": {
                    "shortLink": "https://s.shopee.com.br/SYNTHETIC",
                    "longLink": long_url,
                }
            }
        }
    )
    source = connector(transport, short_links_enabled=True)
    result = source.generate_affiliate_link(URL)
    assert result.url == "https://s.shopee.com.br/SYNTHETIC"
    assert result.account_verified is False
    assert {cap.name: cap.status for cap in source.capabilities()}[
        "generate_affiliate_link"
    ] is CapabilityStatus.REQUIRES_CREDENTIALS
    assert len(transport.calls) == 1  # No redirect or speculative resolution request.


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, AuthorizationDenied),
        (403, AuthorizationDenied),
        (429, IntegrationError),
        (503, IntegrationError),
    ],
)
def test_http_failures_are_typed_and_credentials_are_not_exposed(status, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"SYNTHETIC-error": f"{APP_ID}:{SECRET}"})

    transport = JsonTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
        min_interval=0,
        cache_seconds=0,
    )
    try:
        with pytest.raises(expected) as error:
            connector(transport).fetch_product(REF)
        assert len(calls) == 1
        assert APP_ID not in str(error.value) and SECRET not in str(error.value)
    finally:
        transport.close()


def test_timeout_is_safe_and_does_not_fabricate_a_snapshot():
    def handler(request):
        raise httpx.ReadTimeout("SYNTHETIC timeout with private context", request=request)

    transport = JsonTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
        min_interval=0,
        cache_seconds=0,
    )
    try:
        with pytest.raises(IntegrationError) as error:
            connector(transport).fetch_product(REF)
        assert error.value.code == "TIMEOUT"
    finally:
        transport.close()


def test_credential_instances_have_distinct_cache_namespaces():
    transport = FakeTransport()
    connector(transport).fetch_product(REF)
    connector(transport).fetch_product(REF)
    assert transport.calls[0]["cache_key"] != transport.calls[1]["cache_key"]


def test_invalid_app_id_cannot_inject_a_header_or_expose_its_value():
    transport = FakeTransport()
    source = ShopeeConnector(
        transport,
        app_id=SecretStr("SYNTHETIC\r\nInjected: hidden"),
        secret=SecretStr(SECRET),
        enabled=True,
    )
    with pytest.raises(ConnectorDisabled) as error:
        source.fetch_product(REF)
    assert "Injected" not in str(error.value)
    assert transport.calls == []


@pytest.mark.parametrize("observed", [NOW.replace(tzinfo=None), "SYNTHETIC-invalid-time"])
def test_bad_observation_times_fail_with_a_safe_typed_error(observed):
    with pytest.raises(InvalidPayload):
        connector(FakeTransport(fetched_at=observed)).fetch_product(REF)
