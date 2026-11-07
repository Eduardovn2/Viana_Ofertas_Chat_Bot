"""Fictitious pipeline exercises A-H; every Shopee HTTP is a mock."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from threading import Event

import httpx
import pytest
import test_discovery_operations as operations
import test_shopee_discovery as discovery
from pydantic import SecretStr
from sqlalchemy import func, select

from app.integrations.errors import InvalidPayload
from app.integrations.http import JsonTransport
from app.integrations.shopee import ShopeeConnector
from app.models import (
    AffiliateLink,
    AppSetting,
    Coupon,
    CouponDiscountType,
    CouponValidationState,
    IntegrationEvent,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationQueueItem,
    QueueStatus,
)
from app.services.affiliate_pipeline import (
    PREFIX,
    AffiliatePipelineService,
    MockShopeeLinkGenerator,
)

context = discovery.context
no_network = discovery.no_network


@pytest.fixture
def pipeline_context(context):
    yield context
    if "affiliate_http" in context:
        context["affiliate_http"].close()


def ready(ctx, monkeypatch, *, keep_link=False):
    destination_id = discovery.verified_fictional_setup(ctx, monkeypatch)
    with ctx["factory"]() as session:
        product = session.scalar(select(Product))
        if not keep_link:
            session.delete(product.affiliate_link)  # Synthetic fixture only.
        product_id = product.id
        history_id = session.scalar(
            select(ProductPriceHistory.id).order_by(ProductPriceHistory.id.desc())
        )
        session.commit()
    operations.update(ctx, {"affiliate_links_simulation_enabled": True})
    ctx.update(product_id=product_id, destination_id=destination_id, history_id=history_id)
    ctx.update(mutations=0, mutation_failure=None, mutation_hook=None)
    ctx["long_url"] = (
        "https://shopee.com.br/universal-link/SYNTHETIC-i.7001.1"
        "?utm_source=SYNTHETIC%2Fowner&sig=SYNTHETIC%2Bsignature&tag=a&tag=b"
    )

    def handler(request):
        ctx["mutations"] += 1
        assert request.method == "POST"
        query = json.loads(request.content)["query"]
        assert query.startswith("mutation { generateShortLink(")
        assert "productOfferV2" not in query and "subIds" not in query
        # The durable reservation and claim must be visible before mock I/O.
        with ctx["factory"]() as session:
            values = session.scalars(
                select(AppSetting.value).where(AppSetting.key.startswith(PREFIX))
            ).all()
            assert any(v["status"] == "CLAIMED" and v["attempts"] == 1 for v in values)
        if ctx["mutation_hook"]:
            ctx["mutation_hook"]()
        if ctx["mutation_failure"] == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC tracking must not escape")
        if ctx["mutation_failure"] == "graphql":
            return httpx.Response(200, json={"errors": [{"message": ctx["long_url"]}]})
        if isinstance(ctx["mutation_failure"], int):
            return httpx.Response(ctx["mutation_failure"], json={"error": ctx["long_url"]})
        return httpx.Response(
            200,
            json={
                "data": {
                    "generateShortLink": {
                        "shortLink": "https://s.shopee.com.br/SYNTHETIC-PIPELINE",
                        "longLink": ctx["long_url"],
                    }
                }
            },
        )

    ctx["affiliate_http"] = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    connector = ShopeeConnector(
        JsonTransport(client=ctx["affiliate_http"], min_interval=0),
        app_id=SecretStr("SYNTHETIC_APP"),
        secret=SecretStr("SYNTHETIC_SECRET"),
        enabled=True,
        short_links_enabled=True,
        clock=lambda: ctx["clock"][0],
    )
    gateway = MockShopeeLinkGenerator(connector)
    service = AffiliatePipelineService(
        ctx["factory"], ctx["settings"], gateway, clock=lambda: ctx["clock"][0]
    )
    ctx.update(gateway=gateway, affiliate_service=service)
    ctx["discovery"].affiliate_links = service
    return destination_id


def evaluate(ctx):
    run = discovery.execute(ctx, "EVALUATE")
    assert run["status"] == "SUCCEEDED", run
    return run["result"]["candidates"][0]


def process(ctx):
    return ctx["affiliate_service"].process(
        ctx["product_id"], ctx["destination_id"], ctx["history_id"]
    )


def attempts(ctx):
    with ctx["factory"]() as session:
        return session.scalars(
            select(AppSetting.value).where(AppSetting.key.startswith(PREFIX))
        ).all()


def test_a_preapproved_link_prepares_only_simulated_message(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch, keep_link=True)
    result = evaluate(ctx)
    assert result["decision"] == "MOCK_WOULD_APPROVE"
    assert "Publicidade" in result["preview"] and "90,00" in result["preview"]
    assert ctx["mutations"] == 0 and attempts(ctx) == []
    with ctx["factory"]() as session:
        row = session.scalar(select(PublicationQueueItem))
        assert row.is_simulated and row.status == QueueStatus.PENDING and row.attempts == 0
    assert result["publication_authorized"] is False and result["real_send_calls"] == 0


def test_b_no_comparable_history_never_calls_generator(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    with ctx["factory"]() as session:
        first = session.scalar(select(ProductPriceHistory).order_by(ProductPriceHistory.id))
        first.current_price = None
        session.commit()
    result = evaluate(ctx)
    assert result["decision"] == "BLOCKED"
    assert "FRESH_COMPARABLE_DROP_REQUIRED" in result["reasons"]
    assert ctx["mutations"] == 0 and attempts(ctx) == []


def test_c_matching_identity_preserves_links_but_blocks_account_association(
    pipeline_context, monkeypatch, caplog
):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    result = evaluate(ctx)
    link = result["affiliate_link"]
    assert link["status"] == "GENERATED"
    assert link["product_identity_verified"] is True and link["account_verified"] is False
    assert result["decision"] == "BLOCKED" and result["publication_authorized"] is False
    assert attempts(ctx)[0]["long_url"] == ctx["long_url"]
    assert "SYNTHETIC%2Bsignature" not in json.dumps(result) + caplog.text
    with ctx["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(AffiliateLink)) == 0
        assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 0
        events = session.scalars(
            select(IntegrationEvent).where(IntegrationEvent.operation == "affiliate_link_mock")
        ).all()
        assert [e.status for e in events] == ["RESERVED", "CLAIMED", "GENERATED"]
        assert all("http" not in e.source_reference for e in events)
    assert ctx["mutations"] == 1
    evaluate(ctx)
    assert ctx["mutations"] == 1 and len(attempts(ctx)) == 1


@pytest.mark.parametrize(
    "failure, status",
    [("timeout", "UNCERTAIN"), (503, "UNCERTAIN"), (403, "FAILED"), ("graphql", "FAILED")],
)
def test_d_uncertain_or_refused_result_is_consumed_without_retry(
    pipeline_context, monkeypatch, failure, status
):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    ctx["mutation_failure"] = failure
    result = evaluate(ctx)
    assert result["affiliate_link"]["status"] == status
    evaluate(ctx)
    assert ctx["mutations"] == 1
    assert attempts(ctx)[0]["attempts"] == 1
    assert "SYNTHETIC%2Bsignature" not in json.dumps(result)


@pytest.mark.parametrize(
    "suffix", ["SYNTHETIC-i.7001.2", "SYNTHETIC-i.7002.1", "SYNTHETIC-i.7001.1?variation_id=9"]
)
def test_e_different_product_or_returned_variant_is_rejected(pipeline_context, monkeypatch, suffix):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    ctx["long_url"] = "https://shopee.com.br/universal-link/" + suffix
    result = evaluate(ctx)
    assert result["affiliate_link"]["status"] == "FAILED"
    assert result["decision"] == "BLOCKED"
    evaluate(ctx)
    assert ctx["mutations"] == 1


def test_e_product_variant_not_supported_by_mutation_never_calls_generator(
    pipeline_context, monkeypatch
):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    with ctx["factory"]() as session:
        session.get(Product, ctx["product_id"]).variant_id = "SYNTHETIC-VARIANT"
        session.commit()
    result = evaluate(ctx)
    assert result["decision"] == "BLOCKED" and ctx["mutations"] == 0


def test_f_unverified_coupon_never_changes_price_or_preview(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch, keep_link=True)
    with ctx["factory"]() as session:
        product = session.get(Product, ctx["product_id"])
        session.add(
            Coupon(
                platform=Platform.SHOPEE,
                code="SYNTHETIC-UNVERIFIED",
                discount_type=CouponDiscountType.FIXED,
                discount_value=Decimal("89.00"),
                validation_state=CouponValidationState.UNVERIFIED,
                price_access=PriceAccess.PUBLIC,
                eligible_products=[product],
            )
        )
        session.commit()
    result = evaluate(ctx)
    assert result["decision"] == "MOCK_WOULD_APPROVE"
    assert "90,00" in result["preview"] and "SYNTHETIC-UNVERIFIED" not in result["preview"]
    assert "1,00" not in result["preview"] and ctx["mutations"] == 0


def test_g_concurrent_attempts_generate_once_and_cannot_approve_or_send(
    pipeline_context, monkeypatch
):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    entered, finish = Event(), Event()
    ctx["mutation_hook"] = lambda: (entered.set(), finish.wait(15))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process, ctx)
        assert entered.wait(10)
        try:
            second = pool.submit(process, ctx).result(timeout=10)
            assert second["status"] == "BUSY"
            assert discovery.execute(ctx, "EVALUATE")["status"] == "SUCCEEDED"
        finally:
            finish.set()
        assert first.result(timeout=15)["status"] == "GENERATED"
    assert ctx["mutations"] == 1 and len(attempts(ctx)) == 1
    with ctx["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 0


def test_h_exceptional_price_outside_window_does_not_bypass_safety(pipeline_context, monkeypatch):
    ctx = pipeline_context
    destination = ready(ctx, monkeypatch)
    operations.update(
        ctx,
        {"exceptional_enabled": True, "exceptional_min_discount": "10", "working_days": []},
        destination,
    )
    result = evaluate(ctx)
    assert result["opportunity_kind"] == "EXCEPTIONAL" and result["decision"] == "BLOCKED"
    assert "OUTSIDE_DESTINATION_WINDOW" in result["reasons"]
    assert ctx["mutations"] == 0 and result["publication_authorized"] is False


def test_mock_attribution_confirmation_still_only_creates_pending_simulation(
    pipeline_context, monkeypatch
):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    original = ctx["gateway"].generate
    monkeypatch.setattr(
        ctx["gateway"], "generate", lambda origin: replace(original(origin), account_verified=True)
    )
    result = evaluate(ctx)
    assert result["affiliate_link"]["status"] == "ASSOCIATED"
    assert result["decision"] == "MOCK_WOULD_APPROVE" and result["publication_authorized"] is False
    assert ctx["mutations"] == 1
    evaluate(ctx)
    assert ctx["mutations"] == 1 and discovery.count(ctx, PublicationQueueItem) == 1


def test_persistence_failure_is_uncertain_and_never_repeats(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch)

    def fail(*args):
        raise RuntimeError(ctx["long_url"])

    monkeypatch.setattr(ctx["affiliate_service"], "_save_result", fail)
    assert evaluate(ctx)["affiliate_link"]["status"] == "UNCERTAIN"
    evaluate(ctx)
    assert ctx["mutations"] == 1 and attempts(ctx)[0]["attempts"] == 1


@pytest.mark.parametrize(
    "flag",
    [
        "shopee_enabled",
        "mercado_livre_enabled",
        "telegram_publisher_enabled",
        "telegram_real_sends_authorized",
        "simulation_automation_enabled",
        "shopee_short_links_enabled",
    ],
)
def test_real_flags_fail_closed_before_reservation(pipeline_context, monkeypatch, flag):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    setattr(ctx["settings"], flag, True)
    with pytest.raises(ValueError):
        process(ctx)
    assert ctx["mutations"] == 0 and attempts(ctx) == []


def test_plain_transport_is_rejected_before_mock_or_live_http(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    with httpx.Client(transport=httpx.HTTPTransport()) as client:
        monkeypatch.setattr(ctx["gateway"].connector.transport, "client", client)
        with pytest.raises(ValueError):
            process(ctx)
    assert ctx["mutations"] == 0 and attempts(ctx) == []


@pytest.mark.parametrize(
    "changes",
    [{"affiliate_links_simulation_enabled": False}, {"max_affiliate_links_per_evaluation": 0}],
)
def test_preferences_disable_generation_without_enabling_any_real_control(
    pipeline_context, monkeypatch, changes
):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    operations.update(ctx, changes)
    assert evaluate(ctx)["decision"] == "BLOCKED"
    assert ctx["mutations"] == 0
    assert all(
        not getattr(ctx["settings"], name)
        for name in [
            "shopee_enabled",
            "telegram_publisher_enabled",
            "telegram_real_sends_authorized",
            "simulation_automation_enabled",
        ]
    )


@pytest.mark.parametrize("scope", ["origin", "destination"])
def test_idempotency_scope_preserves_each_origin_and_destination(
    pipeline_context, monkeypatch, scope
):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    assert process(ctx)["status"] == "GENERATED"
    assert process(ctx)["reused"] is True
    assert ctx["mutations"] == 1
    if scope == "destination":
        ctx["destination_id"] = operations.destination(ctx, "SYNTHETIC-SECOND")
    else:
        with ctx["factory"]() as session:
            session.get(
                Product, ctx["product_id"]
            ).original_url = "https://shopee.com.br/SYNTHETIC-i.7001.1"
            session.commit()
    assert process(ctx)["status"] == "GENERATED"
    assert len(attempts(ctx)) == 2 and ctx["mutations"] == 2
    assert all(len(a["payload_sha256"]) == 64 for a in attempts(ctx))


def test_abandoned_claim_is_uncertain_without_repeating_mock(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    process(ctx)
    with ctx["factory"]() as session:
        row = session.scalar(select(AppSetting).where(AppSetting.key.startswith(PREFIX)))
        row.value = {**row.value, "status": "CLAIMED"}
        session.commit()
    assert process(ctx)["status"] == "UNCERTAIN"
    assert process(ctx)["reused"] is True and ctx["mutations"] == 1


def test_revalidation_after_mock_rejects_changed_destination(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch)

    def pause():
        with ctx["factory"]() as session:
            session.get(discovery.PublicationDestination, ctx["destination_id"]).enabled = False
            session.commit()

    ctx["mutation_hook"] = pause
    result = evaluate(ctx)
    assert result["decision"] == "BLOCKED"
    assert result["affiliate_link"]["status"] == "FAILED"
    assert ctx["mutations"] == 1
    with ctx["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(AffiliateLink)) == 0


def test_generation_respects_three_hour_cooldown_and_requires_new_observation(
    pipeline_context, monkeypatch
):
    ctx = pipeline_context
    ready(ctx, monkeypatch, keep_link=True)
    evaluate(ctx)
    with ctx["factory"]() as session:
        item = session.scalar(select(PublicationQueueItem))
        item.status, item.active_slot, item.published_at = (
            QueueStatus.PUBLISHED,
            None,
            ctx["clock"][0],
        )
        session.delete(session.get(Product, ctx["product_id"]).affiliate_link)
        session.commit()
    with pytest.raises(InvalidPayload):
        process(ctx)
    ctx["clock"][0] += timedelta(hours=3, seconds=1)
    with pytest.raises(InvalidPayload):
        process(ctx)
    assert ctx["mutations"] == 0 and attempts(ctx) == []


def test_unapproved_manual_link_is_preserved_without_new_generation(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch, keep_link=True)
    with ctx["factory"]() as session:
        link = session.scalar(select(AffiliateLink))
        link.source = "MANUAL_SHOPEE"
        before = link.url
        session.commit()
    assert evaluate(ctx)["affiliate_link"]["error_code"] == "EXISTING_LINK_REVIEW_REQUIRED"
    assert ctx["mutations"] == 0
    with ctx["factory"]() as session:
        assert session.scalar(select(AffiliateLink.url)) == before


def test_preferences_alone_cannot_install_or_authorize_generator(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    ctx["discovery"].affiliate_links = None
    assert evaluate(ctx)["decision"] == "BLOCKED"
    assert ctx["mutations"] == 0 and attempts(ctx) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"max_affiliate_links_per_evaluation": -1},
        {"max_affiliate_links_per_evaluation": 11},
        {"max_affiliate_links_per_evaluation": "1"},
        {"max_affiliate_links_per_evaluation": True},
        {"affiliate_links_simulation_enabled": "true"},
        {"affiliate_links_simulation_enabled": 1},
    ],
)
def test_new_preferences_are_strict_and_invalid_edit_is_atomic(
    pipeline_context, monkeypatch, changes
):
    ctx = pipeline_context
    ready(ctx, monkeypatch)
    before = ctx["discovery"].preferences()
    with pytest.raises(ValueError):
        operations.update(ctx, changes)
    assert ctx["discovery"].preferences() == before
    assert ctx["mutations"] == 0 and attempts(ctx) == []


def test_interruption_after_claim_is_uncertain_and_never_retries(pipeline_context, monkeypatch):
    ctx = pipeline_context
    ready(ctx, monkeypatch)

    def interrupt(_origin):
        raise KeyboardInterrupt

    monkeypatch.setattr(ctx["affiliate_service"].generator, "generate", interrupt)
    with pytest.raises(KeyboardInterrupt):
        process(ctx)
    assert attempts(ctx)[0]["status"] == "UNCERTAIN"
    assert attempts(ctx)[0]["attempts"] == 1
    assert process(ctx)["status"] == "UNCERTAIN"
    assert ctx["mutations"] == 0
