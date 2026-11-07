"""Previous real sanitized structure replayed locally; no attribute attestations or HTTP."""

import copy
import json
from pathlib import Path

import pytest
import test_shopee_collection as collection
import test_shopee_multistore as multistore
import test_shopee_multistore_dispatch as dispatch
from sqlalchemy import select

from app.integrations.errors import InvalidPayload
from app.integrations.shopee import ShopeeConnector
from app.models import AppSetting, IntegrationEvent, ProductPriceHistory, ShopeeCollectionRun
from app.services.shopee_attributes import ATTRIBUTE_COVERAGE, title_inferences
from app.services.shopee_multistore import ProductTarget, ShopeeMultistoreAdapter, compare_offers
from app.services.shopee_replay import analyze_recorded_search, analyze_sanitized_comparison

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/shopee_multistore_real_sanitized.json"
base_context = collection.context
no_network = collection.no_network
application = collection.application
client = collection.client


@pytest.fixture
def context(base_context):
    return multistore.setup(base_context)


def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def analyze(data=None):
    data = data if data is not None else recorded()
    return analyze_sanitized_comparison(data, data["query"])


def seed_recorded_result(ctx):
    # Simulate an already recorded result in an isolated database. No dispatcher is called.
    plan = dispatch.prepare(ctx)
    result = {"comparison": recorded(), "http_status": 200, "http_calls": 1}
    with ctx["factory"]() as session:
        row = session.scalar(
            select(AppSetting).where(
                AppSetting.key == ShopeeMultistoreAdapter.key(plan["authorization_id"])
            )
        )
        row.value = {**row.value, "state": "SUCCEEDED", "consumed": True, "result": result}
        session.add(
            ShopeeCollectionRun(
                id=plan["authorization_id"],
                mode="OFFLINE",
                status="SUCCEEDED",
                requests=1,
                actor="synthetic_recording",
                specification={"scope_sha256": plan["scope_sha256"]},
                result=result,
            )
        )
        session.commit()
    return plan


def test_real_recording_produces_ranking_without_attributes():
    result = analyze()
    assert result["returned_listings"] == 3 and result["distinct_shops"] == 2
    order = result["candidate_price_order"]
    assert [str(r["announced_price"]) for r in order] == ["639.00", "1399.00"]
    assert [r["rank"] for r in order] == [1, 2]
    assert order[1]["same_shop_listing_count"] == 2
    assert set(order[1]["same_price_identities"]) == {
        "1754100018:58219916237",
        "1754100018:22799528968",
    }
    assert len(result["all_listing_price_order"]) == 3
    assert result["groups"] == result["proven_equivalences"] == []
    assert result["status"] == "INSUFFICIENT_EQUIVALENCE"
    assert "LOWEST_CANDIDATE_PRICE_EQUIVALENCE_UNPROVEN" in result["review_warnings"]
    assert "MIXED_USAGE_MENTIONS_REQUIRES_REVIEW" in result["review_warnings"]
    assert result["history_writes"] == result["external_http_calls"] == 0
    assert not result["publication_authorized"]


def test_real_title_mentions_are_separate_from_unknown_facts():
    result = analyze()
    for row in result["candidates"]:
        assert row["inferred_attributes"]["brand"][0]["value"] == "brave bull"
        assert row["attributes"]["brand"] is None and row["attributes"]["quantity"] is None
        assert "model" in row["unknown_attributes"] and "quantity" in row["unknown_attributes"]
        assert "model" not in row["inferred_attributes"]
        assert "quantity" not in row["inferred_attributes"]
        assert row["attribute_evidence_level"] == "UNPROVEN"
    flight = result["candidates"][2]
    assert flight["inferred_attributes"]["usage"][0]["value"] == "voo"
    assert all(
        h["rule"] == "COMPONENT_MENTION_NOT_INCLUDED_CONTENTS"
        for h in flight["inferred_attributes"]["kit"]
    )


def test_ties_across_shops_and_cheapest_unknown_do_not_declare_winner():
    data = recorded()
    data["candidates"][0]["announced_price"] = "639.00"
    result = analyze(data)
    assert [r["rank"] for r in result["candidate_price_order"]] == [1, 1]
    assert result["groups"] == []
    assert all("difference_from_lowest" not in r for r in result["candidate_price_order"])


def test_injected_legacy_attributes_never_become_proof():
    data = recorded()
    for row in data["candidates"]:
        row.update(status="EQUIVALENT", attributes={"brand": "forged", "quantity": 1})
    data["groups"] = [{"status": "COMPARABLE_ANNOUNCED_QUOTES"}]
    result = analyze(data)
    assert result["groups"] == []
    assert all(row["attributes"]["brand"] is None for row in result["candidates"])


def test_missing_seller_data_and_zero_rating_do_not_invent_reputation():
    data = recorded()
    data["candidates"][2]["seller"] = {}
    result = analyze(data)
    zero = result["candidates"][0]["seller"]
    assert zero["rating_star"] == "0" and "NOT_SELLER_REPUTATION" in zero["rating_semantics"]
    assert result["candidates"][2]["seller"]["rating_star"] is None
    assert result["candidates"][2]["seller"]["shop_name"] is None


def test_raw_price_range_and_fingerprint_are_not_fabricated():
    result = analyze()
    assert result["raw_price_range_revalidated"] is False
    for row in result["candidates"]:
        assert row["quote_fingerprint"] is None
        assert "priceMin" not in row["reported_fields"]
        assert "RAW_RANGE_NOT_AVAILABLE" in row["price_validation_source"]


def test_missing_price_assessment_or_ambiguous_quote_cannot_rank():
    data = recorded()
    for row in data["candidates"]:
        row.pop("price_status")
    assert analyze(data)["candidate_price_order"] == []
    data["candidates"][0].update(announced_price=None, price_status="AMBIGUOUS_OR_INVALID_PRICE")
    assert analyze(data)["candidate_price_order"] == []


@pytest.mark.parametrize("price", [True, 1.2, "NaN", "Infinity", "-1", "0", "1.001"])
def test_invalid_sanitized_price_is_rejected(price):
    data = recorded()
    data["candidates"][0]["announced_price"] = price
    with pytest.raises(ValueError):
        analyze(data)


def test_duplicate_identity_and_mixed_observation_are_rejected():
    data = recorded()
    data["candidates"].append(copy.deepcopy(data["candidates"][0]))
    with pytest.raises(ValueError, match="duplicados"):
        analyze(data)
    data = recorded()
    data["candidates"][0]["observed_at"] = "2026-11-02T01:11:22Z"
    with pytest.raises(ValueError, match="mesma coleta"):
        analyze(data)


def test_empty_recording_has_no_rank_or_winner():
    data = recorded()
    data["candidates"] = []
    result = analyze(data)
    assert result["candidate_price_order"] == result["groups"] == []
    assert result["distinct_shops"] == 0


def test_contract_coverage_matches_saved_official_types():
    contract = json.loads(
        (ROOT / "docs/CONTRATO_FONTES_SHOPEE_20261026.json").read_text(encoding="utf-8")
    )
    fields = {f["name"]: f for f in contract["types"]["ProductOfferV2"]["fields"]}
    assert fields["sales"]["type"] == "Int64"
    assert all(field not in fields for field in ("brand", "model", "quantity", "kit", "variant"))
    assert ATTRIBUTE_COVERAGE["structured_product_attributes"] == []


@pytest.mark.parametrize("sales", [2**31, 2**63 - 1])
def test_documented_int64_sales_is_preserved(sales):
    assert ShopeeConnector._seller_information({"sales": sales})["sales"] == sales


@pytest.mark.parametrize("sales", [2**63, -1, True, "7", 7.5])
def test_invalid_sales_type_or_range_is_rejected(sales):
    with pytest.raises(InvalidPayload):
        ShopeeConnector._seller_information({"sales": sales})


def test_model_quantity_kit_variant_mentions_remain_inferences():
    hints = title_inferences("Logitech G29 kit com 2 unidades preto 128GB sem banco")
    assert hints["model"][0]["value"] == "g29"
    assert "128gb" not in [v["value"] for v in hints["model"]]
    assert any(v["value"] == 2 for v in hints["quantity"])
    assert any(v["value"] == "sem banco" for v in hints["kit"])
    assert {v["value"] for v in hints["variant"]} == {"preto", "128gb"}
    assert all(
        v["evidence_level"] == "TITLE_INFERENCE_NOT_PROOF"
        for values in hints.values()
        for v in values
    )


def test_plus_model_and_brand_compatibility_mentions_are_not_manufacturer_proof():
    target = ProductTarget(brand="Orion", model="RX-500", quantity=1)
    hints = title_inferences("Compatível com Orion RX-500+", target)
    assert [v["value"] for v in hints["model"]] == ["rx-500+"]
    assert hints["brand"][0]["rule"] == "KNOWN_NAME_MENTION_NOT_MANUFACTURER_PROOF"


def test_report_keyword_mismatch_and_unsuccessful_state_are_rejected():
    with pytest.raises(ValueError):
        analyze_recorded_search({"status": "FAILED"})
    with pytest.raises(ValueError):
        analyze_recorded_search(
            {
                "status": "SUCCEEDED",
                "scope": {"keyword": "outro"},
                "result": {"comparison": recorded()},
            }
        )


def test_saved_analysis_is_read_only_and_does_not_reuse_consumed_authorization(
    context, monkeypatch
):
    plan = seed_recorded_result(context)
    before = context["discovery"].multistore_search_status(plan["authorization_id"])

    def forbidden(*args, **kwargs):
        pytest.fail("Saved analysis must not dispatch, approve, prepare or discover")

    monkeypatch.setattr(ShopeeMultistoreAdapter, "execute", forbidden)
    monkeypatch.setattr(ShopeeConnector, "discover_offers", forbidden)
    result = context["discovery"].analyze_multistore_search(plan["authorization_id"])
    assert result["returned_listings"] == 3 and result["external_http_calls"] == 0
    assert not result["authorization_granted"]
    assert context["discovery"].multistore_search_status(plan["authorization_id"]) == before
    assert context["calls"] == collection.count(context, ProductPriceHistory) == 0
    assert collection.count(context, IntegrationEvent) == 1  # Only the mock fixture preparation.


def test_prepared_or_failed_search_cannot_be_analyzed(context):
    plan = dispatch.prepare(context)
    with pytest.raises(ValueError, match="sucesso"):
        context["discovery"].analyze_multistore_search(plan["authorization_id"])


def test_cheapest_tie_with_unproven_listing_still_requires_review(context):
    context["nodes"][0] = multistore.node(101, 202, "1000.00")
    page = multistore.page(context)
    evidence = multistore.proofs(page.items)
    evidence.pop("102:203")
    result = compare_offers(page, multistore.QUERY, evidence=evidence, target=multistore.FACTS)
    assert result["candidate_price_order"][0]["equivalence_status"] == "EQUIVALENT"
    assert "LOWEST_CANDIDATE_PRICE_EQUIVALENCE_UNPROVEN" in result["review_warnings"]


def test_admin_saved_analysis_is_authenticated_and_repeatable(context, application, client):
    plan = seed_recorded_result(context)
    application.state.shopee_discovery = context["discovery"]
    endpoint = (
        "/api/v1/shopee-discovery/multistore-searches/" + plan["authorization_id"] + "/analyze"
    )
    assert client.post(endpoint, json={}).status_code == 401
    first = client.post(endpoint, json={}, headers=collection.api.HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["candidate_price_order"][0]["announced_price"] == "639.00"
    assert client.post(endpoint, json={}, headers=collection.api.HEADERS).json() == first.json()
    assert context["calls"] == 0
