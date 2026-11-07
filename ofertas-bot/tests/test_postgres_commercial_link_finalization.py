"""Production-finalization contracts against isolated PostgreSQL + MockTransport."""

import pytest
import test_commercial_link_finalization as contracts
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "name",
    [
        "future_link_receipt_import_and_existing_pilot_queue",
        "timeout_consumes_reservation_and_never_retries",
        "account_invalidated_after_generation_blocks_offer_consumption",
        "concurrent_execute_cannot_cancel_owner_or_send_twice",
    ],
)
def test_postgres_finalization(context, name):
    getattr(contracts, "test_" + name)(context)


def test_postgres_listing_only_link_preflight(context):
    contracts.test_listing_only_link_preflight_never_attests_price_or_creates_offer(context, False)
