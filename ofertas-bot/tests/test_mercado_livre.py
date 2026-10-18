"""Synthetic contract fixtures only: no captured prices, credentials or live API calls."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
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
from app.integrations.mercado_livre import MercadoLivreConnector
from app.models import Availability, Platform, PriceAccess, SourceType

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
TOKEN = "test-only-token-12345"
ITEM_ID = "MLB3810190843"
URL = "https://produto.mercadolivre.com.br/MLB-3810190843-produto-sintetico_JM"


class SyntheticTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("O conector fez requisição adicional inesperada")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def payloads():
    return [
        {
            "id": ITEM_ID,
            "site_id": "MLB",
            "title": "Produto sintético de contrato",
            "permalink": URL,
            "status": "active",
            "available_quantity": 1,
            "currency_id": "BRL",
            "category_id": "MLB12345",
            "seller_id": 12345,
            "variations": [],
            "pictures": [{"secure_url": "https://http2.mlstatic.com/synthetic.jpg"}],
            # Deliberately incompatible legacy values: none is a valid price source.
            "price": 1,
            "base_price": 2,
            "original_price": 99999,
        },
        {
            "price_id": "145",
            "amount": Decimal("80.00"),
            "regular_amount": Decimal("100.00"),
            "currency_id": "BRL",
            "reference_date": "2025-01-01T00:00:00Z",
            "metadata": {"promotion_id": "PRIVATE-SYNTHETIC", "authorization": TOKEN},
        },
        {
            "id": ITEM_ID,
            "prices": [
                {
                    "id": "145",
                    "type": "promotion",
                    "amount": Decimal("80.00"),
                    # The docs say regular_amount may differ between the resources.
                    "regular_amount": Decimal("120.00"),
                    "currency_id": "BRL",
                    "conditions": {
                        "context_restrictions": ["channel_marketplace"],
                        "start_time": (NOW - timedelta(hours=1)).isoformat(),
                        "end_time": (NOW + timedelta(hours=1)).isoformat(),
                    },
                }
            ],
        },
    ]


def connector(payloads, *, dates=None):
    dates = dates or [NOW] * len(payloads)
    transport = SyntheticTransport(
        [
            JsonResponse(deepcopy(data), fetched_at)
            for data, fetched_at in zip(payloads, dates, strict=True)
        ]
    )
    return MercadoLivreConnector(
        transport, token=SecretStr(TOKEN), enabled=True, clock=lambda: NOW
    ), transport


def ref(**changes):
    return ProductRef(
        **{
            "platform": Platform.MERCADO_LIVRE,
            "external_item_id": ITEM_ID,
            "canonical_url": URL,
            **changes,
        }
    )


def test_public_price_comes_from_sale_price_and_proven_conditions_not_legacy_item(payloads):
    source, transport = connector(payloads)
    result = source.fetch_product(ref())
    assert result.current_price == Decimal("80.00")
    assert result.previous_price == Decimal("100.00")
    assert result.price_access is PriceAccess.PUBLIC
    assert result.availability is Availability.AVAILABLE
    assert result.currency == "BRL"
    assert result.source == "mercado_livre:official_api"
    assert result.source_type is SourceType.OFFICIAL_API
    assert result.is_simulated is False
    assert result.observed_at == NOW
    assert result.platform is Platform.MERCADO_LIVRE
    assert result.external_item_id == ITEM_ID
    assert result.category_external_id == "MLB12345"
    assert result.category_external_name is None
    assert result.seller_id == "12345"
    assert result.image_url == "https://http2.mlstatic.com/synthetic.jpg"
    assert len(result.raw_fingerprint) == 64
    assert result.evidence_metadata["price_access_reason"] == "public_context_proven"
    assert [request[:2] for request in transport.requests] == [
        ("GET", f"https://api.mercadolibre.com/items/{ITEM_ID}"),
        (
            "GET",
            f"https://api.mercadolibre.com/items/{ITEM_ID}/sale_price?context=channel_marketplace",
        ),
        ("GET", f"https://api.mercadolibre.com/items/{ITEM_ID}/prices"),
    ]
    assert all(
        request[2]["headers"] == {"Authorization": f"Bearer {TOKEN}"}
        for request in transport.requests
    )


def test_only_public_normalized_evidence_is_retained_and_fingerprint_is_stable(payloads):
    source, transport = connector(payloads)
    result = source.fetch_product(ref())
    assert TOKEN not in repr(result)
    assert "PRIVATE-SYNTHETIC" not in repr(result)
    assert all(TOKEN not in request[2]["cache_key"] for request in transport.requests)
    modified = deepcopy(payloads)
    modified[1]["metadata"] = {"promotion_id": "OTHER-PRIVATE-SYNTHETIC"}
    second, _ = connector(modified, dates=[NOW - timedelta(seconds=5)] * 3)
    assert second.fetch_product(ref()).raw_fingerprint == result.raw_fingerprint


@pytest.mark.parametrize(
    "enabled,token",
    [(False, None), (False, TOKEN), (True, None), (True, ""), (True, "bad padded token")],
)
def test_disabled_or_missing_credentials_never_calls_transport(enabled, token):
    transport = SyntheticTransport([])
    source = MercadoLivreConnector(
        transport, token=SecretStr(token) if token is not None else None, enabled=enabled
    )
    with pytest.raises(ConnectorDisabled):
        source.fetch_product(ref())
    assert transport.requests == []


def test_capabilities_and_unsupported_operations_never_make_requests():
    transport = SyntheticTransport([])
    source = MercadoLivreConnector(transport)
    capabilities = {capability.name: capability for capability in source.capabilities()}
    assert capabilities["fetch_product"].status is CapabilityStatus.REQUIRES_CREDENTIALS
    assert capabilities["public_price"].status is CapabilityStatus.REQUIRES_CREDENTIALS
    assert capabilities["discover_offers"].status is CapabilityStatus.UNVERIFIED
    assert capabilities["fetch_coupons"].status is CapabilityStatus.UNVERIFIED
    assert capabilities["manual_affiliate_link"].status is CapabilityStatus.SUPPORTED
    for operation in [
        lambda: source.discover_offers("produto"),
        lambda: source.fetch_coupons(ref()),
        lambda: source.generate_affiliate_link(URL),
    ]:
        with pytest.raises(UnsupportedCapability):
            operation()
    assert transport.requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"platform": Platform.SHOPEE},
        {"external_item_id": "MLB123?evil=1"},
        {"canonical_url": "https://127.0.0.1/MLB3810190843"},
        {"canonical_url": "https://mercadolivre.com.br.evil.example/MLB3810190843"},
        {"canonical_url": "https://user:secret@mercadolivre.com.br/MLB3810190843"},
        {"canonical_url": "http://mercadolivre.com.br/MLB3810190843"},
        {"canonical_url": URL + "?access_token=synthetic-secret"},
        {"canonical_url": "https://produto.mercadolivre.com.br/MLB-999999-diferente"},
    ],
)
def test_invalid_or_unsafe_references_fail_before_network(payloads, changes):
    source, transport = connector(payloads)
    with pytest.raises(InvalidPayload):
        source.fetch_product(ref(**changes))
    assert transport.requests == []


@pytest.mark.parametrize("old", [None, "0", "-1", "80", "79.99", "NaN", "100.001"])
def test_absent_or_ineligible_old_price_is_not_invented(payloads, old):
    payloads[1]["regular_amount"] = old
    source, _ = connector(payloads)
    result = source.fetch_product(ref())
    assert result.current_price == Decimal("80")
    assert result.previous_price is None


@pytest.mark.parametrize(
    "amount", [None, "0", "-1", "NaN", "Infinity", "80.001", "10000000000.00", True, 80.0]
)
def test_invalid_current_price_is_an_error_not_a_replacement_observation(payloads, amount):
    payloads[1]["amount"] = amount
    source, _ = connector(payloads)
    with pytest.raises(InvalidPayload):
        source.fetch_product(ref())


@pytest.mark.parametrize(
    "restrictions",
    [["buyer_loyalty_6"], ["channel_marketplace", "user_type_business"], ["unknown_condition"]],
)
def test_restricted_or_vip_context_is_never_public(payloads, restrictions):
    payloads[2]["prices"][0]["conditions"]["context_restrictions"] = restrictions
    source, _ = connector(payloads)
    result = source.fetch_product(ref())
    assert result.price_access is PriceAccess.CONDITIONAL
    assert result.current_price == Decimal("80")
    assert result.previous_price is None


@pytest.mark.parametrize(
    "conditions",
    [
        None,
        {},
        {"context_restrictions": []},
        {"context_restrictions": None, "start_time": None, "end_time": None},
        {"context_restrictions": [], "start_time": "invalid", "end_time": None},
        {
            "context_restrictions": [],
            "start_time": NOW.replace(tzinfo=None).isoformat(),
            "end_time": None,
        },
        {
            "context_restrictions": [],
            "start_time": (NOW + timedelta(seconds=1)).isoformat(),
            "end_time": None,
        },
        {"context_restrictions": [], "start_time": None, "end_time": NOW.isoformat()},
    ],
)
def test_hidden_missing_invalid_or_noncurrent_conditions_do_not_prove_public_price(
    payloads, conditions
):
    payloads[2]["prices"][0]["conditions"] = conditions
    source, _ = connector(payloads)
    result = source.fetch_product(ref())
    assert result.price_access is PriceAccess.UNKNOWN
    assert result.previous_price is None


def test_unrestricted_perpetual_price_has_explicit_public_proof(payloads):
    payloads[2]["prices"][0]["conditions"] = {
        "context_restrictions": [],
        "start_time": None,
        "end_time": None,
    }
    source, _ = connector(payloads)
    assert source.fetch_product(ref()).price_access is PriceAccess.PUBLIC


@pytest.mark.parametrize("change", ["minimum_quantity", "quantity_metadata"])
def test_quantity_prices_remain_conditional(payloads, change):
    if change == "minimum_quantity":
        payloads[2]["prices"][0]["conditions"]["min_purchase_unit"] = 5
    else:
        payloads[1]["metadata"]["is_price_per_quantity"] = True
    source, _ = connector(payloads)
    assert source.fetch_product(ref()).price_access is PriceAccess.CONDITIONAL


@pytest.mark.parametrize("change", ["missing_id", "unmatched_id"])
def test_price_identity_is_required_to_associate_public_conditions(payloads, change):
    payloads[1]["price_id"] = None if change == "missing_id" else "999"
    source, _ = connector(payloads)
    assert source.fetch_product(ref()).price_access is PriceAccess.UNKNOWN


@pytest.mark.parametrize(
    "change",
    [
        "item_currency",
        "sale_currency",
        "prices_currency",
        "mismatched_amount",
        "duplicate_id",
        "bad_prices",
        "bad_item",
        "missing_title",
    ],
)
def test_inconsistent_or_unexpected_schema_fails_safely(payloads, change):
    if change == "item_currency":
        payloads[0]["currency_id"] = "USD"
    elif change == "sale_currency":
        payloads[1]["currency_id"] = "USD"
    elif change == "prices_currency":
        payloads[2]["prices"][0]["currency_id"] = "USD"
    elif change == "mismatched_amount":
        payloads[2]["prices"][0]["amount"] = "81"
    elif change == "duplicate_id":
        payloads[2]["prices"].append(deepcopy(payloads[2]["prices"][0]))
    elif change == "bad_prices":
        payloads[2]["prices"] = "invalid"
    elif change == "bad_item":
        payloads[0]["id"] = "MLB999999"
    else:
        del payloads[0]["title"]
    source, _ = connector(payloads)
    with pytest.raises(InvalidPayload) as error:
        source.fetch_product(ref())
    assert TOKEN not in str(error.value)
    assert "PRIVATE-SYNTHETIC" not in str(error.value)


@pytest.mark.parametrize(
    "change", ["listing_variants", "explicit_variant", "missing_variant_information"]
)
def test_unresolved_variants_do_not_trigger_price_calls_or_use_legacy_variation_price(
    payloads, change
):
    product_ref = ref()
    if change == "listing_variants":
        payloads[0]["variations"] = [{"id": 1234, "price": 1, "available_quantity": 99}]
    elif change == "explicit_variant":
        product_ref = ref(variant_id="1234")
    else:
        del payloads[0]["variations"]
    source, transport = connector(payloads)
    result = source.fetch_product(product_ref)
    assert result.current_price is None
    assert result.previous_price is None
    assert result.price_access is PriceAccess.UNKNOWN
    assert result.availability is Availability.UNKNOWN
    assert result.variant_id == product_ref.variant_id
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "change", [{"status": "paused"}, {"status": "closed"}, {"available_quantity": 0}]
)
def test_unavailable_item_is_metadata_only_without_invented_price(payloads, change):
    payloads[0].update(change)
    source, transport = connector(payloads)
    result = source.fetch_product(ref())
    assert result.availability is Availability.UNAVAILABLE
    assert result.current_price is None
    assert result.price_access is PriceAccess.UNKNOWN
    assert len(transport.requests) == 1


def test_unknown_quantity_does_not_claim_stock_availability(payloads):
    payloads[0].pop("available_quantity")
    source, _ = connector(payloads)
    result = source.fetch_product(ref())
    assert result.availability is Availability.UNKNOWN
    assert result.current_price == Decimal("80")


def test_cached_oldest_evidence_keeps_original_observation_time(payloads):
    source, _ = connector(payloads, dates=[NOW - timedelta(seconds=30), NOW, NOW])
    assert source.fetch_product(ref()).observed_at == NOW - timedelta(seconds=30)


@pytest.mark.parametrize("status", [401, 403])
def test_real_transport_authorization_failure_is_typed_and_not_retried(status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "PRIVATE-SYNTHETIC upstream error"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    transport = JsonTransport(client=client, min_interval=0, max_retries=2, clock=lambda: NOW)
    source = MercadoLivreConnector(transport, SecretStr(TOKEN), enabled=True, clock=lambda: NOW)
    try:
        with pytest.raises(AuthorizationDenied) as error:
            source.fetch_product(ref())
        assert "PRIVATE-SYNTHETIC" not in str(error.value)
        assert TOKEN not in str(error.value)
        assert len(calls) == 1
    finally:
        transport.close()


def test_transport_failure_propagates_without_returning_empty_success():
    transport = SyntheticTransport(
        [IntegrationError("TIMEOUT", "API temporariamente indisponível.")]
    )
    source = MercadoLivreConnector(transport, SecretStr(TOKEN), enabled=True, clock=lambda: NOW)
    with pytest.raises(IntegrationError) as error:
        source.fetch_product(ref())
    assert error.value.code == "TIMEOUT"


@pytest.mark.parametrize("suffix", ["?variation_id=1234", "#searchVariation=1234"])
def test_variant_context_in_registered_url_is_preserved_and_never_priced_as_generic_item(
    payloads, suffix
):
    source, transport = connector(payloads)
    result = source.fetch_product(ref(canonical_url=URL + suffix))
    assert result.canonical_url == URL + suffix
    assert result.current_price is None
    assert result.price_access is PriceAccess.UNKNOWN
    assert len(transport.requests) == 1


def test_unknown_new_price_condition_does_not_silently_become_public(payloads):
    payloads[2]["prices"][0]["conditions"]["unknown_customer_condition"] = True
    source, _ = connector(payloads)
    assert source.fetch_product(ref()).price_access is PriceAccess.UNKNOWN


def test_secret_in_canonical_fragment_is_rejected_before_network(payloads):
    source, transport = connector(payloads)
    with pytest.raises(InvalidPayload):
        source.fetch_product(ref(canonical_url=URL + "#access_token=synthetic-secret"))
    assert transport.requests == []
