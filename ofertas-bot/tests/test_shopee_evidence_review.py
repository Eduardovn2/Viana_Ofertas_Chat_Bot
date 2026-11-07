"""Administrative documentary review exercised with fictitious documents and mock quotes."""

import copy
from datetime import timedelta
from pathlib import Path

import pytest
import test_shopee_collection as collection
import test_shopee_multistore as multistore
import test_shopee_multistore_dispatch as dispatch
import test_shopee_replay as replay
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.models import AppSetting, ProductPriceHistory
from app.services.shopee_evidence_review import ShopeeEvidenceReview
from app.services.shopee_evidence_sources import evidence_sources
from app.services.shopee_multistore import MultistoreSearchScope, ShopeeMultistoreAdapter

base_context = collection.context
no_network = collection.no_network
application = collection.application
client = collection.client


def setup(ctx):
    ctx = multistore.setup(ctx)
    plan = dispatch.ready(ctx)
    dispatch.execute(ctx, plan)  # One synthetic MockTransport discovery, never real HTTP.
    ctx["plan"] = plan
    ctx["reviews"] = ShopeeEvidenceReview(ctx["service"])
    return ctx


@pytest.fixture
def context(base_context):
    return setup(base_context)


def edit_data(ctx, shop="101", item="202", facts=None, **changes):
    state = ctx["reviews"].read(ctx["plan"]["authorization_id"], shop, item)
    return (
        dict(
            actor="synthetic_reviewer",
            expected_revision=state["revision"],
            observation_fingerprint=state["current_observation_fingerprint"],
            origin="MANUAL_LISTING_OBSERVATION",
            reference="FICTICIO documento de teste",
            source_observed_at=ctx["clock"][0].isoformat(),
            source_excerpt="Documento FICTICIO: atributos e termos explicitamente verificados nesta fixture.",
            binding_note="FICTICIO: documento vinculado ao shopId, itemId e opcao desta observacao.",
            facts=copy.deepcopy(multistore.FACTS) if facts is None else facts,
        )
        | changes
    )


def document(ctx, shop="101", item="202"):
    identity = ctx["plan"]["authorization_id"]
    draft = ctx["reviews"].edit(identity, shop, item, edit_data(ctx, shop, item))
    return ctx["reviews"].confirm(
        identity,
        shop,
        item,
        dict(
            actor="synthetic_reviewer",
            expected_revision=draft["revision"],
            confirmed_listing_binding=True,
        ),
    )


def test_high_textual_priority_is_not_proof():
    data = replay.recorded()
    for row in data["candidates"]:
        row["name"] = "Growth Creatina 250g premium 1 unidade"
    data["query"] = "Growth Creatina"
    from app.services.shopee_replay import analyze_sanitized_comparison

    result = analyze_sanitized_comparison(
        data,
        data["query"],
        target={
            "brand": "Growth",
            "model": "Creatina",
            "quantity": 1,
            "net_content": {"amount": "250", "unit": "g"},
            "edition": "premium",
        },
    )
    assert all(q["priority"]["score"] >= 80 for q in result["review_queue"])
    assert result["proven_comparisons"] == result["lowest_price_highlights"] == []
    assert not result["publication_authorized"]
    assert all(
        q["priority"]["semantics"].startswith("HEURISTIC_REVIEW_PRIORITY")
        for q in result["review_queue"]
    )


def test_textual_score_ignores_prices_sales_and_ratings():
    data = replay.recorded()
    first = replay.analyze(data)
    for row in data["candidates"]:
        row["announced_price"] = "1.00"
        row["seller"].update(rating_star="99", sales=2**40)
    second = replay.analyze(data)
    assert [(q["identity"], q["priority"]) for q in first["review_queue"]] == [
        (q["identity"], q["priority"]) for q in second["review_queue"]
    ]


def test_sources_are_local_contract_capabilities_not_permissions():
    sources = evidence_sources()
    feed = next(s for s in sources["sources"] if s["source"] == "listItemFeeds/getItemFeedData")
    assert feed["documented"] and not feed["implemented"]
    assert feed["column_dictionary"] == feed["account_permission"] == "UNKNOWN"
    assert not feed["automatic_requests_authorized"]
    assert not sources["account_permissions_checked"] and sources["external_http_calls"] == 0


def test_keyword_parameterization_preserves_existing_prepared_scope_hash():
    import json

    root = Path(__file__).resolve().parents[1]
    for filename in (
        "PREPARACAO_BUSCA_MULTILOJA_CONTROLADA_20261031_2210.json",
        "PREPARACAO_BUSCA_MULTILOJA_CONTROLADA_20261102.json",
    ):
        plan = json.loads((root / "docs" / filename).read_text(encoding="utf-8"))
        scope = plan["scope"]
        request = MultistoreSearchScope.model_validate(
            {key: scope[key] for key in MultistoreSearchScope.model_fields}
        )
        assert {**scope, **request.model_dump(mode="json")} == scope
        assert fingerprint(scope) == plan["scope_sha256"]


def test_new_keyword_only_prepares_a_new_sealed_mock_scope(context):
    data = dispatch.specification(context)
    data["keyword"] = "Growth Creatina 250g"
    before = context["calls"]
    plan = context["discovery"].prepare_multistore_search(data, offline=True)
    assert plan["status"] == "PREPARED" and plan["approval"] is None
    assert "Growth Creatina 250g" in plan["payload"]["query"]
    assert plan["scope"]["limit"] == 3 and plan["scope"]["max_http_calls"] == 1
    assert context["calls"] == before


def test_new_keyword_mock_execution_preserves_query_and_single_call(context):
    data = dispatch.specification(context)
    data["keyword"] = "Growth Creatina 250g"
    context["nodes"] = [multistore.node(101, 202, "50.00", data["keyword"])]
    plan = context["discovery"].prepare_multistore_search(data, offline=True)
    dispatch.approve(context, plan)
    context["clock"][0] += timedelta(minutes=1)
    before = context["calls"]
    result = dispatch.execute(context, plan)
    assert result["status"] == "SUCCEEDED"
    assert result["result"]["comparison"]["query"] == data["keyword"]
    assert len(result["result"]["comparison"]["candidates"]) == 1
    assert context["calls"] == before + 1
    assert dispatch.execute(context, plan) == result and context["calls"] == before + 1


def test_not_started_review_is_read_only(context):
    count = collection.count(context, AppSetting)
    state = context["reviews"].read(context["plan"]["authorization_id"], "101", "202")
    assert state["state"] == "NOT_STARTED" and state["facts"] == {}
    assert not state["readiness"]["ready"]
    assert collection.count(context, AppSetting) == count and context["calls"] == 1


def test_partial_sources_merge_with_per_attribute_provenance(context):
    identity = context["plan"]["authorization_id"]
    first = context["reviews"].edit(
        identity,
        "101",
        "202",
        edit_data(
            context,
            facts={"brand": "Brave Bull", "model": "cockpit X"},
            origin="OFFICIAL_MANUFACTURER_DOCUMENT",
        ),
    )
    assert first["state"] == "DRAFT" and not first["readiness"]["ready"]
    with pytest.raises(ValueError, match="Faltam"):
        context["reviews"].confirm(
            identity,
            "101",
            "202",
            dict(actor="reviewer", expected_revision=1, confirmed_listing_binding=True),
        )
    rest = {k: v for k, v in multistore.FACTS.items() if k not in {"brand", "model"}}
    second = context["reviews"].edit(identity, "101", "202", edit_data(context, facts=rest))
    assert second["readiness"]["ready"]
    assert second["field_sources"]["brand"]["origin"] == "OFFICIAL_MANUFACTURER_DOCUMENT"
    assert second["field_sources"]["quantity"]["origin"] == "MANUAL_LISTING_OBSERVATION"
    assert len(second["field_sources"]["brand"]["source_fingerprint"]) == 64
    assert "NOT_FETCHED" in second["field_sources"]["brand"]["fingerprint_kind"]


def test_confirmed_reviews_reuse_equivalence_gate_without_publication(context):
    identity = context["plan"]["authorization_id"]
    document(context)
    document(context, "102", "203")
    result = context["discovery"].analyze_multistore_search(identity, target=multistore.FACTS)
    assert len(result["proven_comparisons"]) == 1
    ranking = result["proven_comparisons"][0]["ranking"]
    assert [r["shop_id"] for r in ranking] == ["102", "101"]
    assert all(r["evidence_binding_kind"] == "SANITIZED_OBSERVATION_NOT_RAW_QUOTE" for r in ranking)
    assert all(r["quote_fingerprint"] is None for r in ranking)
    assert not result["raw_price_range_revalidated"] and not result["publication_authorized"]
    assert result["external_http_calls"] == collection.count(context, ProductPriceHistory) == 0
    assert context["calls"] == 1


def test_draft_never_promotes_and_authorization_is_preserved(context):
    identity = context["plan"]["authorization_id"]
    before = context["discovery"].multistore_search_status(identity)
    context["reviews"].edit(identity, "101", "202", edit_data(context))
    assert context["discovery"].analyze_multistore_search(identity)["proven_comparisons"] == []
    document(context, "102", "203")
    document(context)
    assert context["discovery"].multistore_search_status(identity) == before
    assert context["calls"] == 1


def test_editing_documented_review_withdraws_active_proof_and_preserves_archive(context):
    identity = context["plan"]["authorization_id"]
    first = document(context)
    document(context, "102", "203")
    draft = context["reviews"].edit(
        identity, "101", "202", edit_data(context, facts={"model": None})
    )
    assert draft["state"] == "DRAFT" and "model" not in draft["facts"]
    assert draft["previous_documentations"][0]["evidence_sha256"] == first["evidence_sha256"]
    assert context["discovery"].analyze_multistore_search(identity)["lowest_price_highlights"] == []


def test_stale_revision_and_fingerprint_are_rejected(context):
    identity = context["plan"]["authorization_id"]
    body = edit_data(context)
    context["reviews"].edit(identity, "101", "202", body)
    with pytest.raises(ValueError, match="Conflito"):
        context["reviews"].edit(identity, "101", "202", body)
    with pytest.raises(ValueError, match="Fingerprint"):
        context["reviews"].edit(
            identity, "101", "202", edit_data(context, observation_fingerprint="0" * 64)
        )


def test_changed_saved_observation_invalidates_review(context):
    identity = context["plan"]["authorization_id"]
    document(context)
    with context["factory"]() as session:
        row = session.scalar(
            select(AppSetting).where(AppSetting.key == ShopeeMultistoreAdapter.key(identity))
        )
        changed = copy.deepcopy(row.value)
        changed["result"]["comparison"]["candidates"][0]["announced_price"] = "888.00"
        row.value = changed
        session.commit()
    result = context["discovery"].analyze_multistore_search(identity)
    assert result["proven_comparisons"] == []
    assert result["candidates"][0]["review_issue"] == "INVALID_OR_STALE_DOCUMENTED_REVIEW"
    assert not context["reviews"].read(identity, "101", "202")["source_current"]


@pytest.mark.parametrize(
    "field,value", [("quantity", 1), ("condition", "novo"), ("price_conditions", "preço à vista")]
)
def test_manufacturer_document_cannot_supply_seller_terms(context, field, value):
    with pytest.raises(ValueError, match="fabricante"):
        context["reviews"].edit(
            context["plan"]["authorization_id"],
            "101",
            "202",
            edit_data(context, facts={field: value}, origin="OFFICIAL_MANUFACTURER_DOCUMENT"),
        )


def test_future_dates_and_credentials_are_rejected(context):
    identity = context["plan"]["authorization_id"]
    with pytest.raises(ValueError, match="futura"):
        context["reviews"].edit(
            identity,
            "101",
            "202",
            edit_data(
                context, source_observed_at=(context["clock"][0] + timedelta(days=1)).isoformat()
            ),
        )
    with pytest.raises(ValueError, match="credenciais"):
        context["reviews"].edit(
            identity,
            "101",
            "202",
            edit_data(
                context,
                source_excerpt="FICTICIO " + context["settings"].shopee_secret.get_secret_value(),
            ),
        )


def test_admin_review_routes_use_authentication_and_explicit_confirmation(
    context, application, client
):
    application.state.shopee_collection = context["service"]
    application.state.shopee_discovery = context["discovery"]
    identity = context["plan"]["authorization_id"]
    url = f"/api/v1/shopee-discovery/multistore-searches/{identity}/reviews/101/202"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=collection.api.HEADERS).json()["state"] == "NOT_STARTED"
    response = client.put(url, json=edit_data(context), headers=collection.api.HEADERS)
    assert response.status_code == 200, response.text
    data = dict(actor="reviewer", expected_revision=1, confirmed_listing_binding=False)
    assert (
        client.post(url + "/confirm", json=data, headers=collection.api.HEADERS).status_code == 422
    )
    data["confirmed_listing_binding"] = True
    response = client.post(url + "/confirm", json=data, headers=collection.api.HEADERS)
    assert response.status_code == 200 and response.json()["state"] == "DOCUMENTED"
    assert context["calls"] == 1
    assert (
        client.get(
            "/api/v1/shopee-discovery/evidence-sources", headers=collection.api.HEADERS
        ).status_code
        == 200
    )
