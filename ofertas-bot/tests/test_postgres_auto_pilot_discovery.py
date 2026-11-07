import pytest
import test_auto_pilot_discovery as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "name",
    [
        "discovery_without_manual_offers_persists_and_classifies_before_completion",
        "query_exhaustion_drains_verified_queue_without_another_query",
        "late_response_is_preserved_without_publication",
        "documented_statistics_deduplicate_stores_and_keep_decimal",
    ],
)
def test_postgres_discovery(context, monkeypatch, name):
    function = getattr(cases, "test_" + name)
    if name in {
        "query_exhaustion_drains_verified_queue_without_another_query",
        "late_response_is_preserved_without_publication",
    }:
        function(context, monkeypatch)
    else:
        function(context)


@pytest.mark.parametrize("case", ["tie", "different_quantity", "ambiguous_price"])
def test_postgres_statistics(context, case):
    cases.test_group_statistics_require_matching_facts_and_unambiguous_prices(context, case)
