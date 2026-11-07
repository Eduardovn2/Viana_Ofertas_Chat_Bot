"""Real adapter with mocked transports and isolated migrated PostgreSQL schemas."""

import pytest
import test_auto_pilot_real as contracts
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
    ctx["pilot"].close()
    ctx["telegram_client"].close()
    ctx["transport"].close()


@pytest.mark.parametrize(
    "name",
    [
        "prepare_never_approves_activates_or_calls_http",
        "signed_grant_reuses_queue_and_outbox_without_per_message_approval",
        "timeout_does_not_retry_and_stop_restores_destination",
        "deadline_worker_exits_without_http_and_restores_destination",
        "http_without_permit_or_changed_payload_is_blocked",
        "explicit_confirmation_and_hash_required",
    ],
)
def test_postgres_real_adapter(context, name):
    getattr(contracts, "test_" + name)(context)


@pytest.mark.parametrize("tamper", ["scope", "scope_rehash", "signature", "approval"])
def test_postgres_signed_scope(context, tamper):
    contracts.test_signature_scope_and_approval_tampering_blocks_http(context, tamper)


def test_postgres_real_twenty_cap(context, monkeypatch):
    contracts.test_real_twenty_cap_and_rolling_window_reuse_controller(context, monkeypatch)


def test_postgres_final_http_stop(context, monkeypatch):
    contracts.test_stop_at_last_http_boundary_blocks_transmission(context, monkeypatch)


def test_postgres_final_http_expiry(context, monkeypatch):
    contracts.test_expired_window_at_transport_boundary_blocks_http(context, monkeypatch)
