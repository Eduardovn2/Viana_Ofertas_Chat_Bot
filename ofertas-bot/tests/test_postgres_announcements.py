import pytest
import test_announcements as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


def test_postgres_announcement_outbox(context):
    cases.test_multiple_announcements_use_existing_outbox_without_stock_claims(context)


def test_postgres_announcement_timeout(context):
    cases.test_uncertain_announcement_never_retries(context)
