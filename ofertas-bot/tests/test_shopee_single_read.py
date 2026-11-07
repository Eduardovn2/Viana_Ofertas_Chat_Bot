"""A one-shot real adapter exercised exclusively with synthetic MockTransport I/O."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
import test_api as api
import test_shopee_collection as collection
import test_shopee_observation as observation
from pydantic import SecretStr, ValidationError
from sqlalchemy import event, select

from app.models import AppSetting, IntegrationEvent, Platform, Product, ShopeeCollectionRun
from app.services.discovery_operations import DiscoveryOperations
from app.services.shopee_single_read import ReadConfirmation, ShopeeSingleReadAdapter

base_context = collection.context
no_network = collection.no_network
application = api.application
client = api.client


def setup(ctx):
    observation.prepare(ctx)
    ctx["single_read"] = ShopeeSingleReadAdapter(ctx["service"])
    return ctx


@pytest.fixture
def context(base_context):
    return setup(base_context)


def prepare(ctx):
    return ctx["single_read"].prepare(observation.specification(ctx), offline=True)


def confirmation(plan):
    return {
        "scope_sha256": plan["scope_sha256"],
        "actor": "responsavel",
        "confirmed_single_read": True,
    }


def approve(ctx, plan):
    return ctx["single_read"].approve(
        plan["authorization_id"],
        {
            **confirmation(plan),
            "expires_at": (ctx["clock"][0] + timedelta(seconds=300)).isoformat(),
        },
        offline=True,
    )


def execute(ctx, plan):
    return ctx["single_read"].execute(
        plan["authorization_id"], confirmation(plan), offline=True, connector=ctx["connector"]
    )


def test_plan_is_readonly_and_binds_exact_payload(context):
    plan = context["single_read"].plan(observation.specification(context), offline=True)
    assert plan["status"] == "PLAN_ONLY" and plan["mode"] == "MOCK_ONLY"
    assert "productOfferV2(shopId:101,itemId:202,page:1,limit:1)" in plan["payload"]["query"]
    assert context["calls"] == collection.count(context, AppSetting) == 0
    assert (
        collection.count(context, ShopeeCollectionRun)
        == collection.count(context, IntegrationEvent)
        == 0
    )


def test_preparation_and_approval_never_call_upstream(context):
    data = observation.specification(context)
    first = context["single_read"].prepare(data, offline=True)
    assert context["single_read"].prepare(data, offline=True) == first
    assert first["status"] == "PREPARED" and first["requests_reserved"] == 0
    approved = approve(context, first)
    assert approved["status"] == "APPROVED" and not approved["consumed"]
    assert approved["approval"]["actor"] == "responsavel"
    assert context["calls"] == collection.count(context, ShopeeCollectionRun) == 0
    observation.assert_no_commerce(context)


def test_claim_is_durable_before_http_and_never_ingests(context):
    plan = prepare(context)
    approve(context, plan)

    def inspect_claim():
        with context["factory"]() as session:
            row = session.scalar(select(AppSetting))
            run = session.get(ShopeeCollectionRun, plan["authorization_id"])
            assert row.value["state"] == "CLAIMED" and row.value["consumed"]
            assert run.requests == 1 and run.status == "RESERVED"
            assert run.specification["operation"] == "OBSERVE_NO_INGEST"
            operations = session.scalars(
                select(IntegrationEvent.operation).order_by(IntegrationEvent.id)
            ).all()
            assert operations == [
                "identity_read_prepared",
                "identity_read_approved",
                "identity_read_claimed",
            ]

    context["handler"] = inspect_claim
    result = execute(context, plan)
    assert result["status"] == "SUCCEEDED", result
    assert result["consumed"] and result["requests_reserved"] == 1
    assert result["quote"]["price"] == "99.90"
    assert result["quote"]["identity"] == "101:202"
    assert result["result"]["http_status"] == 200 and result["result"]["http_calls"] == 1
    assert not result["quote"]["commercial_verified"]
    assert result["quote"]["price_access"] == result["quote"]["availability"] == "UNKNOWN"
    replay = execute(context, plan)
    assert replay["status"] == "SUCCEEDED" and context["calls"] == 1
    assert "quote" not in replay and result["result"]["quote_persisted"] is False
    assert observation.histories(context) == []
    observation.assert_no_commerce(context)


def test_announced_fields_are_sanitized_without_claiming_promotion(context):
    context["node"].update(
        priceDiscountRate=100,
        periodStartTime=0,
        periodEndTime=1792969200000,
        stock=999,
        payment="PIX",
        coupon="FAKE",
        offerLink="https://evil.test/?tracking=SECRET",
    )
    context["node"]["productName"] = (
        "SYNTHETIC " + context["settings"].shopee_secret.get_secret_value()
    )
    plan = prepare(context)
    approve(context, plan)
    result = execute(context, plan)["quote"]
    assert result["advertised_discount_percent"] == 100
    assert result["offer_period_start_raw"] == 0 and result["offer_period_end_raw"] == 1792969200000
    assert result["period_time_unit"] == result["period_semantics"] == "UNCONFIRMED"
    assert result["name"] == "SYNTHETIC [REDACTED]"
    assert "tracking" not in json.dumps(result) and "commission" not in result
    assert observation.histories(context) == []


def test_unresolved_range_is_returned_without_selecting_variant(context):
    context["node"].update(priceMin="80", priceMax="110")
    plan = prepare(context)
    approve(context, plan)
    result = execute(context, plan)["quote"]
    assert (
        result["price"] is None
        and result["price_min"] == "80.00"
        and result["price_max"] == "110.00"
    )
    assert not result["commercial_verified"]


def test_unapproved_or_wrong_hash_never_claims(context):
    plan = prepare(context)
    with pytest.raises(ValueError, match="Aprovação"):
        execute(context, plan)
    approve(context, plan)
    with pytest.raises(ValueError, match="escopo"):
        context["single_read"].execute(
            plan["authorization_id"],
            {**confirmation(plan), "scope_sha256": "0" * 64},
            offline=True,
            connector=context["connector"],
        )
    assert not context["single_read"].read(plan["authorization_id"])["consumed"]
    assert context["calls"] == 0


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_confirmation_must_be_explicit_and_strict(value):
    with pytest.raises(ValidationError):
        ReadConfirmation.model_validate(
            {"scope_sha256": "0" * 64, "actor": "responsavel", "confirmed_single_read": value}
        )


def test_expired_authorization_cannot_be_extended_or_consumed(context):
    plan = prepare(context)
    approve(context, plan)
    context["clock"][0] += timedelta(seconds=301)
    result = execute(context, plan)
    assert result["status"] == "EXPIRED" and not result["consumed"]
    with pytest.raises(ValueError, match="encerrada"):
        approve(context, plan)
    assert context["calls"] == 0


@pytest.mark.parametrize("change", ["url", "credential", "mode", "payload"])
def test_approval_cannot_authorize_a_changed_scope(context, change, monkeypatch):
    plan = prepare(context)
    approve(context, plan)
    if change == "url":
        with context["factory"]() as session:
            session.get(
                Product, context["product_id"]
            ).original_url = "https://shopee.com.br/changed-i.101.202"
            session.commit()
    elif change == "credential":
        context["settings"].shopee_app_id = SecretStr("DIFFERENT_SYNTHETIC_APP")
    elif change == "mode":
        with context["factory"]() as session:
            row = session.scalar(select(AppSetting))
            row.value = {**row.value, "mode": "REAL_SINGLE_READ"}
            session.commit()
    else:
        from app.integrations.shopee import ShopeeConnector

        original = ShopeeConnector.product_query
        monkeypatch.setattr(
            ShopeeConnector, "product_query", staticmethod(lambda ref: original(ref) + " ")
        )
    with pytest.raises(ValueError, match="escopo"):
        execute(context, plan)
    assert context["calls"] == 0


@pytest.mark.parametrize(
    "failure,status,code,http_status",
    [
        ("timeout", "UNCERTAIN", "TIMEOUT", None),
        (403, "FAILED", "AUTHORIZATION_DENIED", 403),
        (429, "FAILED", "RATE_LIMITED", 429),
        (500, "UNCERTAIN", "UPSTREAM_ERROR", 500),
        ("graphql", "FAILED", "UPSTREAM_GRAPHQL_ERROR", 200),
    ],
)
def test_one_consumed_attempt_on_failure_without_retry(context, failure, status, code, http_status):
    context["failure"] = failure
    plan = prepare(context)
    approve(context, plan)
    result = execute(context, plan)
    assert result["status"] == status and result["error_code"] == code, result
    assert result["result"]["http_status"] == http_status
    if failure == "timeout":
        diagnostic = result["result"]["http_diagnostics"]
        assert diagnostic["timeout_phase"] == "READ" and diagnostic["error_type"] == "ReadTimeout"
    assert execute(context, plan) == result and context["calls"] == 1
    with pytest.raises(ValueError, match="consumida"):
        approve(context, plan)
    assert observation.histories(context) == []


@pytest.mark.parametrize("failure", ["empty", "identity", "invalid_discount"])
def test_invalid_result_never_falls_back_to_another_product(context, failure):
    if failure == "empty":
        context["nodes"] = []
    elif failure == "identity":
        context["node"].update(itemId=999, productLink="https://shopee.com.br/product/101/999")
    else:
        context["node"]["priceDiscountRate"] = 101
    plan = prepare(context)
    approve(context, plan)
    result = execute(context, plan)
    assert result["status"] == "FAILED"
    assert execute(context, plan) == result and context["calls"] == 1
    observation.assert_no_commerce(context)


def test_interruption_is_uncertain_and_never_reissued(context):
    def interrupt():
        raise KeyboardInterrupt()

    context["handler"] = interrupt
    plan = prepare(context)
    approve(context, plan)
    result = execute(context, plan)
    assert result["status"] == "UNCERTAIN" and result["consumed"]
    assert execute(context, plan) == result and context["calls"] == 1


def test_persistence_failure_leaves_consumed_durable_claim(context, monkeypatch):
    plan = prepare(context)
    approve(context, plan)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic database outage")

    with monkeypatch.context() as patch:
        patch.setattr(context["single_read"], "_complete", fail)
        result = execute(context, plan)
        assert result["status"] == "UNCERTAIN" and result["consumed"]
    stored = context["single_read"].read(plan["authorization_id"])
    assert stored["stored_state"] == "CLAIMED" and stored["status"] == "UNCERTAIN"
    assert execute(context, plan)["status"] == "UNCERTAIN" and context["calls"] == 1


def test_expired_lease_does_not_retry_received_response(context):
    context["handler"] = lambda: context["clock"].__setitem__(
        0, context["clock"][0] + timedelta(seconds=601)
    )
    plan = prepare(context)
    approve(context, plan)
    result = execute(context, plan)
    assert result["status"] == "UNCERTAIN" and result["error_code"] == "LEASE_LOST"
    assert execute(context, plan) == result and context["calls"] == 1


def test_concurrent_execution_of_same_authorization_has_one_post(context):
    plan = prepare(context)
    approve(context, plan)

    def call():
        try:
            return execute(context, plan)["status"]
        except ValueError as error:
            assert "execução" in str(error)
            return "BUSY"

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: call(), range(4)))
    assert set(statuses) <= {"BUSY", "UNCERTAIN", "SUCCEEDED"}
    assert context["calls"] == 1 and collection.count(context, ShopeeCollectionRun) == 1
    assert collection.count(context, IntegrationEvent) == 4


def test_global_collection_lease_fences_other_authorizations(context):
    first, second = prepare(context), prepare(context)
    approve(context, first)
    approve(context, second)
    entered, release = Event(), Event()

    def hold():
        entered.set()
        assert release.wait(10)

    context["handler"] = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(execute, context, first)
        assert entered.wait(10)
        try:
            with pytest.raises(ValueError, match="execução"):
                execute(context, second)
        finally:
            release.set()
        assert active.result()["status"] == "SUCCEEDED"
    assert (
        context["calls"] == 1
        and not context["single_read"].read(second["authorization_id"])["consumed"]
    )


def test_no_historical_collection_authorization_is_reused(context):
    old = collection.execute(context)
    with pytest.raises(ValueError, match="históricas"):
        context["single_read"].prepare(
            observation.specification(context, request_id=old["id"]), offline=True
        )
    assert collection.count(context, AppSetting) == 0
    assert context["service"].read(old["id"]) == old


def test_preferences_and_global_flags_are_not_authorization(context):
    with context["factory"]() as session:
        DiscoveryOperations().save(session, {"max_queries_per_period": 0}, 0)
        session.commit()
    plan = prepare(context)
    approve(context, plan)
    with pytest.raises(ValueError, match="Orçamento"):
        execute(context, plan)
    assert context["calls"] == 0
    context["settings"].shopee_enabled = True
    with pytest.raises(ValueError, match="flags"):
        execute(context, plan)


def test_no_commercial_tables_are_written(context):
    allowed = {"app_settings", "integration_events", "shopee_collection_runs", "monitoring_leases"}

    def guard(connection, cursor, statement, parameters, context, many):
        words = statement.strip().lower().split()
        if words and words[0] in {"insert", "update", "delete"}:
            table = words[1] if words[0] == "update" else words[2]
            assert table.strip('"') in allowed, table

    event.listen(context["engine"], "before_cursor_execute", guard)
    try:
        plan = prepare(context)
        approve(context, plan)
        assert execute(context, plan)["status"] == "SUCCEEDED"
    finally:
        event.remove(context["engine"], "before_cursor_execute", guard)
    assert observation.histories(context) == []
    observation.assert_no_commerce(context)


def test_real_registry_is_lazy_and_does_not_change_global_settings(context):
    registry = context["single_read"]._real_registry()
    try:
        connector = registry.get(Platform.SHOPEE)
        assert connector.transport.max_retries == connector.transport.cache_seconds == 0
        assert not connector.short_links_enabled and connector.enabled
        assert not context["settings"].shopee_enabled
        assert context["calls"] == 0
    finally:
        registry.close()


def test_real_execution_branch_uses_registry_but_all_http_is_mocked(context, monkeypatch):
    import app.services.shopee_single_read as module

    original_registry = module.ConnectorRegistry
    registries = []

    class MockRegistry(original_registry):
        def get(self, platform):
            registries.append(self.settings)
            return context["connector"]

        def is_blocked(self, platform):
            return False

        def close(self):
            return None

    monkeypatch.setattr(module, "ConnectorRegistry", MockRegistry)
    # Explicit test harness only: production rejects this mode in *_test databases.
    monkeypatch.setattr(context["single_read"], "_mode", lambda offline: "REAL_SINGLE_READ")
    plan = prepare(context)
    approve(context, plan)
    result = context["single_read"].execute(plan["authorization_id"], confirmation(plan))
    assert result["status"] == "SUCCEEDED" and result["quote"]["mode"] == "REAL_SINGLE_READ"
    assert context["calls"] == 1 and len(registries) == 1
    assert registries[0].shopee_enabled and not registries[0].shopee_short_links_enabled
    assert registries[0].integration_max_retries == registries[0].integration_cache_seconds == 0
    assert not context["settings"].shopee_enabled
    observation.assert_no_commerce(context)


def test_quote_is_ephemeral_and_not_stored_in_any_audit_document(context):
    plan = prepare(context)
    approve(context, plan)
    result = execute(context, plan)
    assert result["quote"]["price"] == "99.90"
    assert "quote" not in context["single_read"].read(plan["authorization_id"])
    with context["factory"]() as session:
        row = session.scalar(select(AppSetting))
        run = session.get(ShopeeCollectionRun, plan["authorization_id"])
        for value in (row.value, run.result):
            serialized = json.dumps(value)
            assert '"price"' not in serialized and '"price_min"' not in serialized
            assert '"advertised_discount_percent"' not in serialized
            assert "99.90" not in serialized
    assert observation.histories(context) == []


def test_post_claim_scope_change_blocks_http_without_reissuing(context):
    plan = prepare(context)
    approve(context, plan)

    def change_scope(request):
        with context["factory"]() as session:
            product = session.get(Product, context["product_id"])
            product.original_url = "https://shopee.com.br/changed-i.101.202"
            session.commit()

    context["http"].event_hooks["request"].insert(0, change_scope)
    result = execute(context, plan)
    assert result["status"] == "UNCERTAIN" and result["consumed"]
    assert context["calls"] == 0


def test_real_authorizations_cannot_be_created_from_test_database(context):
    with pytest.raises(ValueError, match="modo mockado"):
        context["single_read"].prepare(observation.specification(context))
    assert collection.count(context, AppSetting) == 0


def test_admin_routes_require_auth_and_do_not_enable_test_database_real_calls(client):
    route = "/api/v1/shopee-collection/identity-reads"
    data = {
        "request_id": str(uuid4()),
        "actor": "responsavel",
        "product_id": 2,
        "shop_id": "1904573791",
        "item_id": "23199869203",
    }
    assert client.post(route, json=data).status_code == 401
    assert client.post(route + "/plan", json=data, headers=api.HEADERS).status_code == 409
    assert client.post(route, json=data, headers=api.HEADERS).status_code == 409
    assert client.get(route + "/" + data["request_id"], headers=api.HEADERS).status_code == 404
    for action in ("approve", "execute"):
        assert (
            client.post(
                route + "/" + data["request_id"] + "/" + action, json={}, headers=api.HEADERS
            ).status_code
            == 422
        )
    assert (
        client.post(route, json={**data, "offline": True}, headers=api.HEADERS).status_code == 422
    )
