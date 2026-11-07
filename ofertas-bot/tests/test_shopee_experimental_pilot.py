"""Bounded pilot tested without official API calls or operational authorizations."""

from datetime import timedelta
from uuid import uuid4

import pytest
import test_shopee_collection as collection
import test_shopee_recurring as recurring
from pydantic import ValidationError

base_context = collection.context
no_network = collection.no_network


def setup(ctx):
    recurring.setup(ctx)
    ctx["spec"].update(
        max_requests=2, expires_at=(ctx["clock"][0] + timedelta(hours=24)).isoformat()
    )
    return ctx


@pytest.fixture
def context(base_context):
    return setup(base_context)


def prepared(ctx):
    value = ctx["recurring"].prepare_pilot(ctx["spec"], offline=True)
    ctx["confirmation"] = dict(
        scope_sha256=value["scope_sha256"],
        actor="responsavel",
        confirmed_experimental_pilot=True,
        acknowledge_unknown_quotas=True,
    )
    return value


def approved(ctx):
    prepared(ctx)
    return ctx["recurring"].approve_pilot(ctx["grant"], ctx["confirmation"], offline=True)


def execute(ctx, cycle=None):
    return ctx["recurring"].execute_pilot(
        ctx["grant"],
        cycle or uuid4(),
        ctx["confirmation"],
        offline=True,
        connector=ctx["connector"],
    )


def test_prepared_not_approved(context):
    state = prepared(context)
    assert state["mode"] == "MOCK_PILOT" and state["state"] == "PREPARED"
    assert "approval" not in state and state["used"] == 0
    with pytest.raises(ValueError, match="autorização"):
        execute(context)
    assert context["calls"] == 0


@pytest.mark.parametrize("change", [{"max_requests": 3}, {"min_interval_seconds": 21599}])
def test_bounds(context, change):
    context["spec"].update(change)
    with pytest.raises(ValueError, match="Piloto limitado"):
        prepared(context)
    assert context["calls"] == 0


def test_window(context):
    context["spec"]["expires_at"] = (context["clock"][0] + timedelta(hours=25)).isoformat()
    with pytest.raises(ValueError, match="Piloto limitado"):
        prepared(context)


def test_explicit_acknowledgement(context):
    prepared(context)
    with pytest.raises(ValidationError):
        context["recurring"].approve_pilot(
            context["grant"],
            {**context["confirmation"], "acknowledge_unknown_quotas": False},
            offline=True,
        )
    assert context["calls"] == 0


def test_two_cycles_only(context):
    approved(context)
    cycle = uuid4()
    first = execute(context, cycle)
    assert first["status"] == "SUCCEEDED" and first["mode"] == "MOCK_PILOT"
    assert execute(context, cycle) == first
    with pytest.raises(ValueError, match="Intervalo"):
        execute(context)
    recurring.advance(context)
    assert execute(context)["direction"] == "UNCHANGED"
    recurring.advance(context)
    with pytest.raises(ValueError, match="Orçamento"):
        execute(context)
    assert context["calls"] == 2
    recurring.observation.assert_no_commerce(context)


@pytest.mark.parametrize(
    "failure,status", [(403, "FAILED"), (429, "FAILED"), ("timeout", "UNCERTAIN")]
)
def test_pause(context, failure, status):
    approved(context)
    context["failure"] = failure
    cycle = uuid4()
    assert execute(context, cycle)["status"] == status
    assert execute(context, cycle)["status"] == status
    recurring.advance(context)
    with pytest.raises(ValueError, match="autorização"):
        execute(context)
    assert context["calls"] == 1


def test_hash_and_mode_cannot_change(context):
    prepared(context)
    with pytest.raises(ValueError, match="escopo"):
        context["recurring"].approve_pilot(
            context["grant"], {**context["confirmation"], "scope_sha256": "0" * 64}, offline=True
        )
    with pytest.raises(ValueError, match="Modo diverge"):
        recurring.execute(context)
    assert context["calls"] == 0


def test_window_expired(context):
    approved(context)
    context["clock"][0] += timedelta(hours=24)
    with pytest.raises(ValueError, match="validade"):
        execute(context)
    assert context["calls"] == 0


def test_spacing_after_delayed_response(context):
    approved(context)

    def delay():
        context["clock"][0] += timedelta(seconds=5)

    context["handler"] = delay
    execute(context)
    context["handler"] = None
    context["clock"][0] += timedelta(hours=6) - timedelta(seconds=1)
    with pytest.raises(ValueError, match="Intervalo"):
        execute(context)
    context["clock"][0] += timedelta(seconds=1)
    assert execute(context)["status"] == "SUCCEEDED"


def test_account_change(context):
    approved(context)
    from pydantic import SecretStr

    context["settings"].shopee_app_id = SecretStr("DIFFERENT_ACCOUNT")
    with pytest.raises(ValueError, match="divergente"):
        execute(context)
    assert context["calls"] == 0


def test_concurrent_pilot_cycles(context):
    # Same parent CAS/lock and child lease as the recurring contract.
    approved(context)
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(10)

    context["handler"] = block
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(execute, context)
        try:
            assert entered.wait(10)
            with pytest.raises(ValueError, match="autorização"):
                execute(context)
        finally:
            release.set()
        assert future.result()["status"] == "SUCCEEDED"
    assert context["calls"] == 1


def test_no_mode_promotion(context):
    prepared(context)
    with pytest.raises(ValueError):
        context["recurring"].approve_pilot(context["grant"], context["confirmation"])
    assert context["calls"] == 0
