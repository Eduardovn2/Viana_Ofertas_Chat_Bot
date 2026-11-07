"""Runtime preferences and effective decisions; all product evidence is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from threading import Event

import pytest
import test_api as api
import test_shopee_discovery as discovery
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select

from app.jobs.runtime import MonitoringRuntime
from app.models import (
    AppSetting,
    Category,
    CategoryMode,
    DestinationChannel,
    Product,
    PublicationDestination,
    PublicationQueueItem,
    ShopeeDiscoveryRun,
    SimulationPolicy,
)
from app.services.discovery_operations import DiscoveryOperations

context = discovery.context
application = api.application
client = api.client
no_network = discovery.no_network


def update(ctx, changes, destination_id=None, revision=None):
    service = ctx["discovery"]
    if revision is None:
        state = service.preferences(destination_id)
        revision = (
            state["global_revision"] if destination_id is None else state["destination_revision"]
        )
    return service.update_preferences(changes, revision, "offline administrator", destination_id)


def destination(ctx, external_id="offline-one"):
    with ctx["factory"]() as session:
        row = PublicationDestination(
            name="[FICTICIO] preferences",
            channel=DestinationChannel.SIMULATED,
            external_id=external_id,
            enabled=True,
            manual_approval_required=True,
            category_mode=CategoryMode.ALL,
        )
        session.add(row)
        session.commit()
        return row.id


def effective(ctx, destination_id=None):
    with ctx["factory"]() as session:
        return ctx["discovery"].operations.load(session, destination_id)[0]


def test_read_defaults_does_not_write_or_grant_authorization(context):
    result = context["discovery"].preferences()
    assert result["preferences"]["timezone"] == "America/Sao_Paulo"
    assert result["global_revision"] == 0 and result["authorization_granted"] is False
    with context["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(AppSetting)) == 0
    assert context["calls"] == 0


def test_persist_revision_audit_and_reload_independent_service(context):
    result = update(
        context,
        {
            "working_days": [0, 2],
            "windows": [{"start": "08:15", "end": "17:45"}],
            "discovery_interval_seconds": 17,
            "monitor_interval_seconds": 23,
        },
    )
    assert result["global_revision"] == 1 and result["audit_id"]
    with context["factory"]() as session:
        value, revision = DiscoveryOperations().load(session)
        assert value.discovery_interval_seconds == 17 and value.monitor_interval_seconds == 23
        assert revision["global_revision"] == 1
        audit = session.get(ShopeeDiscoveryRun, result["audit_id"])
        assert audit.specification["actor"] == "offline administrator"
        assert audit.result["authorization_granted"] is False
        assert audit.result["after"]["revision"] == 1
    assert context["calls"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"working_days": [7]},
        {"working_days": [True]},
        {"working_days": [1, 1]},
        {"timezone": "fake/invalid"},
        {"timezone": "../etc/passwd"},
        {"windows": [{"start": "25:00", "end": "10:00"}]},
        {"windows": [{"start": "08:00", "end": "08:00"}]},
        {"windows": [{"start": "24:00", "end": "00:00"}]},
        {"discovery_interval_seconds": 0},
        {"monitor_interval_seconds": "1"},
        {"normal_min_discount": "-1"},
        {"normal_min_discount": "NaN"},
        {"normal_min_discount": "101"},
        {"normal_min_discount": "1.001"},
        {"exceptional_min_discount": "-1"},
        {"exceptional_enabled": "true"},
        {"normal_min_discount": "40", "exceptional_min_discount": "30"},
        {"normal_min_score": "Infinity"},
        {"max_queries_per_period": -1},
        {"max_requests_per_run": 3},
        {"max_predictions_per_period": True},
        {"priority_category_ids": [True]},
        {"priority_category_ids": [999999]},
        {"priority_product_ids": [999999]},
        {"backoff_initial_seconds": 200, "backoff_max_seconds": 100},
        {"telegram_publisher_enabled": True},
        {"shopee_enabled": True},
        {"real_sends_authorized": True},
        {"automatic_approval": True},
    ],
)
def test_invalid_preferences_rollback_without_audit(context, changes):
    with pytest.raises(ValueError):
        update(context, changes)
    assert context["discovery"].preferences()["global_revision"] == 0
    with context["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(AppSetting)) == 0
        assert session.scalar(select(func.count()).select_from(ShopeeDiscoveryRun)) == 0
    assert context["calls"] == 0


@pytest.mark.parametrize(
    "instant,opened",
    [
        ("2026-10-19T10:59:00+00:00", False),
        ("2026-10-19T11:00:00+00:00", True),
        ("2026-10-19T19:59:59+00:00", True),
        ("2026-10-19T20:00:00+00:00", False),
        ("2026-10-20T12:00:00+00:00", False),
    ],
)
def test_timezone_days_and_exclusive_boundaries(context, instant, opened):
    update(context, {"working_days": [0], "windows": [{"start": "08:00", "end": "17:00"}]})
    assert effective(context).open_at(datetime.fromisoformat(instant)) is opened


@pytest.mark.parametrize(
    "instant,opened",
    [
        ("2026-10-20T00:59:00+00:00", False),
        ("2026-10-20T01:00:00+00:00", True),
        ("2026-10-20T04:59:00+00:00", True),
        ("2026-10-20T05:00:00+00:00", False),
        ("2026-10-21T01:00:00+00:00", False),
    ],
)
def test_overnight_window_belongs_to_opening_day(context, instant, opened):
    update(context, {"working_days": [0], "windows": [{"start": "22:00", "end": "02:00"}]})
    assert effective(context).open_at(datetime.fromisoformat(instant)) is opened


@pytest.mark.parametrize("fold", [0, 1])
def test_dst_repeated_hour_uses_local_schedule_and_aware_instant(context, fold):
    from zoneinfo import ZoneInfo

    update(
        context,
        {
            "timezone": "America/New_York",
            "working_days": [6],
            "windows": [{"start": "01:00", "end": "02:00"}],
        },
    )
    instant = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=fold)
    assert effective(context).open_at(instant)


@pytest.mark.parametrize("changes", [{"working_days": []}, {"windows": []}])
def test_closed_schedule_records_zero_request(context, changes):
    discovery.configure(context)
    update(context, changes)
    result = discovery.execute(context)
    assert result["status"] == "SUCCEEDED" and result["requests"] == 0
    assert result["result"]["reason"] == "OUTSIDE_OPERATION_WINDOW"
    assert context["calls"] == 0


def test_frequency_applies_to_preexisting_due_times_without_restart(context):
    discovery.configure(context)
    discovery.execute(context)
    update(context, {"discovery_interval_seconds": 10, "monitor_interval_seconds": 20})
    discovery.advance(context, 11)
    assert discovery.execute(context)["requests"] == 1
    assert discovery.execute(context, "MONITOR")["requests"] == 0
    discovery.advance(context, 21)
    assert discovery.execute(context, "MONITOR")["requests"] == 1
    assert context["calls"] == 3


def test_frequency_change_preserves_backoff(context):
    discovery.configure(context)
    context["failure"] = 429
    assert discovery.execute(context)["status"] == "FAILED"
    update(context, {"discovery_interval_seconds": 1, "monitor_interval_seconds": 1})
    discovery.advance(context, 2)
    assert discovery.execute(context)["requests"] == 0 and context["calls"] == 1


def test_query_limits_and_period_apply_dynamically(context):
    discovery.configure(context)
    update(
        context,
        {
            "discovery_interval_seconds": 1,
            "quota_period_seconds": 60,
            "max_queries_per_period": 1,
            "max_queries_per_category_per_period": 10,
        },
    )
    discovery.execute(context)
    discovery.advance(context, 2)
    assert discovery.execute(context)["requests"] == 0
    discovery.advance(context, 60)
    assert discovery.execute(context)["requests"] == 1
    update(context, {"max_queries_per_period": 0})
    discovery.advance(context, 2)
    assert discovery.execute(context)["requests"] == 0 and context["calls"] == 2


def test_destination_independence_inheritance_and_clear_override(context):
    first, second = destination(context), destination(context, "offline-two")
    update(context, {"normal_min_discount": "5"})
    update(context, {"normal_min_discount": "10", "exceptional_enabled": True}, first)
    assert effective(context, first).normal_min_discount == Decimal("10")
    assert effective(context, second).normal_min_discount == Decimal("5")
    assert effective(context, first).exceptional_enabled is True
    assert effective(context, second).exceptional_enabled is False
    update(context, {"normal_min_discount": None}, first)
    assert effective(context, first).normal_min_discount == Decimal("5")
    with pytest.raises(ValueError):
        update(context, {"discovery_interval_seconds": 1}, first)


def test_global_change_validates_existing_destination_overrides_atomically(context):
    target = destination(context)
    update(context, {"exceptional_min_discount": "15"}, target)
    with pytest.raises(ValueError):
        update(context, {"normal_min_discount": "20"})
    assert context["discovery"].preferences()["global_revision"] == 0
    assert effective(context, target).exceptional_min_discount == Decimal("15")


def test_stale_revision_does_not_overwrite_or_audit(context):
    update(context, {"timezone": "UTC"}, revision=0)
    with pytest.raises(ValueError, match="revision conflict"):
        update(context, {"timezone": "America/Sao_Paulo"}, revision=0)
    assert effective(context).timezone == "UTC"
    with context["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(ShopeeDiscoveryRun)) == 1


def test_concurrent_edits_have_one_winner(context):
    def attempt(value):
        try:
            update(context, {"discovery_interval_seconds": value}, revision=0)
            return "SAVED"
        except ValueError:
            return "CONFLICT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (11, 13)))
    assert sorted(results) == ["CONFLICT", "SAVED"]
    assert context["discovery"].preferences()["global_revision"] == 1


def test_inflight_read_prevents_edit_until_reservation_released(context):
    discovery.configure(context)
    entered, finish = Event(), Event()
    context["handler"] = lambda: (entered.set(), finish.wait(15))
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(discovery.execute, context)
        assert entered.wait(10)
        try:
            with pytest.raises(ValueError, match="busy"):
                update(context, {"working_days": []})
        finally:
            finish.set()
        assert work.result(timeout=20)["status"] == "SUCCEEDED"
    update(context, {"working_days": []})
    assert context["discovery"].preferences()["global_revision"] == 1


def test_priorities_reference_database_and_affect_scoring(context, monkeypatch):
    discovery.verified_fictional_setup(context, monkeypatch)
    with context["factory"]() as session:
        product = session.scalar(select(Product))
        category = session.scalar(select(Category).where(Category.slug == "games"))
        product_id, category_id = product.id, category.id
    before = discovery.execute(context, "EVALUATE")["result"]["candidates"][0]["score"]
    update(context, {"priority_category_ids": [category_id], "priority_product_ids": [product_id]})
    after = discovery.execute(context, "EVALUATE")["result"]["candidates"][0]["score"]
    assert Decimal(after) > Decimal(before)


def test_threshold_and_exceptional_mode_are_dynamic_and_never_approve_real(context, monkeypatch):
    target = discovery.verified_fictional_setup(context, monkeypatch)
    update(context, {"normal_min_discount": "15", "exceptional_min_discount": "20"}, target)
    first = discovery.execute(context, "EVALUATE")["result"]["candidates"][0]
    assert first["decision"] == "BLOCKED" and "OPERATIONAL_DISCOUNT_THRESHOLD" in first["reasons"]
    update(
        context,
        {"normal_min_discount": "5", "exceptional_min_discount": "10", "exceptional_enabled": True},
        target,
    )
    second = discovery.execute(context, "EVALUATE")["result"]["candidates"][0]
    assert (
        second["opportunity_kind"] == "EXCEPTIONAL" and second["decision"] == "MOCK_WOULD_APPROVE"
    )
    assert second["publication_authorized"] is False
    update(context, {"exceptional_enabled": False}, target)
    third = discovery.execute(context, "EVALUATE")["result"]["candidates"][0]
    assert third["opportunity_kind"] == "NORMAL" and third["decision"] == "BLOCKED"
    assert context["calls"] == 2


def test_destination_window_and_existing_simulation_filters_are_respected(context, monkeypatch):
    target = discovery.verified_fictional_setup(context, monkeypatch)
    update(context, {"working_days": []}, target)
    result = discovery.execute(context, "EVALUATE")["result"]["candidates"][0]
    assert "OUTSIDE_DESTINATION_WINDOW" in result["reasons"]
    update(context, {"working_days": None}, target)
    with context["factory"]() as session:
        session.add(SimulationPolicy(destination_id=target, enabled=True, terms=["unrelated"]))
        session.commit()
    result = discovery.execute(context, "EVALUATE")["result"]["candidates"][0]
    assert "DESTINATION_PRODUCT_FILTER" in result["reasons"]
    assert discovery.count(context, PublicationQueueItem) == 0


def test_prediction_limit_zero_blocks_without_any_publication(context, monkeypatch):
    target = discovery.verified_fictional_setup(context, monkeypatch)
    update(context, {"max_predictions_per_destination_per_period": 0}, target)
    result = discovery.execute(context, "EVALUATE")["result"]["candidates"][0]
    assert "DESTINATION_PREDICTION_LIMIT" in result["reasons"]
    assert discovery.count(context, PublicationQueueItem) == 0


def test_scheduler_refresh_preserves_paused_jobs_and_blocked_callbacks(context):
    runtime = MonitoringRuntime(context["factory"], context["settings"])
    runtime._scheduler = BackgroundScheduler(timezone="UTC")
    context["discovery"].prepare_schedule(runtime)
    update(
        context,
        {
            "discovery_interval_seconds": 19,
            "monitor_interval_seconds": 29,
            "evaluation_interval_seconds": 31,
        },
    )
    assert [runtime._prepared_jobs[key]["seconds"] for key in discovery.JOB_IDS] == [19, 29, 31]
    for key in discovery.JOB_IDS:
        job = runtime._scheduler.get_job(key)
        assert job.next_run_time is None and job.func()["status"] == "BLOCKED"
    assert not runtime._scheduler.running and context["calls"] == 0


def test_independent_process_edit_is_observed_on_existing_scheduler_tick(context):
    runtime = MonitoringRuntime(context["factory"], context["settings"])
    context["discovery"].prepare_schedule(runtime)
    other = type(context["discovery"])(
        context["factory"], context["settings"], clock=lambda: context["clock"][0]
    )
    other.update_preferences({"discovery_interval_seconds": 37}, 0, "second backend")
    assert runtime._prepared_jobs[discovery.JOB_IDS[0]]["seconds"] != 37
    runtime._tick()
    assert runtime._prepared_jobs[discovery.JOB_IDS[0]]["seconds"] == 37
    assert context["calls"] == 0 and runtime._scheduler is None


def test_preferences_api_auth_validation_cas_and_safety(application, client):
    route = "/api/v1/shopee-discovery/preferences"
    assert client.get(route).status_code == 401
    initial = client.get(route, headers=api.HEADERS).json()
    assert initial["global_revision"] == 0
    payload = {
        "actor": "owner",
        "expected_revision": 0,
        "changes": {"discovery_interval_seconds": 41},
    }
    result = client.patch(route, headers=api.HEADERS, json=payload)
    assert result.status_code == 200 and result.json()["global_revision"] == 1
    assert client.patch(route, headers=api.HEADERS, json=payload).status_code == 409
    malicious = {
        **payload,
        "expected_revision": 1,
        "changes": {"telegram_real_sends_authorized": True},
    }
    assert client.patch(route, headers=api.HEADERS, json=malicious).status_code == 409
    assert client.get(route, headers=api.HEADERS).json()["authorization_granted"] is False
    assert not application.state.settings.telegram_real_sends_authorized
    assert not application.state.settings.shopee_enabled


def test_runtime_product_priority_applies_before_next_monitor_request(context):
    discovery.configure(context, page_size=2)
    context["pages"][1] = [discovery.node(identity=1), discovery.node(identity=2)]
    discovery.execute(context)
    with context["factory"]() as session:
        ids = list(session.scalars(select(Product.id).order_by(Product.id)))
    update(context, {"priority_product_ids": [ids[1]], "monitor_interval_seconds": 1})
    discovery.advance(context, 2)
    # fetch_product needs the matching single official node in the mock response.
    context["pages"][1] = [discovery.node(identity=2)]
    result = discovery.execute(context, "MONITOR")
    assert result["status"] == "SUCCEEDED", result
    assert "itemId:2" in context["requests"][-1]["query"].replace(" ", "")


def test_dynamic_category_filter_uses_existing_ids(context):
    discovery.configure(context)
    with context["factory"]() as session:
        other = session.scalar(select(Category.id).where(Category.slug == "moda"))
    update(context, {"enabled_category_ids": [other]})
    assert discovery.execute(context)["requests"] == 0
    assert context["calls"] == 0
    update(context, {"enabled_category_ids": []})
    assert discovery.execute(context)["requests"] == 1


def test_dynamic_per_run_limit_preserves_cursor_without_error(context):
    discovery.configure(context, max_pages=2)
    context["has_next"] = True
    update(context, {"max_requests_per_run": 1})
    result = discovery.execute(context)
    assert result["status"] == "SUCCEEDED" and result["requests"] == 1
    assert result["result"]["next_page_preserved"] is True
    assert context["calls"] == 1


def test_destination_score_threshold_and_priorities_remain_independent(context, monkeypatch):
    first = discovery.verified_fictional_setup(context, monkeypatch)
    second = destination(context, "threshold-other")
    discovery.configure(
        context, destinations=[first, second], approval={"min_score": "80", "max_per_category": 5}
    )
    update(context, {"normal_min_score": "1000"}, first)
    with context["factory"]() as session:
        product_id = session.scalar(select(Product.id))
    update(context, {"normal_min_score": "0", "priority_product_ids": [product_id]}, second)
    results = discovery.execute(context, "EVALUATE")["result"]["candidates"]
    assert results[0]["decision"] == "BLOCKED" and "MIN_SCORE" in results[0]["reasons"]
    assert results[1]["decision"] == "MOCK_WOULD_APPROVE"
    assert Decimal(results[1]["score"]) > Decimal(results[0]["score"])


def test_destination_prediction_budgets_do_not_consume_each_other(context, monkeypatch):
    first = discovery.verified_fictional_setup(context, monkeypatch)
    second = destination(context, "budget-independent")
    discovery.configure(
        context, destinations=[first, second], approval={"min_score": "80", "max_per_category": 5}
    )
    update(context, {"max_predictions_per_period": 1}, first)
    update(context, {"max_predictions_per_period": 1}, second)
    rows = discovery.execute(context, "EVALUATE")["result"]["candidates"]
    assert [r["decision"] for r in rows] == ["MOCK_WOULD_APPROVE", "MOCK_WOULD_APPROVE"]
    assert all(not r["publication_authorized"] for r in rows)
    assert discovery.count(context, PublicationQueueItem) == 2
