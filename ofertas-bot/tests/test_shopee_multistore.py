"""Synthetic multi-store snapshots; equivalence is never inferred from titles."""

import copy
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import test_shopee as contracts
import test_shopee_collection as collection
from pydantic import ValidationError
from sqlalchemy import select

from app.integrations.contracts import DiscoveryPage
from app.integrations.errors import InvalidPayload
from app.integrations.registry import ConnectorRegistry
from app.models import (
    AppSetting,
    IntegrationEvent,
    Offer,
    Product,
    ProductPriceHistory,
    PublicationQueueItem,
    ShopeeDiscoveryRun,
)
from app.services.shopee_discovery import ShopeeDiscoveryService
from app.services.shopee_multistore import ProductFacts, ShopeeMultistoreService, compare_offers

base_context = collection.context
no_network = collection.no_network
application = collection.application
client = collection.client
QUERY = "Cockpit simulador Brave Bull"
FACTS = dict(
    brand="Brave Bull",
    model="cockpit X",
    quantity=1,
    condition="novo",
    packaging="embalagem original lacrada",
    kit=("estrutura", "banco"),
    variant="azul",
    price_conditions="anunciado sem cupom; pagamento e regiao identicos atestados no documento",
)


def node(shop, item, price, name=QUERY):
    return {
        **copy.deepcopy(contracts.SYNTHETIC_NODE),
        "shopId": shop,
        "itemId": item,
        "productLink": f"https://shopee.com.br/product/{shop}/{item}",
        "productName": name,
        "price": price,
        "priceMin": price,
        "priceMax": price,
        "shopName": f"FICTICIA Loja {shop}",
        "ratingStar": "4.8",
        "sales": 10,
        "shopType": [1],
    }


def setup(ctx):
    ctx["nodes"] = [
        node(101, 202, "1100.00"),
        node(102, 203, "1000.00"),
        node(103, 204, "500.00", QUERY + " capa para cockpit"),
    ]
    ctx["multistore"] = ShopeeMultistoreService(ctx["service"])
    registry = ConnectorRegistry(ctx["settings"], session_factory=ctx["factory"])
    registry._connectors[ctx["connector"].platform] = ctx["connector"]
    ctx["discovery"] = ShopeeDiscoveryService(
        ctx["factory"], ctx["settings"], registry=registry, clock=lambda: ctx["clock"][0]
    )
    return ctx


@pytest.fixture
def context(base_context):
    return setup(base_context)


def page(ctx):
    return ctx["multistore"].discover_offline(QUERY, ctx["connector"])


def proofs(items):
    return {
        s.external_item_id: dict(
            source="MANUAL_DOCUMENTED",
            facts=copy.deepcopy(FACTS),
            quote_fingerprint=s.raw_fingerprint,
            reference="FICTICIO documento de equivalencia",
            actor="test_admin",
            verified_at=s.observed_at,
        )
        for s in items
    }


def compare(ctx):
    data = page(ctx)
    return compare_offers(
        data, QUERY, evidence=proofs(data.items), target=FACTS, exclusions=("capa",)
    )


def test_documented_ranking_and_no_coupon(context):
    result = compare(context)
    assert result["status"] == "COMPARABLE_ANNOUNCED_QUOTES"
    ranked = result["groups"][0]["ranking"]
    assert [r["shop_id"] for r in ranked] == ["102", "101"]
    assert [r["announced_price"] for r in ranked] == [Decimal("1000.00"), Decimal("1100.00")]
    assert ranked[1]["difference_from_lowest"] == Decimal("100.00")
    assert result["candidates"][2]["status"] == "REJECTED"
    assert result["history_writes"] == 0 and not result["publication_authorized"]
    assert collection.count(context, ProductPriceHistory) == 0


def test_no_evidence_no_winner(context):
    result = compare_offers(page(context), QUERY)
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE" and result["groups"] == []


def test_incomplete_title_remains_candidate(context):
    context["nodes"][1]["productName"] = "Brave Bull cockpit"
    result = compare(context)
    assert result["candidates"][1]["status"] == "CANDIDATE"
    assert result["candidates"][1]["reason"] == "INCOMPLETE_QUERY_IDENTITY"
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"


def test_same_shop_deduplication(context):
    context["nodes"][2] = node(102, 999, "900.00")
    result = compare(context)
    ranking = result["groups"][0]["ranking"]
    assert len(ranking) == 2 and ranking[0]["identity"] == "102:999"


def test_tie(context):
    context["nodes"][0] = node(101, 202, "1000.00")
    ranking = compare(context)["groups"][0]
    assert len(ranking["lowest_announced_identities"]) == 2
    assert [r["rank"] for r in ranking["ranking"]] == [1, 1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("brand", "Outra"),
        ("model", "outro"),
        ("model", "cockpit X+"),
        ("quantity", 2),
        ("condition", "usado"),
        ("packaging", "sem embalagem original"),
        ("kit", ("estrutura",)),
        ("variant", "vermelho"),
        ("price_conditions", "cupom exclusivo VIP"),
    ],
)
def test_incompatible_facts(context, field, value):
    data = page(context)
    evidence = proofs(data.items)
    evidence["102:203"]["facts"][field] = value
    result = compare_offers(data, QUERY, evidence=evidence, target=FACTS, exclusions=("capa",))
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"
    assert result["candidates"][1]["status"] == "REJECTED"


def test_unknown_variant_not_verified(context):
    with pytest.raises(ValidationError):
        ProductFacts.model_validate({**FACTS, "variant": "UNKNOWN"})
    data = page(context)
    evidence = proofs(data.items)
    evidence["102:203"]["facts"]["variant"] = "UNKNOWN"
    result = compare_offers(data, QUERY, evidence=evidence, target=FACTS, exclusions=("capa",))
    assert result["candidates"][1]["reason"] == "INCOMPLETE_EQUIVALENCE_EVIDENCE"
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"


def test_ratings_absent_and_semantics(context):
    context["nodes"][0].pop("ratingStar")
    data = page(context)
    info = data.items[0].evidence_metadata["seller_information"]
    assert info["rating_star"] is None and "NOT_SELLER_REPUTATION" in info["rating_semantics"]
    assert info["shop_name"] == "FICTICIA Loja 101"


@pytest.mark.parametrize(
    "field,value",
    [
        ("ratingStar", 4.8),
        ("ratingStar", "NaN"),
        ("ratingStar", "-1"),
        ("sales", True),
        ("sales", -1),
        ("shopType", [True]),
        ("shopName", 42),
    ],
)
def test_invalid_seller_fields(context, field, value):
    context["nodes"][0][field] = value
    with pytest.raises(InvalidPayload):
        page(context)


def test_range_is_not_final_price(context):
    context["nodes"][1].update(priceMin="900", priceMax="1200")
    result = compare(context)
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"
    assert result["candidates"][1]["reason"] == "AMBIGUOUS_OR_INVALID_PRICE"


def test_invalid_price_rejected(context):
    context["nodes"][0]["price"] = "NaN"
    with pytest.raises(InvalidPayload):
        page(context)


def test_quote_bound_evidence(context):
    data = page(context)
    evidence = proofs(data.items)
    evidence["102:203"]["quote_fingerprint"] = "0" * 64
    result = compare_offers(data, QUERY, evidence=evidence, target=FACTS, exclusions=("capa",))
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"


def test_temporal_comparison_not_mixed(context):
    from datetime import timedelta

    data = page(context)
    mixed = DiscoveryPage(
        [
            data.items[0],
            replace(data.items[1], observed_at=data.items[1].observed_at + timedelta(hours=1)),
        ],
        1,
        False,
    )
    with pytest.raises(ValueError, match="mesma coleta"):
        compare_offers(mixed, QUERY)


def test_default_payload_preserved(context):
    legacy = contracts.ShopeeConnector.discovery_query(QUERY, page=1, limit=3)
    assert "shopName" not in legacy and "ratingStar" not in legacy
    selected = contracts.ShopeeConnector.discovery_query(
        QUERY, page=1, limit=3, profile="multistore"
    )
    assert all(field in selected for field in ("shopName", "ratingStar", "sales", "shopType"))
    assert "shopName" not in contracts.ShopeeConnector.product_query(contracts.REF)


def test_preparation_idempotent_and_blocked_execution(context):
    identity = uuid4()
    service = context["multistore"]
    first = service.prepare_search(identity, actor="test_admin", offline=True)
    assert service.prepare_search(identity, actor="test_admin", offline=True) == first
    assert first["state"] == "PREPARED" and first["approval"] is None and not first["consumed"]
    assert first["scope"]["limit"] == 3 and first["scope"]["max_http_calls"] == 1
    assert context["calls"] == 0
    with pytest.raises(ValueError, match="bloqueada"):
        service.execute_real(identity)
    with context["factory"]() as session:
        assert len(list(session.scalars(select(AppSetting)))) == 1


def test_discovery_search_is_integrated_and_read_only(context):
    data = page(context)
    result = context["discovery"].search_multistore(
        QUERY,
        evidence=proofs(data.items),
        target=FACTS,
    )
    assert result["status"] == "COMPARABLE_ANNOUNCED_QUOTES"
    assert result["candidates"][2]["status"] == "REJECTED"  # No caller exclusions needed.
    assert result["mock_http_calls"] == 1 and result["external_http_calls"] == 0
    assert result["has_next_not_followed"] is True
    assert result["comparison_kind"] == "CROSS_STORE_SAME_OBSERVATION"
    for model in (
        AppSetting,
        IntegrationEvent,
        Offer,
        Product,
        ProductPriceHistory,
        PublicationQueueItem,
        ShopeeDiscoveryRun,
    ):
        assert collection.count(context, model) == 0


def test_unproven_candidates_are_price_ordered_without_winner(context):
    result = context["discovery"].search_multistore(QUERY)
    assert [r["shop_id"] for r in result["candidate_price_order"]] == ["102", "101"]
    assert result["groups"] == []
    assert all("difference_from_lowest" not in r for r in result["candidate_price_order"])
    row = result["candidates"][0]
    assert row["attributes"]["packaging"] is None
    assert "variant" in row["unknown_attributes"]
    assert "seller_reputation" in result["commercial_unknowns"]


def test_packaging_must_be_documented(context):
    data = page(context)
    evidence = proofs(data.items)
    evidence["102:203"]["facts"].pop("packaging")
    result = compare_offers(data, QUERY, evidence=evidence, target=FACTS)
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"
    assert result["candidates"][1]["reason"] == "INCOMPLETE_EQUIVALENCE_EVIDENCE"


@pytest.mark.parametrize("value", ["?", "N/A", "não informado", "UNKNOWN", "-"])
def test_unknown_fact_markers_cannot_establish_equivalence(value):
    with pytest.raises(ValidationError):
        ProductFacts.model_validate({**FACTS, "packaging": value})


def test_changed_title_cannot_reuse_equivalence_proof(context):
    data = page(context)
    evidence = proofs(data.items)
    context["nodes"][1]["productName"] += " kit diferente"
    updated = page(context)
    assert updated.items[1].raw_fingerprint != data.items[1].raw_fingerprint
    result = compare_offers(updated, QUERY, evidence=evidence, target=FACTS)
    assert result["candidates"][1]["reason"] == "EVIDENCE_FOR_DIFFERENT_QUOTE"
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"


def test_missing_seller_information_is_explicit(context):
    for node_data in context["nodes"]:
        for field in ("shopName", "ratingStar", "sales", "shopType"):
            node_data.pop(field)
    result = context["discovery"].search_multistore(QUERY)
    seller = result["candidates"][0]["seller"]
    assert all(
        seller[field] is None for field in ("shop_name", "rating_star", "sales", "shop_types")
    )
    assert "NOT_SELLER_REPUTATION" in seller["rating_semantics"]


def test_only_one_shop_is_not_a_multistore_winner(context):
    context["nodes"] = [node(101, 202, "1000"), node(101, 203, "900")]
    result = compare(context)
    group = result["groups"][0]
    assert group["status"] == "INSUFFICIENT_STORES"
    assert group["lowest_announced_identities"] == []
    assert len(result["candidate_price_order"]) == 1


def test_empty_result_does_not_follow_next_page(context):
    context["nodes"] = []
    result = context["discovery"].search_multistore(QUERY)
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"
    assert result["candidates"] == result["candidate_price_order"] == []
    assert context["calls"] == 1


def test_multistore_timeout_has_no_retry_or_fallback(context):
    from app.integrations.errors import IntegrationError

    context["failure"] = "timeout"
    with pytest.raises(IntegrationError):
        context["discovery"].search_multistore(QUERY)
    assert context["calls"] == 1


def test_multistore_rejects_real_transport_before_request(context):
    mock_client = context["transport"].client
    with httpx.Client(trust_env=False) as real_client:
        context["transport"].client = real_client
        try:
            with pytest.raises(ValueError, match="MockTransport"):
                context["discovery"].search_multistore(QUERY)
        finally:
            context["transport"].client = mock_client
    assert context["calls"] == 0


def test_multistore_api_protection_and_decimal_strings(context, application, client):
    endpoint = "/api/v1/shopee-discovery/offline-multistore-search"
    payload = {"search": {"query": QUERY}}
    assert client.post(endpoint, json=payload).status_code == 401
    # The regular application's real transport is rejected before any request.
    assert client.post(endpoint, json=payload, headers=collection.api.HEADERS).status_code == 409
    application.state.shopee_discovery = context["discovery"]
    data = page(context)
    evidence = proofs(data.items)
    for proof in evidence.values():
        proof["verified_at"] = proof["verified_at"].isoformat()
    response = client.post(
        endpoint,
        json={**payload, "target": FACTS, "evidence": evidence},
        headers=collection.api.HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    ranking = result["groups"][0]["ranking"]
    assert ranking[0]["announced_price"] == "1000.00"
    assert ranking[1]["difference_from_lowest"] == "100.00"
    assert result["external_http_calls"] == 0
    assert (
        client.post(
            endpoint, json={"search": {"query": " "}}, headers=collection.api.HEADERS
        ).status_code
        == 422
    )


def test_multistore_api_invalid_rating_is_a_typed_error(context, application, client):
    application.state.shopee_discovery = context["discovery"]
    context["nodes"][0]["ratingStar"] = 4.8
    response = client.post(
        "/api/v1/shopee-discovery/offline-multistore-search",
        json={"search": {"query": QUERY}},
        headers=collection.api.HEADERS,
    )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "INVALID_PAYLOAD"
    assert context["calls"] == 1
