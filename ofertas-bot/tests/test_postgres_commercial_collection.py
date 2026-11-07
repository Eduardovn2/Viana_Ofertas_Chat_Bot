"""Collector transactions on isolated PostgreSQL, no production records."""

import pytest
import test_commercial_collection as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


def test_postgres_mixed_candidates(context, tmp_path):
    cases.test_blocked_source_does_not_stop_valid_rows(context, tmp_path)


def test_postgres_restart_budget(context, tmp_path):
    cases.test_restart_and_document_budget_are_persisted(context, tmp_path)
