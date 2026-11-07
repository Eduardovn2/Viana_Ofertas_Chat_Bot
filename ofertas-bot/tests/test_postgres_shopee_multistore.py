import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_multistore as contracts
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
        "documented_ranking_and_no_coupon",
        "no_evidence_no_winner",
        "same_shop_deduplication",
        "tie",
        "ratings_absent_and_semantics",
        "range_is_not_final_price",
        "quote_bound_evidence",
        "preparation_idempotent_and_blocked_execution",
        "default_payload_preserved",
        "unknown_variant_not_verified",
    ],
)
def test_postgres_multistore(context, name):
    getattr(contracts, "test_" + name)(context)


def test_postgres_multistore_alembic(context):
    config = Config(str(pg.PROJECT_ROOT / "alembic.ini"))
    command.current(config)
    command.check(config)
