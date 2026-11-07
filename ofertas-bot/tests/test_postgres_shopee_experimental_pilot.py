import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_experimental_pilot as contracts

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
        "prepared_not_approved",
        "window",
        "explicit_acknowledgement",
        "two_cycles_only",
        "hash_and_mode_cannot_change",
        "window_expired",
        "spacing_after_delayed_response",
        "account_change",
        "concurrent_pilot_cycles",
        "no_mode_promotion",
    ],
)
def test_postgres_pilot(context, name):
    getattr(contracts, "test_" + name)(context)


@pytest.mark.parametrize(
    "failure,status", [(403, "FAILED"), (429, "FAILED"), ("timeout", "UNCERTAIN")]
)
def test_postgres_pilot_pause(context, failure, status):
    contracts.test_pause(context, failure, status)


@pytest.mark.parametrize("change", [{"max_requests": 3}, {"min_interval_seconds": 21599}])
def test_postgres_pilot_bounds(context, change):
    contracts.test_bounds(context, change)
