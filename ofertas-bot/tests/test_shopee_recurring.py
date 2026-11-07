"""Recurring scopes exercise synthetic responses only; never an operational grant."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
import test_shopee_collection as collection
import test_shopee_observation as observation
import test_shopee_single_read as single
from pydantic import ValidationError
from sqlalchemy import select

from app.jobs.runtime import MonitoringRuntime
from app.models import AppSetting, IntegrationEvent, ProductPriceHistory, SourceType
from app.services.shopee_recurring import ShopeeRecurringMonitor

base_context = collection.context
no_network = collection.no_network


def setup(ctx):
    single.setup(ctx)
    ctx["recurring"] = ShopeeRecurringMonitor(ctx["service"])
    ctx["grant"] = str(uuid4())
    ctx["spec"] = dict(
        authorization_id=ctx["grant"],
        product_id=ctx["product_id"],
        shop_id="101",
        item_id="202",
        actor="responsavel",
        valid_from=ctx["clock"][0].isoformat(),
        expires_at=(ctx["clock"][0] + timedelta(days=2)).isoformat(),
        max_requests=4,
        min_interval_seconds=21600,
    )
    return ctx


@pytest.fixture
def context(base_context):
    return setup(base_context)


def approved(ctx):
    monitor = ctx["recurring"]
    plan = monitor.prepare(ctx["spec"], offline=True)
    monitor.approve(
        ctx["grant"],
        dict(
            scope_sha256=plan["scope_sha256"],
            actor="responsavel",
            confirmed_recurring_read=True,
            quotas_confirmed=True,
            quota_evidence="SYNTHETIC QUOTA EVIDENCE — NOT OFFICIAL",
        ),
        offline=True,
    )


def execute(ctx, cycle=None):
    return ctx["recurring"].execute(
        ctx["grant"], cycle or uuid4(), offline=True, connector=ctx["connector"]
    )


def advance(ctx):
    ctx["clock"][0] += timedelta(hours=6)


def test_missing_unapproved_and_expired(context):
    with pytest.raises(LookupError):
        execute(context)
    monitor = context["recurring"]
    monitor.prepare(context["spec"], offline=True)
    with pytest.raises(ValueError, match="autorização"):
        execute(context)
    approved(context)
    context["clock"][0] += timedelta(days=3)
    with pytest.raises(ValueError, match="validade"):
        execute(context)
    assert context["calls"] == 0


def test_budget_and_interval(context):
    context["spec"]["max_requests"] = 1
    approved(context)
    result = execute(context)
    assert result["status"] == "SUCCEEDED"
    advance(context)
    with pytest.raises(ValueError, match="Orçamento"):
        execute(context)
    assert context["calls"] == 1
    observation.assert_no_commerce(context)


def test_interval_dynamic_without_new_permission(context):
    approved(context)
    execute(context)
    with pytest.raises(ValueError, match="Intervalo"):
        execute(context)
    monitor = context["recurring"]
    with pytest.raises(ValueError, match="ampliar"):
        monitor.set_interval(context["grant"], 60, offline=True)
    monitor.set_interval(context["grant"], 43200, offline=True)
    advance(context)
    with pytest.raises(ValueError, match="Intervalo"):
        execute(context)
    advance(context)
    assert execute(context)["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "price,direction", [("99.90", "UNCHANGED"), ("100.00", "RISE"), ("89.91", "DROP")]
)
def test_series(context, price, direction):
    approved(context)
    first = execute(context)
    assert first["direction"] == "INITIAL"
    advance(context)
    context["node"].update(price=price, priceMin=price, priceMax=price)
    result = execute(context)
    assert result["direction"] == direction
    rows = observation.histories(context)
    assert len(rows) == 2 and rows[0].observed_at < rows[1].observed_at
    assert rows[0].evidence_metadata["price_basis"] == rows[1].evidence_metadata["price_basis"]
    assert all(r.source_type == SourceType.OFFICIAL_API and r.previous_price is None for r in rows)
    assert all(r.availability.value == r.price_access.value == "UNKNOWN" for r in rows)
    assert all(r.evidence_metadata["commercial_verified"] is False for r in rows)
    observation.assert_no_commerce(context)


@pytest.mark.parametrize(
    "failure,status",
    [
        ("timeout", "UNCERTAIN"),
        (403, "FAILED"),
        (429, "FAILED"),
        ("empty", "FAILED"),
        ("identity", "FAILED"),
    ],
)
def test_failures_pause_without_retry(context, failure, status):
    approved(context)
    if failure == "empty":
        context["nodes"] = []
    elif failure == "identity":
        context["node"].update(itemId=999, productLink="https://shopee.com.br/product/101/999")
    else:
        context["failure"] = failure
    cycle = uuid4()
    assert execute(context, cycle)["status"] == status
    assert execute(context, cycle)["status"] == status
    advance(context)
    with pytest.raises(ValueError, match="autorização"):
        execute(context)
    assert context["calls"] == 1 and observation.histories(context) == []
    assert context["recurring"].status(context["grant"])["state"] == "PAUSED"
    observation.assert_no_commerce(context)


def test_replay(context):
    approved(context)
    cycle = uuid4()
    first = execute(context, cycle)
    assert execute(context, cycle) == first
    assert context["calls"] == 1 and len(observation.histories(context)) == 1
    assert context["recurring"].status(context["grant"])["used"] == 1


def test_concurrent_cycles(context):
    approved(context)
    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(10)

    context["handler"] = block
    cycle = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(execute, context, cycle)
        try:
            assert entered.wait(10)
            assert execute(context, cycle)["status"] == "UNCERTAIN"
            with pytest.raises(ValueError, match="autorização"):
                execute(context)
        finally:
            release.set()
        assert future.result()["status"] == "SUCCEEDED"
    assert context["calls"] == 1


def test_claim_precedes_http(context):
    approved(context)

    def inspect():
        state = context["recurring"].status(context["grant"])
        assert state["active_cycle"] and state["used"] == 1
        assert state["state"] == "UNCERTAIN"
        with context["factory"]() as session:
            assert list(
                session.scalars(
                    select(IntegrationEvent.status)
                    .where(IntegrationEvent.operation == "recurring_observation")
                    .order_by(IntegrationEvent.id)
                )
            ) == ["PREPARED", "APPROVED", "CLAIMED"]
            assert (
                session.scalar(select(AppSetting).where(AppSetting.key.like("%:cycle:%"))).value[
                    "status"
                ]
                == "CLAIMED"
            )

    context["handler"] = inspect
    assert execute(context)["status"] == "SUCCEEDED"


def test_changed_account_blocks(context):
    from pydantic import SecretStr

    approved(context)
    context["settings"].shopee_app_id = SecretStr("DIFFERENT_ACCOUNT")
    with pytest.raises(ValueError, match="divergente"):
        execute(context)
    assert context["calls"] == 0


def test_real_mode_blocked(context):
    monitor = context["recurring"]
    for call in (
        lambda: monitor.prepare(context["spec"]),
        lambda: monitor.execute(context["grant"], uuid4()),
        lambda: monitor.approve(context["grant"], {}),
    ):
        with pytest.raises(ValueError, match="real bloqueada"):
            call()
    assert context["calls"] == 0


def test_no_quota_evidence_no_approval(context):
    monitor = context["recurring"]
    plan = monitor.prepare(context["spec"], offline=True)
    with pytest.raises(ValidationError):
        monitor.approve(
            context["grant"],
            dict(
                scope_sha256=plan["scope_sha256"],
                actor="responsavel",
                confirmed_recurring_read=True,
                quotas_confirmed=False,
                quota_evidence="UNKNOWN QUOTAS",
            ),
            offline=True,
        )
    assert context["calls"] == 0


def test_scheduler_prepared_paused(context):
    approved(context)
    runtime = MonitoringRuntime(context["factory"], context["settings"])
    context["recurring"].prepare_scheduler(
        runtime, context["grant"], context["connector"], offline=True
    )
    job = runtime._prepared_jobs[context["recurring"]._key(context["grant"])]
    assert job["next_run_time"] is None and job["max_instances"] == 1
    assert runtime._scheduler is None and context["calls"] == 0
    context["recurring"].set_interval(context["grant"], 43200, offline=True)
    for callback in runtime._configuration_observers.values():
        callback()
    assert job["seconds"] == 43200 and job["next_run_time"] is None


def test_manual_baseline_not_compared(context):
    approved(context)
    first = execute(context)
    with context["factory"]() as session:
        history = session.get(ProductPriceHistory, first["history_id"])
        history.source_type = SourceType.MANUAL
        session.commit()
    advance(context)
    context["node"].update(price="50", priceMin="50", priceMax="50")
    assert execute(context)["direction"] == "INITIAL"


def test_interrupted_claim_cannot_resume(context, monkeypatch):
    approved(context)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(context["recurring"].single, "prepare", interrupted)
    cycle = uuid4()
    with pytest.raises(KeyboardInterrupt):
        execute(context, cycle)
    assert execute(context, cycle)["status"] == "UNCERTAIN"
    assert context["recurring"].status(context["grant"])["state"] == "PAUSED"
    assert context["calls"] == 0


def test_persistence_failure_does_not_repeat_http(context, monkeypatch):
    import app.services.shopee_recurring as module

    approved(context)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic persistence failure")

    monkeypatch.setattr(module, "record_price", unavailable)
    cycle = uuid4()
    with pytest.raises(RuntimeError):
        execute(context, cycle)
    assert execute(context, cycle)["status"] == "UNCERTAIN"
    assert context["calls"] == 1 and observation.histories(context) == []
    assert context["recurring"].status(context["grant"])["used"] == 1
    observation.assert_no_commerce(context)
