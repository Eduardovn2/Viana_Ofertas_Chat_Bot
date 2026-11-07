"""Standardized-product classification: documented facts versus explicit title differences."""

import copy
from decimal import Decimal

import pytest
import test_shopee_collection as collection
import test_shopee_multistore as multistore
import test_shopee_replay as replay
from pydantic import ValidationError

from app.demo_shopee_multistore import CATALOG, fixture_evidence, fixture_nodes, load_catalog, run
from app.services.shopee_attributes import title_inferences
from app.services.shopee_classification import NetContent, title_differences
from app.services.shopee_multistore import ProductTarget, compare_offers

base_context = collection.context
no_network = collection.no_network


def setup(ctx):
    ctx = multistore.setup(ctx)
    ctx["catalog"] = load_catalog(CATALOG.with_name("multistore_creatine.json"))
    ctx["nodes"] = fixture_nodes(ctx["catalog"])
    return ctx


@pytest.fixture
def context(base_context):
    return setup(base_context)


def review(ctx, *, documented=True, target=None):
    page = ctx["multistore"].discover_offline("Growth Creatina", ctx["connector"], limit=20)
    return compare_offers(
        page,
        "Growth Creatina",
        target=target or ctx["catalog"]["product"],
        evidence=fixture_evidence(ctx["catalog"], page) if documented else None,
    )


def pair(result, a, b):
    return next(
        p
        for p in result["pairwise_assessments"]
        if {p["left_identity"], p["right_identity"]} == {a, b}
    )


@pytest.mark.parametrize("amount,unit", [("250", "g"), ("0.25", "kg"), ("250000", "mg")])
def test_documented_mass_has_exact_canonical_representation(amount, unit):
    content = NetContent(amount=amount, unit=unit)
    assert content.model_dump() == {"amount": "250", "unit": "g"}
    assert NetContent.model_validate(content.model_dump()) == content


@pytest.mark.parametrize(
    "amount,unit", [(True, "g"), (0.25, "kg"), ("NaN", "g"), ("-1", "g"), ("0", "g"), ("250", "oz")]
)
def test_invalid_or_inexact_content_is_rejected(amount, unit):
    with pytest.raises(ValidationError):
        NetContent(amount=amount, unit=unit)


def test_mass_and_volume_are_not_interchangeable():
    assert NetContent(amount="250", unit="g") != NetContent(amount="250", unit="ml")
    assert NetContent(amount="0.25", unit="l").model_dump() == {"amount": "250", "unit": "ml"}


def test_creatine_proven_groups_do_not_mix_weight_or_edition(context):
    result = review(context)
    assert len(result["proven_comparisons"]) == 1
    group = result["proven_comparisons"][0]
    assert group["distinct_shops"] == 2
    assert [r["identity"] for r in group["ranking"]] == ["202:2002", "201:2001"]
    assert result["lowest_price_highlights"][0]["announced_price"] == Decimal("64.90")
    assert result["lowest_price_highlights"][0]["identities"] == ["202:2002"]
    assert pair(result, "201:2001", "202:2002")["classification"] == "PROVEN_EQUIVALENT"
    assert pair(result, "201:2001", "203:2004")["classification"] == "PROVEN_INCOMPATIBLE"
    assert pair(result, "201:2001", "204:2005")["classification"] == "PROVEN_INCOMPATIBLE"
    assert pair(result, "201:2001", "205:2006")["classification"] == "INCONCLUSIVE"
    assert result["history_writes"] == 0 and not result["publication_authorized"]


def test_title_only_differences_never_prove_equivalence_or_incompatibility(context):
    result = review(context, documented=False)
    assert result["proven_comparisons"] == result["lowest_price_highlights"] == []
    weight = pair(result, "201:2001", "203:2004")
    assert weight["classification"] == "INCONCLUSIVE" and weight["proven_differences"] == []
    assert weight["title_differences"][0]["attribute"] == "net_content"
    edition = pair(result, "201:2001", "204:2005")
    assert any(d["attribute"] == "edition" for d in edition["title_differences"])
    accessory = pair(result, "201:2001", "205:2006")
    assert any(d["attribute"] == "product_role" for d in accessory["title_differences"])
    assert all(r["equivalence_classification"] == "INCONCLUSIVE" for r in result["candidates"])


def test_store_alternatives_and_unproven_cheapest_are_preserved(context):
    result = review(context)
    store = next(s for s in result["stores"] if s["shop_id"] == "202")
    assert {r["identity"] for r in store["listings"]} == {"202:2002", "202:2003"}
    assert all(r["equivalence_classification"] == "PROVEN_EQUIVALENT" for r in store["listings"])
    assert any(r["identity"] == "205:2006" for r in result["unproven_candidates"])
    assert all(
        "rank" not in r and "difference_from_lowest" not in r for r in result["unproven_candidates"]
    )
    assert result["candidate_price_order"][0]["announced_price"] == Decimal("9.90")
    assert all(h["announced_price"] != Decimal("9.90") for h in result["lowest_price_highlights"])


def test_requested_content_proven_mismatch_is_separate_from_unknown(context):
    result = review(
        context,
        target={**context["catalog"]["product"], "net_content": {"amount": "250", "unit": "g"}},
    )
    rejected = {r["identity"]: r for r in result["proven_incompatible_products"]}
    assert "net_content" in rejected["203:2004"]["mismatched_attributes"]
    assert "quantity" in rejected["208:2009"]["mismatched_attributes"]
    assert "206:2007" not in rejected


def test_missing_documented_mass_cannot_rank_creatine(context):
    context["catalog"]["documented_facts"].pop("net_content")
    for listing in context["catalog"]["listings"]:
        if listing["evidence_overrides"] is not None:
            listing["evidence_overrides"].pop("net_content", None)
    result = review(context)
    assert result["proven_comparisons"] == result["lowest_price_highlights"] == []
    row = result["candidates"][0]
    assert "NET_CONTENT_UNPROVEN" in row["evidence_conflicts"]
    assert row["attributes"]["net_content"] is None


def test_filtered_accessory_is_not_proven_incompatible(context):
    context["nodes"][5]["productName"] = "Capa para Growth Creatina"
    result = review(context, documented=False)
    assert any(r["identity"] == "205:2006" for r in result["filtered_candidates"])
    assert result["proven_incompatible_products"] == []


def test_requested_content_absence_is_not_a_proven_mismatch(context):
    context["catalog"]["documented_facts"].pop("net_content")
    result = review(
        context,
        target={**context["catalog"]["product"], "net_content": {"amount": "250", "unit": "g"}},
    )
    row = result["candidates"][0]
    assert row["equivalence_classification"] == "INCONCLUSIVE"
    assert row not in result["proven_incompatible_products"]


@pytest.mark.parametrize(
    "name,issue",
    [
        ("Growth Creatina 300g comum", "AMBIGUOUS_OR_CONFLICTING_TITLE_CONTENT"),
        ("Growth Creatina 250g/300g comum", "AMBIGUOUS_OR_CONFLICTING_TITLE_CONTENT"),
        ("Growth Creatina 250g premium", "AMBIGUOUS_OR_CONFLICTING_TITLE_EDITION"),
        ("Growth Creatina 2 unidades 250g comum", "AMBIGUOUS_OR_CONFLICTING_TITLE_UNIT_COUNT"),
        ("Dosador para Growth Creatina", "POSSIBLE_ACCESSORY_VERSUS_DOCUMENTED_PRODUCT"),
    ],
)
def test_title_and_document_conflicts_require_review(context, name, issue):
    context["nodes"][0]["productName"] = name
    result = review(context)
    row = result["candidates"][0]
    assert issue in row["evidence_conflicts"]
    assert row["equivalence_classification"] == "INCONCLUSIVE"
    assert "201:2001" not in {
        r["identity"] for g in result["proven_comparisons"] for r in g["ranking"]
    }


def test_dose_and_unit_count_are_not_total_content():
    hints = title_inferences("Growth Creatina pote 300g, 3g por dose, kit 2 x 250g")
    contents = [h["value"] for h in hints["net_content"]]
    assert {h["amount"] for h in contents if h["role"] != "DOSE_MENTION"} == {"300", "250"}
    assert any(h["amount"] == "3" and h["role"] == "DOSE_MENTION" for h in contents)
    assert any(
        h["value"] == 2 and h["rule"] == "MULTIPACK_COUNT_MENTION" for h in hints["quantity"]
    )
    assert not any(h["value"] == 500 for h in hints["quantity"])


def test_absence_of_premium_does_not_imply_standard():
    assert "edition" not in title_inferences("Growth Creatina 250g")
    assert ProductTarget(brand="Growth", model="Creatina", quantity=1).edition is None


@pytest.mark.parametrize("with_edition_in_title", [False, True])
def test_unknown_creatine_edition_never_becomes_standard(context, with_edition_in_title):
    context["catalog"]["documented_facts"].pop("edition")
    context["nodes"][0]["productName"] = "Growth Creatina 250g" + (
        " comum" if with_edition_in_title else ""
    )
    result = review(context)
    row = result["candidates"][0]
    assert "EDITION_UNPROVEN" in row["evidence_conflicts"]
    assert row["attributes"]["edition"] is None
    assert row["equivalence_classification"] == "INCONCLUSIVE"
    assert result["lowest_price_highlights"] == []


def test_explicit_kit_and_variant_differences_remain_title_only():
    left = {"inferred_attributes": title_inferences("Brave Bull cockpit preto com banco")}
    right = {"inferred_attributes": title_inferences("Brave Bull cockpit azul sem banco")}
    differences = title_differences(left, right)
    assert {d["attribute"] for d in differences} == {"kit", "variant"}
    assert all(
        d["evidence_level"] == "EXPLICIT_TITLE_DIFFERENCE_NOT_PRODUCT_PROOF" for d in differences
    )


def test_real_sanitized_regression_has_no_highlight_or_invented_proof():
    result = replay.analyze()
    assert len(result["stores"]) == 2
    assert result["proven_comparisons"] == result["lowest_price_highlights"] == []
    assert len(result["unproven_candidates"]) == 3
    assert all(p["classification"] == "INCONCLUSIVE" for p in result["pairwise_assessments"])
    assert result["external_http_calls"] == 0


def test_pairwise_output_limit_preserves_all_listings():
    data = replay.recorded()
    base = data["candidates"][0]
    data["candidates"] = []
    for index in range(40):
        row = copy.deepcopy(base)
        row.update(
            shop_id=str(500 + index),
            item_id=str(1000 + index),
            identity=f"{500 + index}:{1000 + index}",
        )
        data["candidates"].append(row)
    result = replay.analyze(data)
    assert result["pairwise_assessments_truncated"]
    assert len(result["pairwise_assessments"]) == 500
    assert len(result["stores"]) == result["returned_listings"] == 40


def test_creatine_demo_uses_one_mock_request_only():
    result = run(CATALOG.with_name("multistore_creatine.json"))
    assert result["data_kind"] == "SYNTHETIC_ONLY"
    assert result["discovery_mock_http_calls"] == 1 and result["external_http_calls"] == 0
    assert len(result["proven_comparisons"]) == 1
