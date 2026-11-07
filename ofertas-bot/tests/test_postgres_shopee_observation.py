"""Real PostgreSQL, UUID schemas in ofertas_bot_test, exclusively mock upstream I/O."""

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_observation as contracts
from alembic.config import Config

from alembic import command

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = contracts.prepare(collection.setup(engine, url.render_as_string(hide_password=False)))
    yield ctx
    ctx["transport"].close()


def test_postgres_observation_initial(context):
    contracts.test_identity_query_initial_decimal_context_and_audit(context)


@pytest.mark.parametrize(
    "price,direction,percent",
    [("99.90", "UNCHANGED", "0.00"), ("100.00", "RISE", "0.10"), ("89.91", "DROP", "-10.00")],
)
def test_postgres_observation_series(context, price, direction, percent):
    contracts.test_series_stable_basis_informational_only(context, price, direction, percent)


def test_postgres_observation_replay(context):
    contracts.test_replay_is_idempotent_and_scope_is_immutable(context)


def test_postgres_observation_timeout(context):
    contracts.test_timeout_is_consumed_uncertain_and_audited(context)


def test_postgres_observation_same_scope(context):
    contracts.test_concurrent_same_scope_consumes_one_attempt(context)


def test_postgres_observation_different_scopes(context):
    contracts.test_concurrent_different_scopes_are_fenced(context)


def test_postgres_observation_context(context):
    contracts.test_different_context_never_becomes_drop(context, "payment")


def test_postgres_observation_manual_source(context):
    contracts.test_manual_history_never_supplies_official_baseline(context)


def test_postgres_observation_persistence_failure(context, monkeypatch):
    contracts.test_persistence_failure_rolls_back_and_consumes_attempt(context, monkeypatch)


def test_postgres_observation_range(context):
    contracts.test_range_and_untrusted_extra_conditions_remain_unknown(context)


def test_postgres_observation_alembic_current_check(context):
    command.current(Config(str(pg.PROJECT_ROOT / "alembic.ini")))
    command.check(Config(str(pg.PROJECT_ROOT / "alembic.ini")))
