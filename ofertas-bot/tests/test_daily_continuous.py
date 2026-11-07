from datetime import timedelta
from uuid import uuid5

import httpx
import test_account_evidence_reuse as account
import test_announcements as announcements
import test_auto_pilot as offline
import test_daily_operation as daily
from sqlalchemy import func, select

from app.daily_worker import DailyWorker
from app.services.daily_queue import candidates, record_capture, refresh_candidate, shopee_ready
from app.services.shopee_evidence import record_account_evidence

base_context = daily.base_context
context = daily.context
no_network = daily.no_network


def configure(ctx, **changes):
    return daily.setup(
        ctx,
        **(
            dict(
                operation_version="DAILY_V3",
                max_messages=None,
                messages_per_window=None,
                shopee_posts_per_minute=3,
                telegram_daily_quota=None,
                query_interval_seconds=60,
            )
            | changes
        ),
    )


def open_day(ctx, identifier, *, hour=8):
    ctx["clock"][0] = ctx["clock"][0].replace(hour=hour, minute=0)
    ctx["pilot_id"] = str(uuid5(identifier, ctx["clock"][0].date().isoformat()))


def test_thousand_candidates_persist_and_remain_selectable(context):
    ctx = context
    identifier, digest = configure(ctx, max_queries=1)
    daily.activate(ctx, identifier, digest)
    value = {
        "scope": {
            "plan": {
                "daily_grant_id": str(identifier),
                "operating_policy": {"telegram_per_minute": 20},
                "max_queries": 1,
                "max_messages": 18000,
            }
        },
        "queries": [],
        "reservations": [],
    }
    now = ctx["clock"][0]
    quotes = [
        dict(
            identity=f"101:{10000 + i}",
            name="Cockpit Brave Bull",
            status="CANDIDATE",
            observed_at=now.isoformat(),
        )
        for i in range(1000)
    ]
    with ctx["factory"]() as session:
        record_capture(
            session,
            value,
            dict(
                id="synthetic",
                term="Cockpit simulador Brave Bull",
                state="SUCCEEDED",
                capture={"comparison": {"candidates": quotes}},
            ),
            now,
        )
        session.commit()
    with ctx["factory"]() as session:
        assert (
            session.scalar(select(func.count()).select_from(candidates(value).subquery())) == 1000
        )
        later = now + timedelta(days=1)
        first = refresh_candidate(session, value, later)
        second = refresh_candidate(session, value, later)
        assert first["identity"] != second["identity"]


def test_provider_spacing_429_and_fresh_cross_day_revalidation(context):
    ctx = context
    identifier, digest = configure(ctx, max_queries=1)
    announcements.links(ctx, (1, 2, 3))
    daily.activate(ctx, identifier, digest)
    open_day(ctx, identifier)
    original = ctx["pilot"]._telegram_guard.delegate
    calls = []

    def telegram(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}
            )
        return original.handle_request(request)

    ctx["pilot"]._telegram_guard.delegate = httpx.MockTransport(telegram)
    worker = DailyWorker(ctx["pilot"])
    assert worker.step() == "RUNNING" and len(calls) == 1
    ctx["clock"][0] += timedelta(seconds=2)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING" and len(calls) == 1
    ctx["clock"][0] += timedelta(seconds=1)
    assert worker.step() == "RUNNING" and len(calls) == 2
    assert worker.step() == "RUNNING" and len(calls) == 2
    ctx["clock"][0] += timedelta(seconds=1)
    assert worker.step() == "RUNNING" and len(calls) == 3
    first = daily.report(ctx)
    assert first["counts"]["published"] == 2 and first["scope"]["plan"]["max_messages"] == 18000
    assert first["scope"]["plan"]["messages_per_window"] is None
    failed = next(r for r in first["reservations"] if r["state"] == "FAILED")
    # The old quote cannot be sent directly after the date changes.
    ctx["clock"][0] += timedelta(days=1)
    open_day(ctx, identifier)
    shop, item = failed["identity"].split(":")
    node = next(n for n in ctx["nodes"] if str(n["shopId"]) == shop and str(n["itemId"]) == item)
    ctx["nodes"] = [dict(node, price="101.00", priceMin="101.00", priceMax="101.00")]
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    second = daily.report(ctx)
    assert second["queries"][0]["refresh_identity"] == failed["identity"]
    assert second["counts"]["published"] == 1
    assert "101,00" in ctx["telegram_calls"][-1].get(
        "caption", ctx["telegram_calls"][-1].get("text")
    )


def test_shopee_retry_after_blocks_shared_pacing_without_replaying(context):
    ctx = context
    identifier, digest = configure(ctx, shopee_posts_per_minute=1)
    daily.activate(ctx, identifier, digest)
    open_day(ctx, identifier)
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"}, json={})

    ctx["pilot"]._shopee_guard.delegate = httpx.MockTransport(provider)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    ctx["clock"][0] += timedelta(seconds=61)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    assert len(requests) == 1 and len(daily.report(ctx)["queries"]) == 1
    with ctx["factory"]() as session:
        assert not shopee_ready(session, ctx["settings"], 1, ctx["clock"][0])
        assert shopee_ready(session, ctx["settings"], 1, ctx["clock"][0] + timedelta(seconds=60))


def test_shopee_minute_budget_is_shared_by_discovery_and_links(context):
    ctx = context
    identifier, digest = configure(ctx, max_queries=1, max_links=1, shopee_posts_per_minute=1)
    offline.listings(ctx, [1], verified=False)
    with ctx["factory"]() as session:
        record_account_evidence(
            session, ctx["settings"], account.account_document(ctx), now=ctx["clock"][0]
        )
        session.commit()
    original = ctx["pilot"]._shopee_guard.delegate
    links = []

    def provider(request):
        if b"generateShortLink" not in request.content:
            return original.handle_request(request)
        links.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "generateShortLink": {
                        "shortLink": "https://s.shopee.com.br/continuousSynthetic",
                        "longLink": "https://shopee.com.br/product/101/201",
                    }
                }
            },
        )

    ctx["pilot"]._shopee_guard.delegate = httpx.MockTransport(provider)
    daily.activate(ctx, identifier, digest)
    open_day(ctx, identifier)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING" and not links
    ctx["clock"][0] += timedelta(seconds=61)
    assert DailyWorker(ctx["pilot"]).step() == "RUNNING"
    result = daily.report(ctx)
    assert len(result["queries"]) == len(links) == result["counts"]["published"] == 1
