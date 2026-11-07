"""Independent process runtime over the real adapter, only isolated DB/MockTransport."""

from contextlib import closing
from datetime import timedelta
from threading import Event

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
from sqlalchemy import select

from app.core.config import Settings
from app.deployment import create_control_app
from app.models import PublicationDestination, TelegramPublication, TelegramPublicationStatus
from app.services.auto_pilot_real import RealAutoPilotService
from app.services.auto_pilot_worker import AutoPilotWorker, check_health, worker_status
from app.services.leases import acquire_lease, release_lease

base_context = real.base_context
context = real.context
no_network = real.no_network
application = real.application
client = real.client


def replacement(ctx):
    return RealAutoPilotService(
        ctx["factory"],
        ctx["settings"],
        isolated_testing=True,
        shopee_transport=ctx["http"]._transport,
        telegram_transport=ctx["telegram_client"]._transport,
        clock=lambda: ctx["clock"][0],
    )


def worker(ctx, plan, controller=None, health_file=None):
    return AutoPilotWorker(
        controller or ctx["pilot"], ctx["pilot_id"], plan["scope_sha256"], health_file=health_file
    )


def test_process_waits_without_approving_or_starting_prepared_plan(context):
    ctx = context
    plan = real.prepared(ctx)
    event = Event()
    cycles = []

    def wait(seconds):
        assert 0 < seconds <= 30
        cycles.append(seconds)
        if len(cycles) == 2:
            event.set()

    worker(ctx, plan).run(event, wait=wait)
    report = ctx["pilot"].report(ctx["pilot_id"])
    assert report["state"] == "PREPARED" and report["approval"] is None
    assert ctx["calls"] == 0 and ctx["telegram_calls"] == []


def test_even_approved_plan_requires_explicit_start(context):
    ctx = context
    plan = real.prepared(ctx)
    ctx["pilot"].approve(ctx["pilot_id"], real.approval(plan))
    assert worker(ctx, plan).step()["state"] == "APPROVED"
    with ctx["factory"]() as session:
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled
    assert ctx["calls"] == 0


def test_pause_resume_preserves_hash_and_no_calls_while_paused(context):
    ctx = context
    plan = real.authorized(ctx, max_messages=1)
    assert ctx["pilot"].pause(ctx["pilot_id"])["state"] == "PAUSED"
    assert worker(ctx, plan).step()["state"] == "PAUSED"
    assert not ctx["calls"] and not ctx["telegram_calls"]
    ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])
    offline.listings(ctx, [1])
    report = worker(ctx, plan).step()
    assert report["scope_sha256"] == plan["scope_sha256"]
    assert report["counts"]["published"] == 1
    assert report["state"] == "COMPLETED"


def test_restart_after_claim_never_retries_the_message(context):
    ctx = context
    plan = real.authorized(ctx)
    offline.listings(ctx, [1])
    ctx["pilot"].discover_once(ctx["pilot_id"])
    queued = ctx["pilot"].report(ctx["pilot_id"])["queue"][0]
    ctx["pilot"]._reserve_message(ctx["pilot_id"], queued["id"])
    with closing(replacement(ctx)) as controller:
        report = worker(ctx, plan, controller).step()
    assert report["reservations"][0]["state"] == "UNCERTAIN"
    assert report["queue"][0]["state"] == "UNCERTAIN"
    assert not ctx["telegram_calls"]


def test_restart_after_query_reservation_does_not_retry_payload(context):
    ctx = context
    plan = real.authorized(ctx)
    ctx["pilot"]._reserve_query(ctx["pilot_id"])
    with closing(replacement(ctx)) as controller:
        report = worker(ctx, plan, controller).step()
    assert report["state"] == "COMPLETED" and report["reason"] == "QUERY_UNCERTAINTY_NO_RETRY"
    assert report["queries"][0]["error"] == "PROCESS_INTERRUPTED" and not ctx["calls"]


def test_restart_after_sent_preserves_deduplication(context):
    ctx = context
    plan = real.authorized(ctx)
    offline.listings(ctx, [1])
    worker(ctx, plan).step()
    assert len(ctx["telegram_calls"]) == 1
    with closing(replacement(ctx)) as controller:
        report = worker(ctx, plan, controller).step()
    assert report["counts"]["published"] == 1 and len(ctx["telegram_calls"]) == 1


def test_restart_after_http_before_result_persistence_keeps_uncertain_without_resend(
    context, max_messages=20
):
    class ProcessCrash(BaseException):
        pass

    ctx = context
    plan = real.authorized(ctx, max_messages=max_messages)
    offline.listings(ctx, [1])
    transport = ctx["telegram_client"]._transport
    original = transport.handler

    def interrupted(request):
        original(request)
        raise ProcessCrash()

    transport.handler = interrupted
    with pytest.raises(ProcessCrash):
        worker(ctx, plan).step()
    transport.handler = original
    with closing(replacement(ctx)) as controller:
        report = worker(ctx, plan, controller).step()
    assert len(ctx["telegram_calls"]) == 1
    assert report["reservations"][0]["state"] == "UNCERTAIN"
    if max_messages == 1:
        assert report["state"] == "COMPLETED" and report["reason"] == "MESSAGE_BUDGET"
    with ctx["factory"]() as session:
        entry = session.scalar(select(TelegramPublication))
        assert entry.status == TelegramPublicationStatus.UNCERTAIN and entry.active_slot == 1


def test_restart_after_last_reserved_attempt_closes_uncertainty_and_budget(context):
    test_restart_after_http_before_result_persistence_keeps_uncertain_without_resend(
        context, max_messages=1
    )


def test_another_executor_cannot_tick_until_lease_is_released(context):
    ctx = context
    plan = real.authorized(ctx)
    name = "auto-pilot-execution:" + ctx["pilot_id"]
    owner = acquire_lease(ctx["factory"], name, 180, lambda: ctx["clock"][0])
    try:
        report = worker(ctx, plan).step()
        assert report["counts"]["queries_reserved"] == 0 and not ctx["calls"]
    finally:
        release_lease(ctx["factory"], name, owner)


def test_tick_cadence_survives_restarts_and_multiple_replicas(context):
    ctx = context
    plan = real.authorized(ctx)
    worker(ctx, plan).step()
    first = ctx["calls"]
    with closing(replacement(ctx)) as second:
        worker(ctx, plan, second).step()
        assert ctx["calls"] == first
        ctx["clock"][0] += timedelta(seconds=30)
        worker(ctx, plan, second).step()
        assert ctx["calls"] == first + 1


def test_expired_executor_is_fenced_at_last_http_boundary(context, monkeypatch):
    ctx = context
    plan = real.authorized(ctx)
    original = ctx["pilot"]._authorize_http

    def expire(request):
        ctx["clock"][0] += timedelta(seconds=181)
        return original(request)

    monkeypatch.setattr(ctx["pilot"], "_authorize_http", expire)
    worker(ctx, plan).step()
    assert not ctx["calls"] and not ctx["telegram_calls"]


def test_graceful_process_exit_does_not_revoke_grant_or_reset_budget(context):
    ctx = context
    plan = real.authorized(ctx)
    event = Event()
    worker(ctx, plan).run(event, wait=lambda seconds: event.set())
    report = ctx["pilot"].report(ctx["pilot_id"])
    assert report["state"] == "ACTIVE_REAL"
    assert report["counts"]["queries_reserved"] == 1
    assert worker_status(ctx["factory"], ctx["pilot_id"], ctx["clock"][0])["phase"] == "EXITED"


@pytest.mark.parametrize("cause", ["DEADLINE", "ADMIN_STOP"])
def test_deadline_and_admin_stop_end_the_process_without_http(context, cause):
    ctx = context
    plan = real.authorized(ctx)
    if cause == "DEADLINE":
        ctx["clock"][0] += timedelta(hours=2)
    else:
        ctx["pilot"].stop(ctx["pilot_id"])
    assert worker(ctx, plan).run()["reason"] == cause
    assert not ctx["calls"] and not ctx["telegram_calls"]
    with ctx["factory"]() as session:
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled


def test_wait_is_bounded_by_scope_deadline(context):
    ctx = context
    plan = real.authorized(ctx, expires_at=ctx["clock"][0] + timedelta(seconds=3))
    waits = []

    def advance(seconds):
        waits.append(seconds)
        ctx["clock"][0] += timedelta(seconds=seconds)

    assert worker(ctx, plan).run(wait=advance)["reason"] == "DEADLINE"
    assert waits == [3] and not ctx["telegram_calls"]


def test_health_file_and_persisted_heartbeat_are_current(context, tmp_path):
    ctx = context
    plan = real.prepared(ctx)
    path = tmp_path / "worker.json"
    worker(ctx, plan, health_file=path).step()
    assert check_health(ctx["factory"], path, ctx["pilot_id"], now=ctx["clock"][0])
    assert worker_status(ctx["factory"], ctx["pilot_id"], ctx["clock"][0])["healthy"]
    assert not check_health(
        ctx["factory"], path, ctx["pilot_id"], now=ctx["clock"][0] + timedelta(seconds=181)
    )


def test_wrong_hash_never_causes_external_execution(context):
    ctx = context
    plan = real.authorized(ctx)
    runner = worker(ctx, plan | {"scope_sha256": "0" * 64})
    with pytest.raises(ValueError):
        runner.run()
    assert not ctx["calls"] and not ctx["telegram_calls"]


def test_pause_approval_and_resume_api_require_admin(context, application, client):
    ctx = context
    real.authorized(ctx)
    application.state.auto_pilot_real = ctx["pilot"]
    path = "/api/v1/auto-pilots-real/" + ctx["pilot_id"]
    assert client.post(path + "/pause").status_code == 401
    assert (
        client.post(path + "/pause", headers=real.collection.api.HEADERS).json()["state"]
        == "PAUSED"
    )
    assert (
        client.get(path + "/worker", headers=real.collection.api.HEADERS).json()["phase"]
        == "NOT_STARTED"
    )


def test_deployment_factory_cannot_start_legacy_scheduler():
    app = create_control_app(
        Settings(_env_file=None, database_url="sqlite://", scheduler_enabled=True)
    )
    try:
        assert app.state.execution_profile == "AUTO_PILOT_CONTROL"
        assert not app.state.background_monitoring_allowed
        assert not app.state.settings.scheduler_enabled
        assert not app.state.settings.telegram_publisher_enabled
    finally:
        app.state.telegram_publisher.close()
        app.state.simulated_pipeline.close()
        app.state.engine.dispose()


def test_deploy_readiness_requires_migration_and_persisted_pause(application):
    from fastapi.testclient import TestClient

    app = create_control_app(application.state.settings)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.get("/ready").status_code == 503
        assert (
            client.post("/api/v1/monitoring/pause", headers=real.collection.api.HEADERS).status_code
            == 200
        )
        assert client.get("/ready").status_code == 200
        assert (
            client.post(
                "/api/v1/monitoring/resume", headers=real.collection.api.HEADERS
            ).status_code
            == 409
        )


def test_private_docker_peer_requires_explicit_exact_address_and_admin_key(application):
    from fastapi.testclient import TestClient

    settings = application.state.settings.model_copy(
        update={"admin_allowed_networks": ("172.18.0.1/32",)}
    )
    app = create_control_app(settings)
    with TestClient(app, base_url="http://localhost", client=("172.18.0.1", 1234)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/products").status_code == 401
    with TestClient(app, base_url="http://localhost", client=("172.18.0.2", 1234)) as client:
        assert client.get("/health", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    for bad in (("0.0.0.0/0",), ("172.18.0.0/24",), ("8.8.8.8/32",)):
        with pytest.raises(ValueError):
            Settings(_env_file=None, admin_allowed_networks=bad)
