"""Dynamic preferences on real PostgreSQL test schemas, exclusively mocked APIs."""

import pytest
import test_discovery_operations as contracts
import test_postgres_categories as pg
import test_shopee_discovery as discovery

categories_postgres = pg.categories_postgres
no_network = contracts.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = discovery.setup(engine, url.render_as_string(hide_password=False))
    yield ctx
    ctx["transport"].close()


def test_postgres_preferences_persist_and_audit(context):
    contracts.test_persist_revision_audit_and_reload_independent_service(context)


def test_postgres_concurrent_edit_has_one_winner(context):
    contracts.test_concurrent_edits_have_one_winner(context)


def test_postgres_inflight_request_protects_configuration(context):
    contracts.test_inflight_read_prevents_edit_until_reservation_released(context)


def test_postgres_preferences_independent_per_destination(context):
    contracts.test_destination_independence_inheritance_and_clear_override(context)


def test_postgres_global_change_preserves_valid_overrides(context):
    contracts.test_global_change_validates_existing_destination_overrides_atomically(context)


def test_postgres_frequencies_apply_without_restart(context):
    contracts.test_frequency_applies_to_preexisting_due_times_without_restart(context)


def test_postgres_exceptional_mode_remains_mock_only(context, monkeypatch):
    contracts.test_threshold_and_exceptional_mode_are_dynamic_and_never_approve_real(
        context, monkeypatch
    )


def test_postgres_existing_destination_policy_preserved(context, monkeypatch):
    contracts.test_destination_window_and_existing_simulation_filters_are_respected(
        context, monkeypatch
    )


def test_postgres_failed_validation_is_atomic(context):
    contracts.test_invalid_preferences_rollback_without_audit(
        context, {"priority_category_ids": [999999]}
    )
