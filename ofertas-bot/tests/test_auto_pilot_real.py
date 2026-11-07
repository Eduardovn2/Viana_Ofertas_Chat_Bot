"""Real adapter state machine and HTTP boundary; every transport is explicitly mocked."""

import copy
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import test_auto_pilot as offline
import test_shopee_collection as collection
from sqlalchemy import select

from app.models import AppSetting, PublicationDestination
from app.services.auto_pilot import PREFIX
from app.services.auto_pilot_real import PilotApproval, RealAutoPilotService

base_context = collection.context
no_network = collection.no_network
application = collection.application
client = collection.client


def setup(ctx):
    ctx = offline.setup(ctx)
    original_sender = ctx["publisher"]
    settings = ctx["settings"].model_copy(
        update={
            "telegram_bot_token": original_sender.settings.telegram_bot_token,
            "telegram_allowed_chat_ids": (offline.CHAT,),
        }
    )
    ctx["settings"] = settings
    controller = RealAutoPilotService(
        ctx["factory"],
        settings,
        isolated_testing=True,
        shopee_transport=ctx["http"]._transport,
        telegram_transport=ctx["telegram_client"]._transport,
        clock=lambda: ctx["clock"][0],
    )
    ctx["pilot"], ctx["publisher"] = controller, controller.publisher
    # Start with the same paused destination as production. Only a signed pilot
    # grant may authorize temporary activation in this isolated database.
    with ctx["factory"]() as session:
        session.get(PublicationDestination, ctx["destination_id"]).enabled = False
        session.commit()
    return ctx


@pytest.fixture
def context(base_context):
    ctx = setup(base_context)
    yield ctx
    ctx["pilot"].close()
    ctx["telegram_client"].close()


def prepared(ctx, **changes):
    spec = (
        dict(
            authorization_id=str(uuid4()),
            actor="synthetic_owner",
            destination_id=ctx["destination_id"],
            not_before=ctx["clock"][0],
            expires_at=ctx["clock"][0] + timedelta(hours=2),
            terms=[offline.QUERY],
            operating_policy=None,  # Immutable legacy grant semantics.
        )
        | changes
    )
    result = ctx["pilot"].prepare(spec)
    ctx["pilot_id"] = result["authorization_id"]
    return result


def approval(plan):
    return dict(
        actor="synthetic_responsible",
        scope_sha256=plan["scope_sha256"],
        confirmed_pilot_scope=True,
        acknowledge_unconfirmed_quotas_and_permissions=True,
    )


def authorized(ctx, **changes):
    plan = prepared(ctx, **changes)
    ctx["pilot"].approve(ctx["pilot_id"], approval(plan))
    ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])
    return plan


def test_prepare_never_approves_activates_or_calls_http(context):
    ctx = context
    plan = prepared(ctx)
    assert plan["state"] == "PREPARED" and plan["approval"] is None
    assert "scope_signature" not in plan
    with pytest.raises(ValueError):
        ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])
    # Even invoking a tick against PREPARED cannot issue HTTP.
    ctx["pilot"].tick(ctx["pilot_id"])
    with ctx["factory"]() as session:
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled
    assert not ctx["telegram_calls"] and ctx["calls"] == 0


def test_signed_grant_reuses_queue_and_outbox_without_per_message_approval(context):
    ctx = context
    plan = authorized(ctx, max_messages=2)
    offline.listings(ctx, [1, 2, 3])
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["counts"]["published"] == 2 and result["reason"] == "MESSAGE_BUDGET"
    assert result["scope"]["mode"] == "REAL_AUTO_PILOT" and result["external_http_calls"] == 0
    with ctx["factory"]() as session:
        row = session.scalar(
            select(AppSetting.value).where(AppSetting.key == PREFIX + ctx["pilot_id"])
        )
        assert sum(r.get("transport_consumed", False) for r in row["queries"]) == 1
        assert sum(r.get("transport_consumed", False) for r in row["reservations"]) == 2
        assert row["scope_signature"] and row["approval_signature"]
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled
    with pytest.raises(ValueError):
        ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])
    assert len(ctx["telegram_calls"]) == 2


@pytest.mark.parametrize("tamper", ["scope", "scope_rehash", "signature", "approval"])
def test_signature_scope_and_approval_tampering_blocks_http(context, tamper):
    ctx = context
    plan = prepared(ctx)
    ctx["pilot"].approve(ctx["pilot_id"], approval(plan))
    with ctx["factory"]() as session:
        row = session.scalar(select(AppSetting).where(AppSetting.key == PREFIX + ctx["pilot_id"]))
        value = copy.deepcopy(row.value)
        if tamper in {"scope", "scope_rehash"}:
            value["scope"]["plan"]["terms"] = ["changed"]
            if tamper == "scope_rehash":
                from app.integrations.contracts import fingerprint

                value["scope_sha256"] = fingerprint(value["scope"])
        elif tamper == "signature":
            value["scope_signature"] = "0" * 64
        else:
            value["approval"]["actor"] = "invented_actor"
        row.value = value
        session.commit()
    with pytest.raises(ValueError):
        ctx["pilot"].tick(ctx["pilot_id"])
    assert not ctx["telegram_calls"] and ctx["calls"] == 0
    assert ctx["pilot"].stop(ctx["pilot_id"])["state"] == "STOPPED"


def test_timeout_does_not_retry_and_stop_restores_destination(context):
    ctx = context
    authorized(ctx)
    offline.listings(ctx, [1])
    ctx["telegram_failure"] = True
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["counts"]["uncertain"] == 1
    ctx["pilot"].tick(ctx["pilot_id"])
    assert len(ctx["telegram_calls"]) == 1
    ctx["pilot"].stop(ctx["pilot_id"])
    with ctx["factory"]() as session:
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled


def test_deadline_worker_exits_without_http_and_restores_destination(context):
    ctx = context
    plan = authorized(ctx)

    def advance(seconds):
        ctx["clock"][0] += timedelta(hours=2)

    result = ctx["pilot"].run_worker(ctx["pilot_id"], plan["scope_sha256"], wait=advance)
    assert result["reason"] == "DEADLINE" and ctx["calls"] <= 1
    with ctx["factory"]() as session:
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled


def test_http_without_permit_or_changed_payload_is_blocked(context):
    ctx = context
    authorized(ctx)
    with pytest.raises(ValueError, match="reserva"):
        ctx["pilot"]._shopee_client.post(
            "https://open-api.affiliate.shopee.com.br/graphql", json={"query": "mutation forbidden"}
        )
    reserved = ctx["pilot"]._reserve_query(ctx["pilot_id"])
    ctx["pilot"]._context.permit = (ctx["pilot_id"], "query", reserved["id"])
    with pytest.raises(ValueError, match="Payload"):
        ctx["pilot"]._shopee_client.post(
            "https://open-api.affiliate.shopee.com.br/graphql", json={"query": "changed"}
        )
    assert ctx["calls"] == 0


def test_testing_cannot_select_real_transport_or_production_environment(context):
    ctx = context
    with pytest.raises(ValueError, match="MockTransport"):
        RealAutoPilotService(
            ctx["factory"],
            ctx["settings"],
            isolated_testing=True,
            shopee_transport=httpx.HTTPTransport(),
            telegram_transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        )
    live = RealAutoPilotService(ctx["factory"], ctx["settings"])
    try:
        with pytest.raises(ValueError, match="ofertas_bot/public"):
            live.prepare({})
    finally:
        live.close()


def test_explicit_confirmation_and_hash_required(context):
    ctx = context
    plan = prepared(ctx)
    for changes in (
        {"confirmed_pilot_scope": False},
        {"confirmed_pilot_scope": "true"},
        {"acknowledge_unconfirmed_quotas_and_permissions": False},
    ):
        with pytest.raises(ValueError):
            PilotApproval.model_validate(approval(plan) | changes)
    with pytest.raises(ValueError):
        ctx["pilot"].approve(ctx["pilot_id"], approval(plan) | {"scope_sha256": "0" * 64})


def test_real_api_authentication_and_no_implicit_start(context, application, client):
    ctx = context
    prepared(ctx)
    application.state.auto_pilot_real = ctx["pilot"]
    path = "/api/v1/auto-pilots-real/" + ctx["pilot_id"]
    assert client.get(path).status_code == 401
    assert client.get(path, headers=collection.api.HEADERS).json()["state"] == "PREPARED"
    assert ctx["calls"] == 0 and not ctx["telegram_calls"]


def test_real_twenty_cap_and_rolling_window_reuse_controller(context, monkeypatch):
    monkeypatch.setattr(offline, "prepare", authorized)
    offline.test_twenty_messages_end_the_default_pilot(context)
    with context["factory"]() as session:
        assert not session.get(PublicationDestination, context["destination_id"]).enabled


def test_stop_at_last_http_boundary_blocks_transmission(context, monkeypatch):
    ctx = context
    authorized(ctx)
    offline.listings(ctx, [1])
    original = ctx["pilot"]._authorize_http

    def stop_before_telegram(request):
        if request.url.host == "api.telegram.org":
            ctx["pilot"].stop(ctx["pilot_id"])
        return original(request)

    monkeypatch.setattr(ctx["pilot"], "_authorize_http", stop_before_telegram)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["state"] == "STOPPED" and not ctx["telegram_calls"]
    assert result["counts"]["uncertain"] == 1


def test_expired_window_at_transport_boundary_blocks_http(context, monkeypatch):
    ctx = context
    authorized(ctx)
    offline.listings(ctx, [1])
    original = ctx["pilot"]._authorize_http

    def expire_before_telegram(request):
        if request.url.host == "api.telegram.org":
            ctx["clock"][0] += timedelta(hours=2)
        return original(request)

    monkeypatch.setattr(ctx["pilot"], "_authorize_http", expire_before_telegram)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["reason"] == "DEADLINE" and not ctx["telegram_calls"]


def test_assisted_backend_cannot_start_even_an_approved_real_pilot(context, application, client):
    ctx = context
    plan = prepared(ctx)
    ctx["pilot"].approve(ctx["pilot_id"], approval(plan))
    application.state.auto_pilot_real = ctx["pilot"]
    application.state.execution_profile = "ASSISTED_PREPARATION"
    path = "/api/v1/auto-pilots-real/" + ctx["pilot_id"]
    assert (
        client.post(
            path + "/start",
            json={"scope_sha256": plan["scope_sha256"]},
            headers=collection.api.HEADERS,
        ).status_code
        == 409
    )
    assert client.post(path + "/tick", headers=collection.api.HEADERS).status_code == 409
    assert ctx["calls"] == 0 and not ctx["telegram_calls"]


@pytest.mark.parametrize(
    "failure,reason", [(403, "SHOPEE_PERMISSION_DENIED"), ("timeout", "QUERY_UNCERTAINTY_NO_RETRY")]
)
def test_query_permissions_and_uncertainty_do_not_repeat_payload(context, failure, reason):
    ctx = context
    authorized(ctx)
    ctx["failure"] = failure
    ctx["pilot"].tick(ctx["pilot_id"])
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["reason"] == reason and ctx["calls"] == 1
    assert not ctx["telegram_calls"]


def test_telegram_permission_failure_stops_before_next_message(context):
    ctx = context
    authorized(ctx)
    offline.listings(ctx, [1, 2, 3])
    attempts = []

    def denied(request):
        attempts.append(True)
        return httpx.Response(403, json={"ok": False, "error_code": 403})

    ctx["pilot"]._telegram_guard.delegate = httpx.MockTransport(denied)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["reason"] == "TELEGRAM_PERMISSION_DENIED"
    assert len(attempts) == 1 and result["counts"]["published"] == 0


def test_request_credential_header_cannot_escape_signed_scope(context):
    ctx = context
    authorized(ctx)
    offline.listings(ctx, [1])

    def corrupt(request):
        request.headers["Authorization"] = (
            "SHA256 Credential=other, Timestamp=1, Signature=" + "0" * 64
        )

    ctx["pilot"]._shopee_client.event_hooks["request"].append(corrupt)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert ctx["calls"] == 0 and not ctx["telegram_calls"]
    assert not result["queries"][0].get("transport_consumed", False)
