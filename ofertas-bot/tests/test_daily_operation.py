import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4, uuid5

import httpx
import pytest
import test_account_evidence_reuse as account
import test_announcements as announcements
import test_auto_pilot as offline
import test_auto_pilot_real as real
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import daily_operation as daily_api
from app.api.daily_operation import LOGIN, router
from app.daily_worker import DailyWorker
from app.services.auto_pilot import PREFIX
from app.services.daily_operation import (
    DAY,
    ZONE,
    approve,
    control,
    ensure_setting,
    prepare,
    setting,
)
from app.services.shopee_evidence import record_account_evidence

base_context = real.base_context
context = real.context
no_network = real.no_network


def setup(ctx, *, max_links=0, quota=True, **overrides):
    ctx["clock"][0] = datetime(2026, 11, 6, 7, 59, tzinfo=ZONE)
    now = ctx["clock"][0]
    identifier = uuid4()
    with ctx["factory"]() as session:
        result = prepare(
            session,
            ctx["settings"],
            dict(
                authorization_id=identifier,
                actor="synthetic_owner",
                destination_id=ctx["destination_id"],
                start_date=now.date(),
                end_date=now.date() + timedelta(days=1),
                terms=[offline.QUERY],
                max_queries=3,
                max_messages=3,
                max_links=max_links,
                messages_per_window=3,
                query_interval_seconds=3600,
                quota_reference="synthetic:quota" if quota else None,
                shopee_daily_quota=6 if quota else None,
                telegram_daily_quota=3 if quota else None,
            )
            | overrides,
        )
    return identifier, result["scope_sha256"]


def activate(ctx, identifier, digest, *, start=True):
    with ctx["factory"]() as session:
        approve(
            session,
            ctx["settings"],
            identifier,
            digest,
            "synthetic_owner",
            confirmed=True,
            acknowledge_unconfirmed_quotas=False,
            now=ctx["clock"][0],
        )
        if start:
            control(session, ctx["settings"], "start", ctx["clock"][0], identifier)


def report(ctx):
    with ctx["factory"]() as session:
        day = setting(session, DAY + ctx["clock"][0].astimezone(ZONE).date().isoformat())
        pilot_id = day.value["pilot_id"]
    ctx["pilot_id"] = pilot_id
    return ctx["pilot"].report(pilot_id)


def test_daily_100_limit_preserves_discovery_and_resets_only_next_day(context, monkeypatch):
    ctx = context
    identifier, digest = setup(
        ctx,
        max_links=24,
        max_queries=36,
        max_messages=100,
        shopee_daily_quota=60,
        telegram_daily_quota=100,
        query_interval_seconds=600,
    )
    ctx["clock"][0] = ctx["clock"][0].replace(hour=11, minute=0)
    announcements.links(ctx)
    first, second = ctx["nodes"]
    ctx["nodes"] = [first]
    ctx["pilot_id"] = str(uuid5(identifier, ctx["clock"][0].date().isoformat()))
    activate(ctx, identifier, digest)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    original = report(ctx)
    assert original["counts"]["published"] == 1
    with ctx["factory"]() as session:
        row = setting(session, PREFIX + ctx["pilot_id"], lock=True)
        spent = [
            dict(
                id=str(uuid4()),
                identity=f"{1000 + i}:{2000 + i}",
                at=(ctx["clock"][0] - timedelta(hours=2)).isoformat(),
                state="FAILED",
                http_started=True,
                transport_consumed=True,
                destination_id=ctx["destination_id"],
            )
            for i in range(98)
        ]
        row.value = dict(row.value, reservations=spent + row.value["reservations"])
        session.commit()
    ctx["clock"][0] += timedelta(minutes=10)
    ctx["nodes"] = [second]
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    result = report(ctx)
    assert len(result["reservations"]) == 100 and len(ctx["telegram_calls"]) == 2
    ctx["clock"][0] += timedelta(minutes=10)
    offline.listings(ctx, [3], verified=False)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    result = report(ctx)
    assert len(result["reservations"]) == 100 and len(result["queries"]) == 3
    assert not result.get("daily_links") and len(ctx["telegram_calls"]) == 2
    assert result["findings"][-1]["reason"] == "DAILY_MESSAGE_BUDGET_EXHAUSTED"

    class SimulatedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return ctx["clock"][0].astimezone(tz)

    monkeypatch.setattr(daily_api, "datetime", SimulatedDatetime)
    with panel_client(ctx) as client:
        status = client.get("/api/v1/daily", headers={"X-API-Key": PANEL_KEY}).json()
    assert status["publication_limit"] == 100 and status["used"]["messages"] == 100
    assert status["published_today"] == 2 and status["waiting_to_send"] == []
    ctx["clock"][0] = ctx["clock"][0].replace(hour=23, minute=0)
    assert DailyWorker(ctx["pilot"]).step() == "WAITING"
    assert len(report(ctx)["queries"]) == 3
    ctx["clock"][0] = (ctx["clock"][0] + timedelta(days=1)).replace(hour=8)
    ctx["pilot_id"] = str(uuid5(identifier, ctx["clock"][0].date().isoformat()))
    ctx["nodes"] = [first]
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    assert len(report(ctx)["reservations"]) == 1


def test_daily_burst_uses_sliding_window_and_keeps_discovery(context):
    ctx = context
    identifier, digest = setup(
        ctx,
        max_messages=100,
        max_queries=3,
        messages_per_window=2,
        telegram_daily_quota=100,
        query_interval_seconds=60,
    )
    announcements.links(ctx, (1, 2, 3))
    activate(ctx, identifier, digest)
    ctx["clock"][0] = ctx["clock"][0].replace(hour=8, minute=0)
    ctx["pilot_id"] = str(uuid5(identifier, ctx["clock"][0].date().isoformat()))
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    result = report(ctx)
    assert len(ctx["telegram_calls"]) == 2 and len(result["reservations"]) == 2
    assert any(item["state"] == "QUEUED" for item in result["queue"])
    ctx["clock"][0] += timedelta(seconds=61)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    result = report(ctx)
    assert len(result["queries"]) == 2 and len(result["reservations"]) == 2


def test_100_draft_with_unknown_quotas_cannot_be_approved(context):
    identifier, digest = setup(
        context,
        max_messages=100,
        quota=False,
        max_queries=None,
        max_links=None,
        query_interval_seconds=None,
    )
    with context["factory"]() as session, pytest.raises(ValueError, match="orçamentos Shopee"):
        approve(
            session,
            context["settings"],
            identifier,
            digest,
            "synthetic_owner",
            confirmed=True,
            acknowledge_unconfirmed_quotas=True,
            now=context["clock"][0],
        )


def test_daily_window_restart_pause_stop_and_next_day(context, monkeypatch):
    ctx = context
    offline.listings(ctx, [1], verified=False)
    identifier, digest = setup(ctx)
    worker = DailyWorker(ctx["pilot"])
    assert worker.step() == "STOPPED"
    activate(ctx, identifier, digest, start=False)

    class SimulatedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return ctx["clock"][0].astimezone(tz)

    monkeypatch.setattr(daily_api, "datetime", SimulatedDatetime)

    def command(action):
        with panel_client(ctx) as client:
            return client.post(
                "/api/v1/daily/control",
                headers={"X-API-Key": PANEL_KEY},
                json={"action": action, "identifier": str(identifier)},
            )

    assert command("start").status_code == 200
    assert worker.step() == "WAITING"
    ctx["clock"][0] = ctx["clock"][0].replace(hour=11, minute=0)
    assert worker.step() == "RUNNING"
    first = report(ctx)
    assert len(first["queries"]) == 1
    ctx["clock"][0] += timedelta(seconds=31)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    assert len(report(ctx)["queries"]) == 1
    assert command("pause").status_code == 200
    ctx["clock"][0] += timedelta(hours=2)
    with pytest.raises(ValueError, match="pausada"):
        ctx["pilot"].discover_once(first["authorization_id"])
    assert worker.step() == "PAUSED"
    assert len(report(ctx)["queries"]) == 1
    assert command("resume").status_code == 200
    ctx["clock"][0] = ctx["clock"][0].replace(hour=23, minute=0)
    assert worker.step() == "WAITING"
    assert len(report(ctx)["queries"]) == 1
    ctx["clock"][0] = (ctx["clock"][0] + timedelta(days=1)).replace(hour=8)
    assert worker.step() == "RUNNING"
    second = report(ctx)
    assert second["authorization_id"] != first["authorization_id"] and len(second["queries"]) == 1
    assert command("stop").status_code == 200
    assert command("start").status_code == 422
    assert worker.step() == "STOPPED"


def test_recurring_grant_requires_documented_quotas(context):
    identifier, digest = setup(context, quota=False)
    with context["factory"]() as session, pytest.raises(ValueError, match="quotas confirmadas"):
        approve(
            session,
            context["settings"],
            identifier,
            digest,
            "synthetic_owner",
            confirmed=True,
            acknowledge_unconfirmed_quotas=True,
            now=context["clock"][0],
        )


def test_daily_supply_continues_after_uncertain_link(context):
    ctx = context
    offline.listings(ctx, [1, 2], verified=False)
    identifier, digest = setup(ctx, max_links=2)
    with ctx["factory"]() as session:
        record_account_evidence(
            session, ctx["settings"], account.account_document(ctx), now=ctx["clock"][0]
        )
        session.commit()
    original = ctx["pilot"]._shopee_guard.delegate
    link_calls = []

    def respond(request):
        body = json.loads(request.content)["query"]
        if "generateShortLink" not in body:
            return original.handle_request(request)
        link_calls.append(body)
        if len(link_calls) == 1:
            raise httpx.ReadTimeout("mock", request=request)
        origin = next(
            f"https://shopee.com.br/product/{100 + n}/{200 + n}"
            for n in (1, 2)
            if f"/{100 + n}/{200 + n}" in body
        )
        return httpx.Response(
            200,
            json={
                "data": {
                    "generateShortLink": {
                        "shortLink": "https://s.shopee.com.br/syntheticDaily",
                        "longLink": origin,
                    }
                }
            },
        )

    ctx["pilot"]._shopee_guard.delegate = httpx.MockTransport(respond)
    activate(ctx, identifier, digest)
    ctx["clock"][0] = ctx["clock"][0].replace(hour=8, minute=0)
    worker = DailyWorker(ctx["pilot"])
    # The mock Telegram provider inspects the deterministic child reservation.
    from uuid import uuid5

    ctx["pilot_id"] = str(uuid5(identifier, ctx["clock"][0].date().isoformat()))
    assert worker.step() == "RUNNING"
    ctx["clock"][0] += timedelta(seconds=31)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    result = report(ctx)
    assert [r["state"] for r in result["daily_links"]] == ["UNCERTAIN", "SUCCEEDED"]
    assert result["counts"]["published"] == 1 and len(link_calls) == 2


PANEL_KEY = "synthetic-admin-secret-0123456789"


def panel_client(context):
    app = FastAPI()
    app.state.session_factory = context["factory"]
    app.state.settings = context["settings"].model_copy(
        update={"admin_api_key": SecretStr(PANEL_KEY)}
    )
    app.include_router(router)
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))


def test_panel_uses_expiring_cookie_and_rejects_foreign_origin(context):
    code = "synthetic-one-use-code"
    from datetime import UTC

    with context["factory"]() as session:
        ensure_setting(
            session,
            LOGIN,
            dict(
                digest=hashlib.sha256(code.encode()).hexdigest(),
                expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                used=False,
            ),
        )
        session.commit()
    with panel_client(context) as client:
        assert client.get("/daily").status_code == 200
        assert client.get("/api/v1/daily").status_code == 401
        headers = {"Origin": "http://127.0.0.1:8000", "X-Daily-Panel": "1"}
        r = client.post("/api/v1/daily/session", json={"code": code}, headers=headers)
        assert r.status_code == 200 and "HttpOnly" in r.headers["set-cookie"]
        assert client.get("/api/v1/daily").status_code == 200
        assert (
            client.post("/api/v1/daily/session", json={"code": code}, headers=headers).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/daily/control",
                json={"action": "stop"},
                headers=dict(headers, Origin="https://other.example"),
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/daily/control", json={"action": "stop"}, headers=headers
            ).status_code
            == 200
        )
