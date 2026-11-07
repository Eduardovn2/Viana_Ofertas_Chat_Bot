"""Same dispatcher contract against migrated, UUID-isolated PostgreSQL schemas."""

import inspect

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_multistore as multistore
import test_shopee_multistore_dispatch as contracts

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = multistore.setup(collection.setup(engine, url.render_as_string(hide_password=False)))
    yield ctx
    ctx["transport"].close()


@pytest.mark.parametrize(
    "name",
    [
        "prepare_is_sealed_idempotent_and_never_calls",
        "success_uses_durable_reservation_and_existing_comparator",
        "rotated_secret_invalidates_approval",
        "no_approval_and_outside_window_cannot_consume",
        "uuid_cannot_be_reused_after_consumption",
        "uuid_cannot_reuse_existing_authorization",
        "request_hook_prevents_second_physical_call",
        "sanitized_result_excludes_credentials_and_urls",
        "wire_payload_change_is_blocked_before_http",
        "reduced_result_budget_blocks_dispatch",
    ],
)
def test_postgres_dispatch(context, monkeypatch, name):
    function = getattr(contracts, "test_" + name)
    if "monkeypatch" in inspect.signature(function).parameters:
        function(context, monkeypatch)
    else:
        function(context)


@pytest.mark.parametrize("size", [0, 1])
def test_postgres_insufficient(context, size):
    contracts.test_insufficient_results_never_trigger_complementary_search(context, size)


@pytest.mark.parametrize(
    "failure,status", [(401, "FAILED"), (403, "FAILED"), ("timeout", "UNCERTAIN")]
)
def test_postgres_failures(context, failure, status):
    contracts.test_failure_is_consumed_and_not_retried(context, failure, status)


@pytest.mark.parametrize(
    "part,field,value",
    [
        ("request", "keyword", "Outro produto"),
        ("request", "limit", 4),
        ("scope", "payload_sha256", "0" * 64),
    ],
)
def test_postgres_scope_tampering(context, part, field, value):
    contracts.test_scope_tampering_before_claim_prevents_http(context, part, field, value)
