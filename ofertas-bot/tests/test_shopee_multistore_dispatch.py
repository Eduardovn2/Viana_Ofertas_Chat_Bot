"""The real one-shot dispatcher exercised only with synthetic MockTransport I/O."""

import copy
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
import test_shopee_collection as collection
import test_shopee_multistore as multistore
from pydantic import SecretStr
from sqlalchemy import select

from app.models import AppSetting, IntegrationEvent, ProductPriceHistory, ShopeeCollectionRun
from app.services.discovery_operations import KEY
from app.services.shopee_multistore import ShopeeMultistoreAdapter

base_context = collection.context
no_network = collection.no_network
application = collection.application
client = collection.client


@pytest.fixture
def context(base_context):
    return multistore.setup(base_context)


def specification(ctx):
    return dict(
        authorization_id=str(uuid4()),
        actor="test_owner",
        not_before=(ctx["clock"][0] + timedelta(minutes=1)).isoformat(),
        expires_at=(ctx["clock"][0] + timedelta(minutes=11)).isoformat(),
    )


def prepare(ctx, data=None):
    return ctx["discovery"].prepare_multistore_search(data or specification(ctx), offline=True)


def confirmation(plan):
    return dict(scope_sha256=plan["scope_sha256"], actor="test_owner", confirmed_single_read=True)


def approve(ctx, plan):
    return ctx["discovery"].approve_multistore_search(
        plan["authorization_id"], confirmation(plan), offline=True
    )


def execute(ctx, plan):
    return ctx["discovery"].execute_multistore_search(
        plan["authorization_id"], confirmation(plan), offline=True, connector=ctx["connector"]
    )


def ready(ctx):
    plan = prepare(ctx)
    approve(ctx, plan)
    ctx["clock"][0] += timedelta(minutes=1)
    return plan


def test_prepare_is_sealed_idempotent_and_never_calls(context):
    data = specification(context)
    plan = prepare(context, data)
    assert prepare(context, data) == plan
    assert plan["status"] == "PREPARED" and plan["approval"] is None
    assert not plan["consumed"] and plan["requests_reserved"] == 0
    body = json.dumps(plan["payload"], ensure_ascii=False, separators=(",", ":")).encode()
    assert plan["scope"]["payload_sha256"] == hashlib.sha256(body).hexdigest()
    assert plan["scope"]["keyword"] == multistore.QUERY
    assert plan["scope"]["max_http_calls"] == 1
    approved = approve(context, plan)
    assert approved["status"] == "APPROVED" and approved["requests_reserved"] == 0
    assert context["calls"] == 0


def test_success_uses_durable_reservation_and_existing_comparator(context):
    plan = ready(context)

    def inspect():
        with context["factory"]() as session:
            auth = session.scalar(
                select(AppSetting).where(
                    AppSetting.key == ShopeeMultistoreAdapter.key(plan["authorization_id"])
                )
            )
            run = session.get(ShopeeCollectionRun, plan["authorization_id"])
            assert auth.value["state"] == "CLAIMED" and auth.value["consumed"]
            assert run.status == "RESERVED" and run.requests == 1
            assert run.specification["operation"] == "MULTISTORE_NO_INGEST"
            assert session.scalars(
                select(IntegrationEvent.operation).order_by(IntegrationEvent.id)
            ).all() == [
                "multistore_search_prepared",
                "multistore_search_approved",
                "multistore_search_claimed",
            ]

    context["handler"] = inspect
    result = execute(context, plan)
    assert result["status"] == "SUCCEEDED" and result["requests_reserved"] == 1
    comparison = result["result"]["comparison"]
    assert [row["shop_id"] for row in comparison["candidate_price_order"]] == ["102", "101"]
    assert len(comparison["incompatible_products"]) == 1
    assert comparison["incompatible_products"][0]["shop_id"] == "103"
    assert comparison["status"] == "INSUFFICIENT_EQUIVALENCE"
    assert comparison["proven_equivalences"] == []  # Title/API fields are not proof.
    assert comparison["has_next_not_followed"]
    assert comparison["price_semantics"] == "ANNOUNCED_NOT_CONFIRMED_FINAL"
    assert execute(context, plan) == result and context["calls"] == 1
    assert collection.count(context, ProductPriceHistory) == 0


@pytest.mark.parametrize("size", [0, 1])
def test_insufficient_results_never_trigger_complementary_search(context, size):
    context["nodes"] = context["nodes"][:size]
    result = execute(context, ready(context))
    assert result["status"] == "SUCCEEDED"
    assert len(result["result"]["comparison"]["candidates"]) == size
    assert result["result"]["comparison"]["status"] == "INSUFFICIENT_EQUIVALENCE"
    assert context["calls"] == 1


@pytest.mark.parametrize(
    "failure,status", [(401, "FAILED"), (403, "FAILED"), ("timeout", "UNCERTAIN")]
)
def test_failure_is_consumed_and_not_retried(context, failure, status):
    context["failure"] = failure
    plan = ready(context)
    result = execute(context, plan)
    assert result["status"] == status and result["consumed"]
    assert result["result"]["http_calls"] == 1
    assert execute(context, plan) == result
    assert context["calls"] == 1


@pytest.mark.parametrize(
    "part,field,value",
    [
        ("request", "keyword", "Outro produto"),
        ("request", "limit", 4),
        ("request", "page", 2),
        ("scope", "payload_sha256", "0" * 64),
        ("scope", "endpoint", "https://example.invalid"),
        ("scope", "credential_scope", "0" * 64),
        ("scope", "expires_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_scope_tampering_before_claim_prevents_http(context, part, field, value):
    plan = ready(context)
    with context["factory"]() as session:
        row = session.scalar(
            select(AppSetting).where(
                AppSetting.key == ShopeeMultistoreAdapter.key(plan["authorization_id"])
            )
        )
        changed = copy.deepcopy(row.value)
        changed[part][field] = value
        row.value = changed
        session.commit()
    with pytest.raises(ValueError):
        execute(context, plan)
    assert context["calls"] == 0
    assert collection.count(context, ShopeeCollectionRun) == 0


def test_rotated_secret_invalidates_approval(context):
    plan = ready(context)
    context["settings"].shopee_secret = SecretStr("different_synthetic_secret")
    with pytest.raises(ValueError, match="escopo aprovado"):
        execute(context, plan)
    assert context["calls"] == 0


def test_no_approval_and_outside_window_cannot_consume(context):
    plan = prepare(context)
    with pytest.raises(ValueError, match="Aprovação"):
        execute(context, plan)
    approve(context, plan)
    with pytest.raises(ValueError, match="janela"):
        execute(context, plan)
    assert context["calls"] == 0 and collection.count(context, ShopeeCollectionRun) == 0
    context["clock"][0] += timedelta(minutes=12)
    assert execute(context, plan)["status"] == "EXPIRED"
    assert context["calls"] == 0


def test_uuid_cannot_be_reused_after_consumption(context):
    data = specification(context)
    plan = prepare(context, data)
    approve(context, plan)
    context["clock"][0] += timedelta(minutes=1)
    execute(context, plan)
    with pytest.raises(ValueError):
        prepare(
            context,
            {**data, "not_before": (context["clock"][0] + timedelta(minutes=1)).isoformat()},
        )
    assert context["calls"] == 1


def test_uuid_cannot_reuse_existing_authorization(context):
    data = specification(context)
    with context["factory"]() as session:
        session.add(
            AppSetting(
                key="shopee_recurring:" + data["authorization_id"], value={"state": "APPROVED"}
            )
        )
        session.commit()
    with pytest.raises(ValueError, match="UUID já utilizado"):
        prepare(context, data)
    assert context["calls"] == 0


def test_request_hook_prevents_second_physical_call(context, monkeypatch):
    original = type(context["connector"]).discover_offers

    def twice(connector, *args, **kwargs):
        original(connector, *args, **kwargs)
        return original(connector, *args, **kwargs)

    monkeypatch.setattr(type(context["connector"]), "discover_offers", twice)
    result = execute(context, ready(context))
    assert result["status"] == "FAILED" and result["error_code"] == "SCOPE_VIOLATION"
    assert context["calls"] == 1


def test_sanitized_result_excludes_credentials_and_urls(context):
    secret = context["settings"].shopee_secret.get_secret_value()
    context["nodes"][0]["productName"] = multistore.QUERY + "\n" + secret
    context["nodes"][0]["shopName"] = "Loja " + secret
    result = execute(context, ready(context))
    assert result["status"] == "SUCCEEDED"
    serialized = json.dumps(result)
    assert secret not in serialized and "[REDACTED]" in serialized
    comparison_json = json.dumps(result["result"]["comparison"])
    assert "productLink" not in comparison_json and "canonical_url" not in comparison_json
    assert collection.count(context, ProductPriceHistory) == 0


def test_admin_routes_are_authenticated_and_scope_is_readable(context, application, client):
    base = "/api/v1/shopee-discovery/multistore-searches"
    data = specification(context)
    assert client.post(base, json=data).status_code == 401
    plan = prepare(context, data)
    application.state.shopee_discovery = context["discovery"]
    url = base + "/" + plan["authorization_id"]
    response = client.get(url, headers=collection.api.HEADERS)
    assert response.status_code == 200 and response.json()["status"] == "PREPARED"
    # Public endpoints cannot request MOCK_ONLY or supply a mock connector.
    for suffix in ("/approve", "/execute"):
        response = client.post(
            url + suffix, json=confirmation(plan), headers=collection.api.HEADERS
        )
        assert response.status_code == 409
    assert context["calls"] == 0


def test_wire_payload_change_is_blocked_before_http(context, monkeypatch):
    original = type(context["connector"])._request

    def changed(connector, query, **kwargs):
        return original(connector, query.replace("limit:3", "limit:4"), **kwargs)

    monkeypatch.setattr(type(context["connector"]), "_request", changed)
    result = execute(context, ready(context))
    assert result["status"] == "FAILED" and result["error_code"] == "SCOPE_VIOLATION"
    assert result["consumed"] and result["requests_reserved"] == 1
    assert context["calls"] == 0


def test_reduced_result_budget_blocks_dispatch(context):
    plan = ready(context)
    with context["factory"]() as session:
        session.add(
            AppSetting(
                key=KEY, value={"revision": 1, "preferences": {"max_results_per_evaluation": 1}}
            )
        )
        session.commit()
    with pytest.raises(ValueError, match="Preferências"):
        execute(context, plan)
    assert context["calls"] == 0
    assert collection.count(context, ShopeeCollectionRun) == 0
