import pytest
import test_postgres_auto_pilot_real as pg
import test_single_imported_publication as contracts

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("mismatch", [False, True])
def test_postgres_one_pinned_message_without_queries(context, mismatch):
    contracts.test_single_imported_message_with_zero_queries(context, mismatch)
