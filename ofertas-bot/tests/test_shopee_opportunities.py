"""Consolidated discovery and automatic reuse of already valid documentary evidence."""

import copy

import pytest
import test_shopee_collection as collection
import test_shopee_defined_product as defined
import test_shopee_discovery as discovery
import test_shopee_evidence_review as reviews
import test_shopee_multistore as multistore
import test_shopee_replay as replay
from pydantic import ValidationError
from sqlalchemy import select

from app.models import AppSetting, Offer, ProductPriceHistory, PublicationQueueItem
from app.services.offers import OfferEngine
from app.services.shopee_evidence_review import ShopeeEvidenceReview
from app.services.shopee_multistore import ShopeeMultistoreAdapter
from app.services.shopee_opportunities import CommercialHandoffPreview, consolidate_opportunities

base_context = collection.context
discovery_context = discovery.context
no_network = collection.no_network
application = collection.application
client = collection.client


@pytest.fixture
def context(base_context):
    return multistore.setup(base_context)


def documented(context):
    ctx = reviews.setup(context)
    reviews.document(ctx)
    reviews.document(ctx, "102", "203")
    return ctx


def test_real_recording_automatically_selects_two_shops_without_claiming_equivalence():
    analysis = replay.analyze()
    view = analysis["opportunity_view"]
    assert view["counts"]["human_review_required"] == 3
    assert view["counts"]["selected_for_review"] == 2
    assert view["counts"]["deferred_review"] == 1
    assert len({r["shop_id"] for r in view["selected_for_review"]}) == 2
    assert view["commercial_handoff"]["candidates"] == []
    assert not view["commercial_handoff"]["execution_allowed"]
    assert all(
        d["attribute_states"]["brand"] == "INFERRED_ONLY" for d in view["candidate_decisions"]
    )
    assert all(d["attribute_states"]["model"] == "UNKNOWN" for d in view["candidate_decisions"])


def test_complete_evidence_does_not_wait_for_unknown_candidates(context):
    ctx = defined.setup(context)
    result = defined.review(ctx)
    view = result["opportunity_view"]
    assert view["counts"]["automatic_compared"] == 4  # Includes the same-store alternative.
    assert view["counts"]["human_review_required"] == 1
    assert view["counts"]["price_revalidation_required"] == 1
    assert len(view["proven_comparisons"]) == 1 and len(view["stores"]) == 11
    assert len(view["commercial_handoff"]["candidates"]) == 2  # Tie, never arbitrarily broken.
    assert {r["identity"] for r in view["commercial_handoff"]["candidates"]} == {
        "102:1003",
        "103:1004",
    }
    assert all(
        q["identity"] not in {"101:1001", "102:1002", "102:1003", "103:1004"}
        for q in result["review_queue"]
    )


def test_documented_reviews_are_reused_without_new_confirmation_or_offer_creation(
    context, monkeypatch
):
    ctx = documented(context)
    identity = ctx["plan"]["authorization_id"]
    before = ctx["discovery"].multistore_search_status(identity)
    review_before = ctx["reviews"].read(identity, "101", "202")

    def forbidden(*args, **kwargs):
        pytest.fail("Consolidation must not confirm, dispatch, publish or create an offer")

    monkeypatch.setattr(ShopeeEvidenceReview, "confirm", forbidden)
    monkeypatch.setattr(ShopeeMultistoreAdapter, "execute", forbidden)
    monkeypatch.setattr(OfferEngine, "process", forbidden)
    view = ctx["discovery"].multistore_opportunities(identity, target=multistore.FACTS)
    assert view["counts"]["automatic_compared"] == 2
    assert view["counts"]["human_review_required"] == 0
    assert view["selected_for_review"] == []
    assert view["source_authorization_id"] == identity
    assert len(view["commercial_handoff"]["candidates"]) == 1
    assert ctx["reviews"].read(identity, "101", "202") == review_before
    assert ctx["discovery"].multistore_search_status(identity) == before
    for model in (Offer, PublicationQueueItem, ProductPriceHistory):
        assert collection.count(ctx, model) == 0
    assert ctx["calls"] == 1


def test_draft_fields_are_pending_confirmation_not_automatic_facts(context):
    ctx = reviews.setup(context)
    identity = ctx["plan"]["authorization_id"]
    ctx["reviews"].edit(
        identity, "101", "202", reviews.edit_data(ctx, facts={"brand": "Brave Bull"})
    )
    view = ctx["discovery"].multistore_opportunities(identity)
    row = next(r for r in view["candidate_decisions"] if r["identity"] == "101:202")
    assert row["human_review_required"] and row["attribute_states"]["brand"] == "INFERRED_ONLY"
    assert (
        next(g for g in row["missing_information"] if g["attribute"] == "brand")["reason"]
        == "PENDING_MANUAL_CONFIRMATION"
    )
    assert view["proven_comparisons"] == []
    assert ctx["reviews"].read(identity, "101", "202")["state"] == "DRAFT"


def test_single_documented_seller_waits_for_peer_not_manual_reconfirmation(context):
    ctx = reviews.setup(context)
    identity = ctx["plan"]["authorization_id"]
    reviews.document(ctx)
    view = ctx["discovery"].multistore_opportunities(identity)
    decision = next(d for d in view["candidate_decisions"] if d["identity"] == "101:202")
    assert decision["action"] == "AWAITING_COMPARABLE_SELLER"
    assert not decision["human_review_required"]
    assert not any(q["identity"] == "101:202" for q in view["review_queue"])
    assert view["counts"]["human_review_required"] == 1


def test_changed_observation_and_stale_reviews_return_to_triage(context):
    ctx = documented(context)
    identity = ctx["plan"]["authorization_id"]
    with ctx["factory"]() as session:
        record = session.scalar(
            select(AppSetting).where(AppSetting.key == ShopeeMultistoreAdapter.key(identity))
        )
        value = copy.deepcopy(record.value)
        value["result"]["comparison"]["candidates"][0]["announced_price"] = "950.00"
        record.value = value
        session.commit()
    view = ctx["discovery"].multistore_opportunities(identity)
    assert view["counts"]["automatic_compared"] == 0
    assert view["counts"]["human_review_required"] == 2
    assert view["commercial_handoff"]["candidates"] == []
    assert all(
        any(g["attribute"] == "evidence_binding" for g in d["missing_information"])
        for d in view["candidate_decisions"]
        if d["human_review_required"]
    )


def test_title_conflicts_are_isolated_from_valid_comparisons(context):
    ctx = defined.setup(context)
    # Unknown variant is already an incomplete proof. Give a complete but contradictory
    # quantity statement to another listing; this must not stop the valid group.
    ctx["nodes"][4]["productName"] = "Orion RX-500 2 unidades"
    view = defined.review(ctx)["opportunity_view"]
    conflict = next(d for d in view["candidate_decisions"] if d["identity"] == "104:1005")
    assert conflict["human_review_required"] and conflict["conflicting_evidence"]
    assert conflict["attribute_states"]["quantity"] == "CONFLICTING_OR_INSUFFICIENT_DOCUMENTATION"
    assert view["proven_comparisons"][0]["distinct_shops"] == 3


def test_documented_incompatible_product_is_not_sent_for_manual_review(context):
    view = defined.review(defined.setup(context))["opportunity_view"]
    incompatible = next(d for d in view["candidate_decisions"] if d["identity"] == "105:1006")
    assert incompatible["action"] == "REJECTED_DOCUMENTED_MISMATCH"
    assert not incompatible["human_review_required"]


@pytest.mark.parametrize("limit", [0, 1, 2, 3])
def test_review_selection_budget_is_independent_of_request_quotas(limit):
    view = consolidate_opportunities(replay.analyze(), selection_limit=limit)
    assert len(view["selected_for_review"]) == min(limit, 3)
    assert view["counts"]["human_review_required"] == 3
    assert not view["publication_authorized"]


@pytest.mark.parametrize("limit", [True, -1, 21, 1.5])
def test_invalid_review_selection_limit_is_rejected(limit):
    with pytest.raises(ValueError):
        consolidate_opportunities(replay.analyze(), selection_limit=limit)


def test_handoff_remains_announced_only_and_cannot_authorize_publication(context):
    view = defined.review(defined.setup(context))["opportunity_view"]
    handoff = view["commercial_handoff"]
    assert handoff["reference_kind"] == "CROSS_STORE_ANNOUNCED_PRICE"
    assert handoff["historical_discount"] is None and not handoff["coupon_required"]
    assert not handoff["can_create_offer"] and not handoff["execution_allowed"]
    assert handoff["blockers"]
    assert all(
        c["final_price"] is None and c["availability"] == "UNKNOWN" for c in handoff["candidates"]
    )
    with pytest.raises(ValidationError):
        CommercialHandoffPreview(publication_authorized=True)


def test_existing_discovery_cycle_analyzes_the_already_fetched_page(discovery_context):
    ctx = discovery_context
    discovery.configure(ctx, page_size=2, max_pages=1)
    second = discovery.node(identity=2)
    second.update(shopId=7002, productLink="https://shopee.com.br/product/7002/2")
    ctx["pages"][1] = [discovery.node(), second]
    result = discovery.execute(ctx)
    assert result["status"] == "SUCCEEDED", result
    views = result["result"]["multistore_opportunities"]
    assert len(views) == 1 and views[0]["distinct_shops"] == 2
    assert views[0]["counts"]["human_review_required"] == 2
    assert ctx["calls"] == result["requests"] == 1
    assert collection.count(ctx, PublicationQueueItem) == 0


def test_consolidated_api_is_authenticated_and_read_only(context, application, client):
    ctx = documented(context)
    application.state.shopee_discovery = ctx["discovery"]
    url = (
        "/api/v1/shopee-discovery/multistore-searches/"
        + ctx["plan"]["authorization_id"]
        + "/opportunities"
    )
    assert client.post(url, json={}).status_code == 401
    response = client.post(url, json={"selection_limit": 0}, headers=collection.api.HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["automatic_compared"] == 2
    assert response.json()["commercial_handoff"]["can_create_offer"] is False
    assert (
        client.post(url, json={"selection_limit": True}, headers=collection.api.HEADERS).status_code
        == 422
    )
    assert ctx["calls"] == 1
