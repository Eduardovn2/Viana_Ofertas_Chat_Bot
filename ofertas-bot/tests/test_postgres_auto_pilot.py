"""AUTO_PILOT persistence/concurrency in migrated isolated PostgreSQL schemas."""

import pytest
import test_auto_pilot as contracts
import test_postgres_categories as pg
import test_shopee_collection as collection

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = contracts.setup(collection.setup(engine, url.render_as_string(hide_password=False)))
    yield ctx
    ctx["telegram_client"].close()
    ctx["transport"].close()


@pytest.mark.parametrize(
    "name",
    [
        "batch_five_without_fixed_gap_and_rolling_window",
        "message_budget_and_idempotent_dispatch",
        "twenty_messages_end_the_default_pilot",
        "no_verified_offers_means_zero_publications",
        "uncertain_never_retries_and_keeps_outbox_slot",
        "deadline_and_admin_stop_are_terminal",
        "deadline_without_stop_prevents_queries_and_sends",
        "query_timeout_consumes_budget_no_retry",
        "cross_pilot_listing_cooldown",
    ],
)
def test_postgres_auto_pilot(context, name):
    getattr(contracts, "test_" + name)(context)


def test_postgres_concurrent_worker(context, monkeypatch):
    contracts.test_concurrent_batch_has_one_sender(context, monkeypatch)


@pytest.mark.parametrize("change", ["variant", "price", "link", "document", "document_changed"])
def test_postgres_changed_evidence(context, change):
    contracts.test_evidence_changed_in_queue_is_not_auto_approved(context, change)
