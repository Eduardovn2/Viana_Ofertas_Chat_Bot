from concurrent.futures import ThreadPoolExecutor

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_quote_ingestion as contracts

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = contracts.setup(collection.setup(engine, url.render_as_string(hide_password=False)))
    yield ctx
    ctx["transport"].close()


def test_postgres_historical_idempotence(context):
    contracts.test_initial_official_provenance_and_idempotence(context)


def test_postgres_manual_separation(context):
    contracts.test_manual_baseline_and_documentary_records_preserved(context)


def test_postgres_ingestion_concurrency(context):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: contracts.ingest(context), range(4)))
    assert len({r.id for r in rows}) == 1


def test_postgres_operational_gate(context):
    contracts.test_operational_ingestion_unconditionally_blocked(context)
