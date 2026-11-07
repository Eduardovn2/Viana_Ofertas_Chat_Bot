"""Real PostgreSQL isolation, synthetic Shopee responses and zero live publishers."""

from pathlib import Path

import pytest
import test_affiliate_pipeline as contracts
import test_postgres_categories as pg
import test_shopee_discovery as discovery
from alembic.config import Config

from alembic import command

pytestmark = pytest.mark.postgres
categories_postgres = pg.categories_postgres
no_network = contracts.no_network


@pytest.fixture
def pipeline_context(categories_postgres):
    engine, url = categories_postgres
    ctx = discovery.setup(engine, url.render_as_string(hide_password=False))
    yield ctx
    ctx["transport"].close()
    if "affiliate_http" in ctx:
        ctx["affiliate_http"].close()


def test_postgres_a_preapproved_preview(pipeline_context, monkeypatch):
    contracts.test_a_preapproved_link_prepares_only_simulated_message(pipeline_context, monkeypatch)


def test_postgres_b_initial_history(pipeline_context, monkeypatch):
    contracts.test_b_no_comparable_history_never_calls_generator(pipeline_context, monkeypatch)


def test_postgres_c_identity_is_not_attribution(pipeline_context, monkeypatch, caplog):
    contracts.test_c_matching_identity_preserves_links_but_blocks_account_association(
        pipeline_context, monkeypatch, caplog
    )


@pytest.mark.parametrize(
    "failure,status",
    [("timeout", "UNCERTAIN"), (503, "UNCERTAIN"), (403, "FAILED"), ("graphql", "FAILED")],
)
def test_postgres_d_no_retries(pipeline_context, monkeypatch, failure, status):
    contracts.test_d_uncertain_or_refused_result_is_consumed_without_retry(
        pipeline_context, monkeypatch, failure, status
    )


@pytest.mark.parametrize("suffix", ["SYNTHETIC-i.7001.2", "SYNTHETIC-i.7001.1?variation_id=9"])
def test_postgres_e_product_or_variant_mismatch(pipeline_context, monkeypatch, suffix):
    contracts.test_e_different_product_or_returned_variant_is_rejected(
        pipeline_context, monkeypatch, suffix
    )


def test_postgres_g_concurrent_reservation(pipeline_context, monkeypatch):
    contracts.test_g_concurrent_attempts_generate_once_and_cannot_approve_or_send(
        pipeline_context, monkeypatch
    )


def test_postgres_h_exceptional_window(pipeline_context, monkeypatch):
    contracts.test_h_exceptional_price_outside_window_does_not_bypass_safety(
        pipeline_context, monkeypatch
    )


def test_postgres_pending_simulation_only(pipeline_context, monkeypatch):
    contracts.test_mock_attribution_confirmation_still_only_creates_pending_simulation(
        pipeline_context, monkeypatch
    )


def test_postgres_persistence_failure_consumes_attempt(pipeline_context, monkeypatch):
    contracts.test_persistence_failure_is_uncertain_and_never_repeats(pipeline_context, monkeypatch)


def test_postgres_alembic_current_and_check(categories_postgres):
    # The shared fixture migrates only its UUID schema in ofertas_bot_test.
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.current(config)
    command.check(config)
