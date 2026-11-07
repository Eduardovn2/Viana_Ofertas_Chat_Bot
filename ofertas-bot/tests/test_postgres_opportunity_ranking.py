import pytest
import test_postgres_auto_pilot_real as pg
import test_ranking_integration as cases

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "case",
    [
        "ranking_survives_restart_and_budget_without_publishing_unknown_candidates",
        "proven_comparison_automatically_supplies_price_dimension",
        "radar_reuses_real_same_context_history_without_writing_scores",
    ],
)
def test_postgres_ranking(context, case):
    getattr(cases, "test_" + case)(context)
