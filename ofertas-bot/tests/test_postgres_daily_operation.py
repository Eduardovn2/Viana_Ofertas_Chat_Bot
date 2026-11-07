import pytest
import test_daily_operation as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


def test_postgres_daily_resume_and_stop(context, monkeypatch):
    cases.test_daily_window_restart_pause_stop_and_next_day(context, monkeypatch)


def test_postgres_daily_link_budget(context):
    cases.test_daily_supply_continues_after_uncertain_link(context)


def test_postgres_daily_100_limit(context, monkeypatch):
    cases.test_daily_100_limit_preserves_discovery_and_resets_only_next_day(context, monkeypatch)
