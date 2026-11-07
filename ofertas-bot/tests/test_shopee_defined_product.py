"""Defined-brand/model/quantity comparisons; every quote and attestation is synthetic."""

import copy
from dataclasses import replace
from decimal import Decimal

import pytest
import test_shopee_collection as collection
import test_shopee_multistore as multistore
from pydantic import ValidationError

from app.demo_shopee_multistore import fixture_evidence, fixture_nodes, load_catalog, run
from app.integrations.contracts import DiscoveryPage
from app.models import (
    AppSetting,
    IntegrationEvent,
    Offer,
    Product,
    ProductPriceHistory,
    PublicationQueueItem,
)
from app.services.shopee_multistore import ProductFacts, ProductTarget

base_context = collection.context
no_network = collection.no_network
application = collection.application
client = collection.client


def setup(ctx):
    ctx = multistore.setup(ctx)
    ctx["catalog"] = load_catalog()
    ctx["nodes"] = fixture_nodes(ctx["catalog"])
    return ctx


@pytest.fixture
def context(base_context):
    return setup(base_context)


def capture(ctx):
    product = ProductTarget.model_validate(ctx["catalog"]["product"])
    return ctx["multistore"].discover_offline(
        product.search_query(), ctx["connector"], limit=len(ctx["nodes"])
    )


def review(ctx, *, product=None, evidence=True):
    page = capture(ctx)
    return ctx["discovery"].review_defined_product(
        page,
        product or ctx["catalog"]["product"],
        evidence=fixture_evidence(ctx["catalog"], page) if evidence else None,
    )


def test_partial_target_accepts_only_defined_brand_model_quantity():
    target = ProductTarget(brand="Orion", model="RX-500", quantity=1)
    assert target.search_query() == "orion rx-500"
    assert target.kit is target.variant is target.price_conditions is None
    with pytest.raises(ValidationError):
        ProductFacts.model_validate(target.model_dump())


@pytest.mark.parametrize("quantity", [True, False, "1", 1.5, 0, -1, 10001])
def test_target_quantity_is_strict_positive_count(quantity):
    with pytest.raises(ValidationError):
        ProductTarget(brand="Orion", model="RX-500", quantity=quantity)


def test_defined_ranking_ties_and_deduplication(context):
    result = review(context)
    assert result["status"] == "COMPARABLE_ANNOUNCED_QUOTES"
    group = result["groups"][0]
    assert group["distinct_shops"] == 3
    ranking = group["ranking"]
    assert [r["identity"] for r in ranking] == ["102:1003", "103:1004", "101:1001"]
    assert [r["rank"] for r in ranking] == [1, 1, 3]
    assert ranking[-1]["difference_from_lowest"] == Decimal("200.00")
    assert ranking[-1]["difference_from_lowest_percent"] == Decimal("18.18")
    assert group["spread_amount"] == Decimal("200.00")
    assert "NOT_HISTORICAL_DISCOUNT" in group["spread_semantics"]
    assert len(result["incompatible_products"]) == 6
    assert len(result["inconclusive_products"]) == 2
    assert result["external_http_calls"] == result["mock_http_calls"] == 0
    assert context["calls"] == 1  # Review does not rediscover.


def test_partial_target_does_not_merge_different_kits_or_variants(context):
    result = review(context, product={"brand": "Orion", "model": "RX-500", "quantity": 1})
    assert len(result["groups"]) == 3
    assert sorted(g["distinct_shops"] for g in result["groups"]) == [1, 1, 3]
    assert sum(g["status"] == "COMPARABLE_ANNOUNCED_QUOTES" for g in result["groups"]) == 1
    assert all(
        not g["lowest_announced_identities"] for g in result["groups"] if g["distinct_shops"] == 1
    )


def test_documented_target_conflicts_are_explained_even_with_incomplete_title(context):
    result = review(context)
    rows = {row["identity"]: row for row in result["candidates"]}
    for identity, field in [
        ("104:1005", "kit"),
        ("105:1006", "brand"),
        ("106:1007", "quantity"),
        ("108:1009", "variant"),
        ("111:1012", "model"),
    ]:
        assert rows[identity]["status"] == "REJECTED"
        assert rows[identity]["mismatched_attributes"] == [field]
    assert rows["109:1010"]["reason"] == "EXCLUSION"


def test_target_is_not_evidence_and_no_unknown_variant_is_filled(context):
    result = review(context, evidence=False)
    assert result["groups"] == [] and result["status"] == "INSUFFICIENT_EQUIVALENCE"
    assert all(r["attributes"]["brand"] is None for r in result["candidates"])
    assert all("quantity" in r["unknown_attributes"] for r in result["candidates"])
    result = review(context)
    unknown = next(r for r in result["candidates"] if r["identity"] == "107:1008")
    assert unknown["status"] == "CANDIDATE" and unknown["attributes"]["variant"] is None


def test_different_price_conditions_are_separate_groups(context):
    context["catalog"]["listings"][0]["evidence_overrides"]["price_conditions"] = (
        "cupom VIP e somente aplicativo"
    )
    result = review(context)
    assert len(result["groups"]) == 2
    assert sorted(g["distinct_shops"] for g in result["groups"]) == [1, 2]
    comparable = next(g for g in result["groups"] if g["distinct_shops"] == 2)
    assert all("sem cupom" in r["attributes"]["price_conditions"] for r in comparable["ranking"])


def test_range_missing_ratings_and_quote_fingerprints(context):
    result = review(context)
    rows = {r["identity"]: r for r in result["candidates"]}
    assert rows["110:1011"]["price_status"] == "AMBIGUOUS_OR_INVALID_PRICE"
    assert rows["110:1011"]["announced_price"] is None
    assert rows["103:1004"]["seller"]["rating_star"] is None
    assert "NOT_SELLER_REPUTATION" in rows["103:1004"]["seller"]["rating_semantics"]
    assert all(len(r["quote_fingerprint"]) == 64 for r in rows.values())


def test_review_is_read_only_and_cannot_call_connector(context, monkeypatch):
    page = capture(context)
    evidence = fixture_evidence(context["catalog"], page)

    def forbidden(*args, **kwargs):
        pytest.fail("Review must not perform another discovery")

    monkeypatch.setattr(type(context["connector"]), "discover_offers", forbidden)
    result = context["discovery"].review_defined_product(
        page, context["catalog"]["product"], evidence=evidence
    )
    assert result["status"] == "COMPARABLE_ANNOUNCED_QUOTES"
    for model in (
        AppSetting,
        IntegrationEvent,
        Product,
        ProductPriceHistory,
        Offer,
        PublicationQueueItem,
    ):
        assert collection.count(context, model) == 0
    assert context["calls"] == 1


@pytest.mark.parametrize("limit", [True, 0, -1, 101, 1.5])
def test_offline_limit_is_bounded_before_http(context, limit):
    with pytest.raises(ValueError, match="Limite offline"):
        context["multistore"].discover_offline("Orion RX-500", context["connector"], limit=limit)
    assert context["calls"] == 0


def test_replay_rejects_duplicate_identity(context):
    page = capture(context)
    duplicate = DiscoveryPage([page.items[0], page.items[0]], 1, False)
    with pytest.raises(ValueError, match="duplicadas"):
        context["discovery"].review_defined_product(duplicate, context["catalog"]["product"])


def test_replay_cannot_rank_a_forged_single_price_over_a_range(context):
    page = capture(context)
    changed = replace(page.items[-2], current_price=Decimal("1200.00"))
    replay = DiscoveryPage([changed], 1, False)
    evidence = fixture_evidence(context["catalog"], replay)
    result = context["discovery"].review_defined_product(
        replay, context["catalog"]["product"], evidence=evidence
    )
    assert result["groups"] == result["candidate_price_order"] == []


def test_defined_product_search_uses_single_existing_discovery(context):
    result = context["discovery"].search_defined_product(context["catalog"]["product"], limit=12)
    assert result["query"] == "orion rx-500" and len(result["candidates"]) == 12
    assert context["calls"] == result["mock_http_calls"] == 1
    assert result["groups"] == [] and result["external_http_calls"] == 0


def test_defined_product_api_accepts_partial_target_and_decimal_prices(
    context, application, client
):
    endpoint = "/api/v1/shopee-discovery/offline-product-search"
    body = {"product": {"brand": "Orion", "model": "RX-500", "quantity": 1}, "limit": 12}
    assert client.post(endpoint, json=body).status_code == 401
    assert client.post(endpoint, json=body, headers=collection.api.HEADERS).status_code == 409
    application.state.shopee_discovery = context["discovery"]
    page = capture(context)
    proofs = fixture_evidence(context["catalog"], page)
    for proof in proofs.values():
        proof["verified_at"] = proof["verified_at"].isoformat()
    response = client.post(
        endpoint, json={**body, "evidence": proofs}, headers=collection.api.HEADERS
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["requested_product"] == {"brand": "orion", "model": "rx-500", "quantity": 1}
    group = next(g for g in result["groups"] if g["distinct_shops"] == 3)
    assert group["ranking"][0]["announced_price"] == "1099.90"
    assert group["ranking"][-1]["difference_from_lowest_percent"] == "18.18"
    invalid = copy.deepcopy(body)
    invalid["product"].pop("quantity")
    assert client.post(endpoint, json=invalid, headers=collection.api.HEADERS).status_code == 422


def test_runnable_demo_is_synthetic_single_request_and_no_history():
    result = run()
    assert result["data_kind"] == "SYNTHETIC_ONLY"
    assert result["discovery_mock_http_calls"] == 1 and result["returned_listings"] == 12
    assert result["external_http_calls"] == result["history_writes"] == 0
    assert result["groups"][0]["distinct_shops"] == 3
