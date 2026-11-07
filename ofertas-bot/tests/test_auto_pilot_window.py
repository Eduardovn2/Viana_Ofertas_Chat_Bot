"""Window-driven operation with deterministic clocks and sealed MockTransport."""

import json
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
import test_auto_pilot_worker as workers
import test_official_offer_import as exports
import test_shopee_multistore as multi
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.models import AppSetting, PublicationDestination
from app.services.auto_pilot import PREFIX, AutoPilotPlan
from app.services.shopee_multistore import compare_offers
from app.services.shopee_opportunities import pilot_opportunities

base_context = real.base_context
context = real.context
no_network = real.no_network


def start(ctx, **changes):
    return real.authorized(ctx, operating_policy={}, **changes)


def test_new_plans_default_to_windowed_without_rewriting_legacy_scopes(context):
    ctx = context
    legacy = real.prepared(ctx)
    assert "operating_policy" not in legacy["scope"]["plan"]
    spec = AutoPilotPlan.model_validate(legacy["scope"]["plan"])
    assert spec.operating_policy.on_no_work == "WAIT_UNTIL_END"
    assert (
        ctx["pilot"].report(ctx["pilot_id"])["query_schedule"]["operating_policy"]
        == "LEGACY_SIGNED_SCOPE"
    )


def test_two_hours_distribute_twelve_queries_and_finish_at_deadline(context):
    ctx = context
    start(ctx, max_queries=12)
    beginning = ctx["clock"][0]
    recording = json.loads(
        (Path(__file__).parent / "fixtures/shopee_multistore_real_sanitized.json").read_text(
            encoding="utf-8"
        )
    )
    ctx["nodes"] = [
        multi.node(int(r["shop_id"]), int(r["item_id"]), r["announced_price"], r["name"])
        for r in recording["candidates"]
    ]
    for index in range(12):
        if index:
            ctx["clock"][0] = beginning + timedelta(seconds=index * 600 - 30)
            ctx["pilot"].tick(ctx["pilot_id"])
            assert ctx["calls"] == index
        ctx["clock"][0] = beginning + timedelta(seconds=index * 600)
        result = ctx["pilot"].tick(ctx["pilot_id"])
        assert ctx["calls"] == index + 1 and result["state"] == "ACTIVE_REAL"
    assert result["query_schedule"]["budget_exhausted"]
    assert result["activity_state"] == "WAITING_NO_WORK_QUERY_BUDGET"
    assert len(result["opportunities"]["analysis_queue"]) == 36
    assert all(q["capture"]["comparison"]["candidates"][0]["name"] for q in result["queries"])
    ctx["clock"][0] = beginning + timedelta(seconds=7199)
    assert ctx["pilot"].tick(ctx["pilot_id"])["state"] == "ACTIVE_REAL"
    ctx["clock"][0] = beginning + timedelta(seconds=7200)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["reason"] == "DEADLINE" and ctx["calls"] == 12
    assert not ctx["telegram_calls"]
    with ctx["factory"]() as session:
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled


def test_budget_exhaustion_keeps_processing_and_worker_waits_to_end(context):
    ctx = context
    plan = start(ctx, max_queries=1, expires_at=ctx["clock"][0] + timedelta(seconds=90))
    offline.listings(ctx, [1], verified=False)
    waits = []

    def advance(seconds):
        waits.append(seconds)
        ctx["clock"][0] += timedelta(seconds=seconds)

    result = workers.worker(ctx, plan).run(wait=advance)
    assert result["reason"] == "DEADLINE" and sum(waits) == pytest.approx(90, abs=0.5)
    assert ctx["calls"] == 1 and not ctx["telegram_calls"]
    assert result["opportunities"]["analysis_queue"][0]["publication_state"] == "BLOCKED"


def test_ten_minute_three_query_pilot_does_not_end_in_ninety_seconds(context):
    ctx = context
    start(ctx, duration_seconds=600, max_queries=3)
    beginning = ctx["clock"][0]
    offline.listings(ctx, [1], verified=False)
    for seconds, expected in [(0, 1), (180, 1), (210, 2), (390, 2), (420, 3), (599, 3)]:
        ctx["clock"][0] = beginning + timedelta(seconds=seconds)
        result = ctx["pilot"].tick(ctx["pilot_id"])
        assert ctx["calls"] == expected and result["state"] == "ACTIVE_REAL"
    assert result["activity_state"] == "WAITING_NO_WORK_QUERY_BUDGET"
    ctx["clock"][0] = beginning + timedelta(seconds=600)
    assert ctx["pilot"].tick(ctx["pilot_id"])["reason"] == "DEADLINE"
    assert ctx["calls"] == 3 and not ctx["telegram_calls"]


def test_explicit_no_work_policy_can_end_early(context):
    ctx = context
    real.authorized(ctx, operating_policy={"on_no_work": "STOP_WHEN_DRAINED"}, max_queries=1)
    offline.listings(ctx, [], verified=False)
    assert ctx["pilot"].tick(ctx["pilot_id"])["reason"] == "NO_WORK_AFTER_QUERY_BUDGET"


def test_restart_keeps_budget_deadline_and_next_query_time(context):
    ctx = context
    plan = start(ctx, max_queries=3)
    offline.listings(ctx, [1], verified=False)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    due = result["query_schedule"]["next_query_at"]
    with closing(workers.replacement(ctx)) as replacement:
        ctx["clock"][0] += timedelta(seconds=30)
        result = workers.worker(ctx, plan, replacement).step()
        assert result["query_schedule"]["next_query_at"] == due
        assert ctx["calls"] == 1
        ctx["clock"][0] += timedelta(seconds=2370)
        result = workers.worker(ctx, plan, replacement).step()
        assert ctx["calls"] == 2 and result["counts"]["queries_reserved"] == 2
        assert result["scope_sha256"] == plan["scope_sha256"]
        assert result["effective_expires_at"] == plan["effective_expires_at"]


def test_late_tick_does_not_burst_to_catch_up(context):
    ctx = context
    start(ctx, max_queries=12)
    offline.listings(ctx, [1], verified=False)
    ctx["clock"][0] += timedelta(seconds=1800)
    ctx["pilot"].tick(ctx["pilot_id"])
    assert ctx["calls"] == 1
    ctx["clock"][0] += timedelta(seconds=30)
    ctx["pilot"].tick(ctx["pilot_id"])
    assert ctx["calls"] == 1


def test_pause_resume_preserves_schedule_anchor(context):
    ctx = context
    plan = start(ctx, max_queries=12)
    offline.listings(ctx, [1], verified=False)
    first = ctx["pilot"].tick(ctx["pilot_id"])
    ctx["clock"][0] += timedelta(seconds=300)
    ctx["pilot"].pause(ctx["pilot_id"])
    ctx["clock"][0] += timedelta(seconds=120)
    resumed = ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])
    assert resumed["started_at"] == first["started_at"]
    assert resumed["query_schedule"]["next_query_at"] == first["query_schedule"]["next_query_at"]
    assert resumed["effective_expires_at"] == first["effective_expires_at"]
    ctx["pilot"].tick(ctx["pilot_id"])
    assert ctx["calls"] == 1
    ctx["clock"][0] += timedelta(seconds=30)
    ctx["pilot"].tick(ctx["pilot_id"])
    assert ctx["calls"] == 1


def test_multiple_eligible_offers_publish_throughout_two_hours_without_coupons(context):
    ctx = context
    start(ctx, max_queries=3, max_messages=10)
    beginning = ctx["clock"][0]
    for index, numbers in enumerate(([1, 2], [3, 4], [5, 6])):
        ctx["clock"][0] = beginning + timedelta(seconds=index * 2400)
        offline.listings(ctx, numbers)
        result = ctx["pilot"].tick(ctx["pilot_id"])
        assert result["counts"]["published"] == (index + 1) * 2
        assert result["state"] == "ACTIVE_REAL"
    assert result["activity_state"] == "WAITING_NO_WORK_QUERY_BUDGET"
    assert result["counts"]["automatically_approved"] == 6
    ctx["clock"][0] = beginning + timedelta(seconds=7200)
    assert ctx["pilot"].tick(ctx["pilot_id"])["reason"] == "DEADLINE"
    assert ctx["calls"] == 3 and len(ctx["telegram_calls"]) == 6


def test_confirmed_official_export_can_supply_after_queries_are_exhausted(context):
    ctx = context
    start(ctx, max_queries=1)
    offline.listings(ctx, [], verified=False)
    assert ctx["pilot"].tick(ctx["pilot_id"])["state"] == "ACTIVE_REAL"
    ctx["clock"][0] += timedelta(seconds=30)
    data, row = exports.document(ctx)
    exports.run(ctx, exports.encoded(data, [row]))
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert ctx["calls"] == 1 and result["counts"]["published"] == 1
    assert result["findings"][-1]["reason"] == "EXISTING_VERIFIED_OFFICIAL_EXPORT"


def test_uncertain_query_is_never_retried_after_restart(context):
    ctx = context
    plan = start(ctx, max_queries=3)
    ctx["pilot"]._reserve_query(ctx["pilot_id"])
    with closing(workers.replacement(ctx)) as replacement:
        result = workers.worker(ctx, plan, replacement).step()
        assert result["queries"][0]["state"] == "FAILED_OR_UNCERTAIN"
        assert result["reason"] == "QUERY_UNCERTAINTY_NO_RETRY"
    assert ctx["calls"] == 0 and not ctx["telegram_calls"]


def test_query_spacing_is_signed_and_checked_on_direct_reservation(context):
    ctx = context
    real.authorized(ctx, operating_policy={"query_interval_seconds": 300}, max_queries=3)
    first = ctx["pilot"]._reserve_query(ctx["pilot_id"])
    assert first and ctx["pilot"]._reserve_query(ctx["pilot_id"]) is None
    with ctx["factory"]() as session:
        value = session.scalar(
            select(AppSetting.value).where(AppSetting.key == PREFIX + ctx["pilot_id"])
        )
        assert value["scope_sha256"] == fingerprint(value["scope"])
    assert ctx["calls"] == 0


def test_opportunity_ranking_uses_only_proven_mean_and_median(context):
    ctx = context
    ctx["nodes"] = [multi.node(101, 201, "100.00"), multi.node(102, 202, "200.00")]
    page = multi.page(ctx)
    comparison = compare_offers(page, multi.QUERY, evidence=multi.proofs(page.items))
    comparison = json.loads(json.dumps(comparison, default=str))
    capture = dict(
        capture_id="synthetic", comparison=comparison, comparison_sha256=fingerprint(comparison)
    )
    result = pilot_opportunities([capture], now=ctx["clock"][0])
    row = result["analysis_queue"][0]
    assert row["identity"] == "101:201"
    assert row["opportunity"]["kind"] == "BELOW_PROVEN_GROUP_MEAN_AND_MEDIAN"
    assert row["opportunity"]["statistics"]["mean"] == "150.00"
    assert row["opportunity"]["statistics"]["median"] == "150.00"
    assert not row["publication_authorized"]
