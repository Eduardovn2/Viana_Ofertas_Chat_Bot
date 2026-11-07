"""Restart and concurrent process contracts on isolated migrated PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
import test_auto_pilot_worker as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "case",
    [
        "process_waits_without_approving_or_starting_prepared_plan",
        "even_approved_plan_requires_explicit_start",
        "pause_resume_preserves_hash_and_no_calls_while_paused",
        "restart_after_claim_never_retries_the_message",
        "restart_after_query_reservation_does_not_retry_payload",
        "restart_after_sent_preserves_deduplication",
        "restart_after_http_before_result_persistence_keeps_uncertain_without_resend",
        "restart_after_last_reserved_attempt_closes_uncertainty_and_budget",
        "another_executor_cannot_tick_until_lease_is_released",
        "tick_cadence_survives_restarts_and_multiple_replicas",
        "expired_executor_is_fenced_at_last_http_boundary",
        "graceful_process_exit_does_not_revoke_grant_or_reset_budget",
        "wait_is_bounded_by_scope_deadline",
        "wrong_hash_never_causes_external_execution",
    ],
)
def test_postgres_independent_worker(context, case, monkeypatch):
    function = getattr(cases, "test_" + case)
    if case == "expired_executor_is_fenced_at_last_http_boundary":
        function(context, monkeypatch)
    else:
        function(context)


def test_two_process_controllers_share_one_executor_and_one_send(context, monkeypatch):
    ctx = context
    plan = real.authorized(ctx, max_messages=1)
    offline.listings(ctx, [1])
    entered, released = Event(), Event()
    original = ctx["pilot"]._discover_analysis

    def blocked(*args):
        entered.set()
        assert released.wait(15)
        return original(*args)

    monkeypatch.setattr(ctx["pilot"], "_discover_analysis", blocked)
    with closing(cases.replacement(ctx)) as second, ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(cases.worker(ctx, plan).step)
        try:
            assert entered.wait(15)
            result = cases.worker(ctx, plan, second).step()
            assert result["counts"]["messages_reserved"] == 0 and ctx["calls"] == 0
        finally:
            released.set()
        assert pending.result(timeout=30)["counts"]["published"] == 1
    assert len(ctx["telegram_calls"]) == 1
