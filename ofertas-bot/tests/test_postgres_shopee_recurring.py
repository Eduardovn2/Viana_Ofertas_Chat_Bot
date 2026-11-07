"""Reuse the recurring contracts on migrated UUID schemas in ofertas_bot_test."""

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_recurring as contracts
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


@pytest.mark.parametrize(
    "name",
    [
        "missing_unapproved_and_expired",
        "budget_and_interval",
        "interval_dynamic_without_new_permission",
        "replay",
        "concurrent_cycles",
        "claim_precedes_http",
        "changed_account_blocks",
        "real_mode_blocked",
        "no_quota_evidence_no_approval",
        "scheduler_prepared_paused",
        "manual_baseline_not_compared",
    ],
)
def test_postgres_recurring_contract(context, name):
    getattr(contracts, "test_" + name)(context)


@pytest.mark.parametrize(
    "price,direction", [("99.90", "UNCHANGED"), ("100.00", "RISE"), ("89.91", "DROP")]
)
def test_postgres_recurring_series(context, price, direction):
    contracts.test_series(context, price, direction)


@pytest.mark.parametrize(
    "failure,status",
    [
        ("timeout", "UNCERTAIN"),
        (403, "FAILED"),
        (429, "FAILED"),
        ("empty", "FAILED"),
        ("identity", "FAILED"),
    ],
)
def test_postgres_recurring_failure(context, failure, status):
    contracts.test_failures_pause_without_retry(context, failure, status)


def test_postgres_recurring_interruption(context, monkeypatch):
    contracts.test_interrupted_claim_cannot_resume(context, monkeypatch)


def test_postgres_recurring_persistence_failure(context, monkeypatch):
    contracts.test_persistence_failure_does_not_repeat_http(context, monkeypatch)


def test_postgres_recurring_alembic(context):
    config = Config(str(pg.PROJECT_ROOT / "alembic.ini"))
    command.current(config)
    command.check(config)
