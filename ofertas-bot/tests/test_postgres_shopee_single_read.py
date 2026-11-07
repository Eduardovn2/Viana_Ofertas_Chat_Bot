"""One-shot claims in real PostgreSQL UUID schemas; all HTTP is synthetic."""

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_single_read as contracts
from alembic.config import Config

from alembic import command

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = contracts.setup(collection.setup(engine, url.render_as_string(hide_password=False)))
    yield ctx
    ctx["transport"].close()


def test_postgres_single_read_plan(context):
    contracts.test_plan_is_readonly_and_binds_exact_payload(context)


def test_postgres_single_read_preparation(context):
    contracts.test_preparation_and_approval_never_call_upstream(context)


def test_postgres_single_read_durable_claim(context):
    contracts.test_claim_is_durable_before_http_and_never_ingests(context)


@pytest.mark.parametrize(
    "failure,status,code,http_status",
    [
        ("timeout", "UNCERTAIN", "TIMEOUT", None),
        (403, "FAILED", "AUTHORIZATION_DENIED", 403),
        (500, "UNCERTAIN", "UPSTREAM_ERROR", 500),
    ],
)
def test_postgres_single_read_failure(context, failure, status, code, http_status):
    contracts.test_one_consumed_attempt_on_failure_without_retry(
        context, failure, status, code, http_status
    )


def test_postgres_single_read_concurrent_same_authorization(context):
    contracts.test_concurrent_execution_of_same_authorization_has_one_post(context)


def test_postgres_single_read_concurrent_other_authorization(context):
    contracts.test_global_collection_lease_fences_other_authorizations(context)


def test_postgres_single_read_persistence_failure(context, monkeypatch):
    contracts.test_persistence_failure_leaves_consumed_durable_claim(context, monkeypatch)


def test_postgres_single_read_interruption(context):
    contracts.test_interruption_is_uncertain_and_never_reissued(context)


def test_postgres_single_read_expiry(context):
    contracts.test_expired_authorization_cannot_be_extended_or_consumed(context)


def test_postgres_single_read_changed_scope(context, monkeypatch):
    contracts.test_approval_cannot_authorize_a_changed_scope(context, "url", monkeypatch)


def test_postgres_single_read_no_commercial_dml(context):
    contracts.test_no_commercial_tables_are_written(context)


def test_postgres_single_read_no_quote_retention(context):
    contracts.test_quote_is_ephemeral_and_not_stored_in_any_audit_document(context)


def test_postgres_single_read_legacy_uuid(context):
    contracts.test_no_historical_collection_authorization_is_reused(context)


def test_postgres_single_read_alembic(context):
    command.current(Config(str(pg.PROJECT_ROOT / "alembic.ini")))
    command.check(Config(str(pg.PROJECT_ROOT / "alembic.ini")))
