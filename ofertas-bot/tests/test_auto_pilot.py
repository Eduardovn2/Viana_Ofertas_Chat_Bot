"""AUTO_PILOT queue, rolling cadence and budgets; only fictitious evidence/MockTransport."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

import httpx
import pytest
import test_shopee_collection as collection
import test_shopee_multistore as multi
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.integrations.telegram import TelegramPublisher
from app.models import (
    AffiliateLink,
    AppSetting,
    Availability,
    CategoryMode,
    DestinationChannel,
    Offer,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    TelegramPublication,
)
from app.services.auto_pilot import PREFIX, AutoPilotPlan, AutoPilotService
from app.services.manual_offers import create_manual_offer

base_context = collection.context
no_network = collection.no_network
application = collection.application
client = collection.client
QUERY = "Cockpit simulador Brave Bull"
CHAT = "-1009999999999"


def setup(ctx):
    ctx = multi.setup(ctx)
    with ctx["factory"]() as session:
        destination = PublicationDestination(
            name="FICTICIO AUTO_PILOT",
            external_id=CHAT,
            enabled=True,
            channel=DestinationChannel.TELEGRAM,
            category_mode=CategoryMode.ALL,
            min_interval_seconds=1800,
            max_messages_per_hour=20,
        )
        session.add(destination)
        session.commit()
        ctx["destination_id"] = destination.id
    ctx["telegram_calls"] = []
    ctx["telegram_failure"] = False

    def reply(request):
        with ctx["factory"]() as session:
            pilot = session.scalar(
                select(AppSetting.value).where(AppSetting.key == PREFIX + ctx["pilot_id"])
            )
            assert any(r["state"] == "CLAIMED" and r["http_started"] for r in pilot["reservations"])
            assert session.scalar(select(PublicationLog).where(PublicationLog.status == "PENDING"))
            assert session.scalar(
                select(TelegramPublication).where(TelegramPublication.status == "SENDING")
            )
        ctx["telegram_calls"].append(json.loads(request.content))
        if ctx["telegram_failure"]:
            raise httpx.ReadTimeout("FICTICIO timeout")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": len(ctx["telegram_calls"]),
                    "chat": {"id": int(CHAT), "type": "group"},
                },
            },
        )

    ctx["telegram_client"] = httpx.Client(transport=httpx.MockTransport(reply), trust_env=False)
    sender_settings = ctx["settings"].model_copy(
        update={
            "telegram_publisher_enabled": True,
            "telegram_real_sends_authorized": True,
            "telegram_allowed_chat_ids": (CHAT,),
            "telegram_bot_token": SecretStr("123456789:SYNTHETIC_AUTO_PILOT_ONLY_NOT_REAL_TOKEN"),
        }
    )
    ctx["publisher"] = TelegramPublisher(sender_settings, client=ctx["telegram_client"])
    ctx["pilot"] = AutoPilotService(
        ctx["factory"],
        ctx["settings"],
        discovery=ctx["discovery"],
        publisher=ctx["publisher"],
        clock=lambda: ctx["clock"][0],
    )
    return ctx


@pytest.fixture
def context(base_context):
    ctx = setup(base_context)
    yield ctx
    ctx["telegram_client"].close()


def prepare(ctx, **changes):
    plan = dict(
        authorization_id=str(uuid4()),
        actor="synthetic_owner",
        destination_id=ctx["destination_id"],
        not_before=ctx["clock"][0],
        expires_at=ctx["clock"][0] + timedelta(hours=2),
        terms=[QUERY, "Cockpit Brave Bull simulador"],
        operating_policy=None,  # Exercise compatibility with existing signed grants.
        **changes,
    )
    report = ctx["pilot"].prepare(plan)
    ctx["pilot_id"] = report["authorization_id"]
    ctx["pilot"].start_offline(ctx["pilot_id"], report["scope_sha256"])
    return report


def listings(ctx, numbers, *, verified=True):
    ctx["nodes"] = []
    for number in numbers:
        name = QUERY + f" modelo FICTICIO-{number}"
        ctx["nodes"].append(multi.node(100 + number, 200 + number, "100.00", name))
        if not verified:
            continue
        with ctx["factory"]() as session:
            product = Product(
                platform=Platform.SHOPEE,
                external_id=f"{100 + number}:{200 + number}",
                variant_id="synthetic-sku-blue",
                name=name,
                original_url=f"https://shopee.com.br/product/{100 + number}/{200 + number}",
                monitoring_enabled=False,
            )
            session.add(product)
            session.flush()
            session.add(
                AffiliateLink(
                    product_id=product.id,
                    url="https://s.shopee.com.br/syntheticFixture",
                    source="MANUAL_SHOPEE_VERIFIED",
                )
            )
            session.flush()
            session.expire(product, ["affiliate_link"])
            create_manual_offer(
                session,
                ctx["settings"],
                product_id=product.id,
                current_price=Decimal("100.00"),
                actor="synthetic_commercial_verifier",
                verified=True,
                availability=Availability.AVAILABLE,
                price_access=PriceAccess.PUBLIC,
                now=ctx["clock"][0],
                documented_identity=dict(
                    identity=product.external_id,
                    variant_id=product.variant_id,
                    original_url_sha256=fingerprint({"original_url": product.original_url}),
                    source="MANUAL_DOCUMENTED",
                    actor="synthetic_document_reviewer",
                    reference="FICTICIO documento confirmado da fixture",
                    source_sha256=fingerprint({"fictitious_document": number}),
                    verified_at=ctx["clock"][0].isoformat(),
                    confirmed_listing_binding=True,
                    facts=dict(multi.FACTS, model=f"modelo FICTICIO-{number}"),
                ),
            )
            session.commit()


def test_batch_five_without_fixed_gap_and_rolling_window(context):
    ctx = context
    prepare(ctx)
    listings(ctx, [1, 2, 3])
    ctx["pilot"].discover_once(ctx["pilot_id"])
    listings(ctx, [4, 5, 6])
    ctx["pilot"].discover_once(ctx["pilot_id"])
    result = ctx["pilot"].dispatch_batch(ctx["pilot_id"])
    assert result["counts"]["published"] == 5 and len(ctx["telegram_calls"]) == 5
    assert result["counts"]["automatically_approved"] == 5
    assert ctx["pilot"].dispatch_batch(ctx["pilot_id"])["counts"]["published"] == 5
    queries_before = ctx["calls"]
    assert ctx["pilot"].tick(ctx["pilot_id"])["counts"]["published"] == 5
    assert ctx["calls"] == queries_before  # Full window must not burn query budget.
    ctx["clock"][0] += timedelta(seconds=599)
    assert ctx["pilot"].dispatch_batch(ctx["pilot_id"])["counts"]["published"] == 5
    ctx["clock"][0] += timedelta(seconds=1)
    # New, explicitly verified fixture evidence for different products. The
    # controller must never refresh an old attestation or price by itself.
    listings(ctx, [7, 8, 9])
    ctx["pilot"].discover_once(ctx["pilot_id"])
    result = ctx["pilot"].dispatch_batch(ctx["pilot_id"])
    assert result["counts"]["published"] == 8
    assert any(q.get("reason") == "MANUAL_EVIDENCE_STALE" for q in result["queue"])
    with ctx["factory"]() as session:
        assert (
            session.get(PublicationDestination, ctx["destination_id"]).min_interval_seconds == 1800
        )
        assert all(
            p.approved_by.startswith("AUTO_PILOT_RULES:")
            for p in session.scalars(select(TelegramPublication))
        )
        assert all(
            o.coupon_id is None and o.previous_price is None for o in session.scalars(select(Offer))
        )


def test_message_budget_and_idempotent_dispatch(context):
    ctx = context
    prepare(ctx, max_messages=2)
    listings(ctx, [1, 2, 3])
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["state"] == "COMPLETED" and result["reason"] == "MESSAGE_BUDGET"
    assert result["counts"]["published"] == 2
    for _ in range(2):
        ctx["pilot"].tick(ctx["pilot_id"])
    assert len(ctx["telegram_calls"]) == 2 and ctx["calls"] == 1


def test_twenty_messages_end_the_default_pilot(context):
    ctx = context
    prepare(ctx)
    for window in range(4):
        if window:
            ctx["clock"][0] += timedelta(seconds=600)
        start = window * 6 + 1
        for first in (start, start + 3):
            listings(ctx, range(first, first + 3))
            ctx["pilot"].discover_once(ctx["pilot_id"])
        result = ctx["pilot"].dispatch_batch(ctx["pilot_id"])
        assert result["counts"]["published"] == (window + 1) * 5
    assert result["state"] == "COMPLETED" and result["reason"] == "MESSAGE_BUDGET"
    assert result["counts"]["messages_reserved"] == 20 and ctx["calls"] == 8
    ctx["pilot"].tick(ctx["pilot_id"])
    assert len(ctx["telegram_calls"]) == 20 and ctx["calls"] == 8


def test_no_verified_offers_means_zero_publications(context):
    ctx = context
    prepare(ctx)
    listings(ctx, [1, 2, 3], verified=False)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["counts"]["inconclusive"] == 3
    assert result["counts"]["published"] == result["counts"]["automatically_approved"] == 0
    assert not ctx["telegram_calls"]
    assert collection.count(ctx, ProductPriceHistory) == collection.count(ctx, Offer) == 0


def test_uncertain_never_retries_and_keeps_outbox_slot(context):
    ctx = context
    prepare(ctx)
    listings(ctx, [1])
    ctx["telegram_failure"] = True
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["counts"]["uncertain"] == 1
    ctx["pilot"].tick(ctx["pilot_id"])
    assert len(ctx["telegram_calls"]) == 1
    with ctx["factory"]() as session:
        entry = session.scalar(select(TelegramPublication))
        assert entry.status.value == "UNCERTAIN" and entry.active_slot == 1


def test_deadline_and_admin_stop_are_terminal(context):
    ctx = context
    prepare(ctx)
    listings(ctx, [1])
    ctx["pilot"].discover_once(ctx["pilot_id"])
    ctx["pilot"].stop(ctx["pilot_id"])
    ctx["clock"][0] += timedelta(hours=2)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["state"] == "STOPPED" and result["reason"] == "ADMIN_STOP"
    assert not ctx["telegram_calls"] and ctx["calls"] == 1


def test_deadline_without_stop_prevents_queries_and_sends(context):
    ctx = context
    prepare(ctx)
    ctx["clock"][0] += timedelta(hours=2)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["state"] == "COMPLETED" and result["reason"] == "DEADLINE"
    assert ctx["calls"] == 0 and not ctx["telegram_calls"]


def test_query_timeout_consumes_budget_no_retry(context):
    ctx = context
    prepare(ctx, max_queries=1)
    ctx["failure"] = "timeout"
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["reason"] == "QUERY_BUDGET"
    assert result["queries"][0]["state"] == "FAILED_OR_UNCERTAIN"
    ctx["pilot"].tick(ctx["pilot_id"])
    assert ctx["calls"] == 1 and not ctx["telegram_calls"]


@pytest.mark.parametrize("change", ["variant", "price", "link", "document", "document_changed"])
def test_evidence_changed_in_queue_is_not_auto_approved(context, change):
    ctx = context
    prepare(ctx)
    listings(ctx, [1])
    ctx["pilot"].discover_once(ctx["pilot_id"])
    with ctx["factory"]() as session:
        p = session.scalar(select(Product))
        if change == "variant":
            p.variant_id = ""
        elif change == "price":
            session.scalar(select(Offer)).current_price = Decimal("1.00")
        elif change == "link":
            p.affiliate_link.source = "MANUAL_SHOPEE"
        elif change == "document":
            session.scalar(select(ProductPriceHistory)).evidence_metadata = {}
        else:
            h = session.scalar(select(ProductPriceHistory))
            metadata = copy.deepcopy(h.evidence_metadata)
            metadata["documented_identity"]["reference"] = "FICTICIO documento modificado"
            h.evidence_metadata = metadata
        session.commit()
    result = ctx["pilot"].dispatch_batch(ctx["pilot_id"])
    assert result["counts"]["rejected"] >= 1 and not ctx["telegram_calls"]
    assert result["counts"]["automatically_approved"] == 0


def test_scope_mutation_and_uuid_reuse_block(context):
    ctx = context
    plan = prepare(ctx)
    spec = copy.deepcopy(plan["scope"]["plan"])
    spec["max_messages"] = 1
    with pytest.raises(ValueError):
        ctx["pilot"].prepare(spec)
    with pytest.raises(ValueError):
        ctx["pilot"].start_offline(ctx["pilot_id"], plan["scope_sha256"])
    with ctx["factory"]() as session:
        row = session.scalar(select(AppSetting).where(AppSetting.key == PREFIX + ctx["pilot_id"]))
        value = copy.deepcopy(row.value)
        value["scope"]["plan"]["terms"] = ["changed"]
        row.value = value
        session.commit()
    with pytest.raises(ValueError, match="Escopo"):
        ctx["pilot"].tick(ctx["pilot_id"])


def test_real_transport_and_real_database_are_blocked(context, monkeypatch):
    ctx = context
    prepare(ctx)
    monkeypatch.setattr(ctx["publisher"], "_client", None)
    with pytest.raises(ValueError, match="MockTransport"):
        ctx["pilot"].tick(ctx["pilot_id"])
    enabled = ctx["settings"].model_copy(update={"shopee_enabled": True})
    with pytest.raises(ValueError):
        AutoPilotService(ctx["factory"], enabled).prepare({})


def test_mounted_real_transport_cannot_bypass_mock_guard(context, monkeypatch):
    ctx = context
    prepare(ctx)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        mounts={"https://api.telegram.org": httpx.HTTPTransport()},
    ) as unsafe:
        monkeypatch.setattr(ctx["publisher"], "_client", unsafe)
        with pytest.raises(ValueError, match="MockTransport"):
            ctx["pilot"].tick(ctx["pilot_id"])
    assert not ctx["telegram_calls"] and ctx["calls"] == 0


def test_plan_limits(context):
    ctx = context
    plan = prepare(ctx)["scope"]["plan"]
    for changes in (
        {"max_messages": 21},
        {"messages_per_window": 6},
        {"page": 2},
        {"expires_at": ctx["clock"][0] + timedelta(hours=3)},
    ):
        with pytest.raises(ValidationError):
            AutoPilotPlan.model_validate(plan | changes)


def test_documentation_reuses_manual_creation_path_and_requires_explicit_confirmation(context):
    from app.services.product_documentation import DocumentedIdentity

    ctx = context
    listings(ctx, [1])
    with ctx["factory"]() as session:
        h = session.scalar(select(ProductPriceHistory))
        proof = copy.deepcopy(h.evidence_metadata["documented_identity"])
        assert proof["source"] == "MANUAL_DOCUMENTED" and proof["confirmed_listing_binding"] is True
        assert proof["facts"]["quantity"] == 1
    for confirmation in (False, 1, "true"):
        with pytest.raises(ValidationError):
            DocumentedIdentity.model_validate(proof | {"confirmed_listing_binding": confirmation})


def test_changed_destination_or_credentials_blocks_scope(context):
    ctx = context
    prepare(ctx)
    with ctx["factory"]() as session:
        session.get(PublicationDestination, ctx["destination_id"]).external_id = "-1008888888888"
        session.commit()
    with pytest.raises(ValueError, match="Destino"):
        ctx["pilot"].tick(ctx["pilot_id"])
    assert not ctx["telegram_calls"] and ctx["calls"] == 0


def test_cross_pilot_listing_cooldown(context):
    ctx = context
    prepare(ctx)
    listings(ctx, [1])
    ctx["pilot"].tick(ctx["pilot_id"])
    prepare(ctx)  # Independent mock authorization, not a consumed live UUID.
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["counts"]["published"] == 0
    assert result["queue"][0]["reason"] == "LISTING_DESTINATION_COOLDOWN"
    assert len(ctx["telegram_calls"]) == 1


def test_stop_after_reservation_prevents_discovery_http(context, monkeypatch):
    ctx = context
    prepare(ctx)
    original = ctx["pilot"]._reserve_query

    def reserve_and_stop(pilot_id):
        result = original(pilot_id)
        ctx["pilot"].stop(pilot_id)
        return result

    monkeypatch.setattr(ctx["pilot"], "_reserve_query", reserve_and_stop)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["state"] == "STOPPED" and result["counts"]["queries_reserved"] == 1
    assert ctx["calls"] == 0 and not ctx["telegram_calls"]


def test_concurrent_batch_has_one_sender(context, monkeypatch):
    ctx = context
    prepare(ctx)
    listings(ctx, [1])
    ctx["pilot"].discover_once(ctx["pilot_id"])
    entered, release = Event(), Event()
    original = ctx["publisher"].send

    def hold(chat, message):
        entered.set()
        assert release.wait(10)
        return original(chat, message)

    monkeypatch.setattr(ctx["publisher"], "send", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(ctx["pilot"].dispatch_batch, ctx["pilot_id"])
        try:
            assert entered.wait(10)
            second = ctx["pilot"].dispatch_batch(ctx["pilot_id"])
            assert second["counts"]["messages_reserved"] == 1
        finally:
            release.set()
        assert first.result(timeout=10)["counts"]["published"] == 1
    assert len(ctx["telegram_calls"]) == 1


def test_api_requires_auth_and_does_not_start_a_scheduler(context, application, client):
    ctx = context
    plan = prepare(ctx)
    application.state.auto_pilot = ctx["pilot"]
    path = "/api/v1/auto-pilots/" + ctx["pilot_id"]
    assert client.get(path).status_code == 401
    response = client.get(path, headers=collection.api.HEADERS)
    assert response.status_code == 200 and response.json()["scope_sha256"] == plan["scope_sha256"]
    assert application.state.monitoring._scheduler is None
