import pytest
import test_daily_continuous as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


def test_persistent_backlog(context):
    cases.test_thousand_candidates_persist_and_remain_selectable(context)


def test_delivery_pacing_and_next_day(context):
    cases.test_provider_spacing_429_and_fresh_cross_day_revalidation(context)
