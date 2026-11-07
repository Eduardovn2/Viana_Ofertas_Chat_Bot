"""Read-only reanalysis in a migrated, isolated PostgreSQL test schema."""

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_multistore as multistore
import test_shopee_replay as contracts

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = multistore.setup(collection.setup(engine, url.render_as_string(hide_password=False)))
    yield ctx
    ctx["transport"].close()


def test_postgres_saved_analysis_is_read_only(context, monkeypatch):
    contracts.test_saved_analysis_is_read_only_and_does_not_reuse_consumed_authorization(
        context, monkeypatch
    )


def test_postgres_prepared_search_cannot_be_analyzed(context):
    contracts.test_prepared_or_failed_search_cannot_be_analyzed(context)


def test_postgres_target_does_not_fill_recorded_attributes(context):
    plan = contracts.seed_recorded_result(context)
    result = context["discovery"].analyze_multistore_search(
        plan["authorization_id"], target={"brand": "Brave Bull", "model": "RX-500", "quantity": 1}
    )
    assert result["requested_product"]["model"] == "rx-500"
    assert all(r["attributes"]["model"] is None for r in result["candidates"])
    assert result["proven_equivalences"] == [] and context["calls"] == 0
