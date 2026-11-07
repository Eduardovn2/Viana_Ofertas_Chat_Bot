"""Assisted production workflow, entirely synthetic in UUID PostgreSQL test schemas."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event

import httpx
import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_commercial_handoff as handoff
import test_shopee_evidence_review as reviews
import test_shopee_multistore as multistore
import test_shopee_multistore_dispatch as discovery
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.integrations.errors import IntegrationError
from app.integrations.telegram import TelegramPublisher
from app.main import create_app
from app.models import Offer, Product, ProductPriceHistory, PublicationLog, TelegramPublication
from app.services.manual_offers import create_manual_offer
from app.services.shopee_evidence_review import ShopeeEvidenceReview
from app.services.telegram_publication import TelegramPublicationService

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres
HEADERS = {"X-API-Key": "synthetic-e2e-administrative-key"}
CHAT = "-1009999999999"
TOKEN = "123456789:SYNTHETIC_ONLY_NOT_A_LIVE_TOKEN_123456789"


@pytest.fixture
def flow(categories_postgres, monkeypatch):
    import app.api.manual_offers as routes

    engine, url = categories_postgres
    ctx = multistore.setup(collection.setup(engine, url.render_as_string(hide_password=False)))
    ctx["send_calls"] = []
    ctx["send_failure"] = None

    def telegram_mock(request):
        assert request.method == "POST" and request.url.path.endswith("/sendMessage")
        body = json.loads(request.content)
        assert body["chat_id"] == CHAT
        # The reservation and attempt must be visible in a different transaction
        # before even the simulated Telegram transport is entered.
        with ctx["factory"]() as session:
            entry = session.scalar(
                select(TelegramPublication).where(TelegramPublication.status == "SENDING")
            )
            assert entry is not None and entry.active_slot == 1
            log = session.scalar(
                select(PublicationLog).where(
                    PublicationLog.offer_id == entry.offer_id,
                    PublicationLog.attempt_number == entry.attempt_number,
                )
            )
            assert log is not None and log.status.value == "PENDING"
            history = session.get(
                ProductPriceHistory, session.get(Offer, entry.offer_id).price_history_id
            )
            assert history.evidence_metadata["multistore_context"]["identity"] == "102:203"
            assert (
                history.evidence_metadata["multistore_registered_variant_id"]
                == "synthetic-blue-sku"
            )
        ctx["send_calls"].append(body)
        if ctx.get("send_hook"):
            ctx["send_hook"]()
        if ctx["send_failure"] == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 700 + len(ctx["send_calls"]),
                    "chat": {"id": int(CHAT), "type": "group"},
                },
            },
        )

    # These settings exist only in this fixture and its MockTransport publisher.
    # Application/production flags and all environment credentials remain untouched.
    isolated = ctx["settings"].model_copy(
        update={
            "telegram_publisher_enabled": True,
            "telegram_real_sends_authorized": True,
            "telegram_bot_token": SecretStr(TOKEN),
            "telegram_allowed_chat_ids": (CHAT,),
        }
    )
    app_settings = ctx["settings"].model_copy(
        update={"admin_api_key": SecretStr(HEADERS["X-API-Key"])}
    )
    app = create_app(app_settings)
    ctx["app"] = app
    with httpx.Client(transport=httpx.MockTransport(telegram_mock), trust_env=False) as transport:
        ctx["publication"] = TelegramPublicationService(
            isolated,
            publisher=TelegramPublisher(isolated, client=transport),
            clock=lambda: ctx["clock"][0],
        )
        monkeypatch.setattr(
            routes, "TelegramPublicationService", lambda settings: ctx["publication"]
        )

        def fixed_create(session, settings, **kwargs):
            return create_manual_offer(session, settings, now=ctx["clock"][0], **kwargs)

        monkeypatch.setattr(routes, "create_manual_offer", fixed_create)
        try:
            with TestClient(
                app, base_url="http://localhost", client=("127.0.0.1", 50000)
            ) as client:
                app.state.session_factory = ctx["factory"]
                app.state.shopee_discovery = ctx["discovery"]
                app.state.shopee_collection = ctx["service"]
                ctx["client"] = client
                yield ctx
        finally:
            ctx["transport"].close()
            app.state.engine.dispose()


def post(ctx, path, body, status=200):
    response = ctx["client"].post(path, json=body, headers=HEADERS)
    assert response.status_code == status, response.text
    return response.json()


def discovered(ctx):
    ctx["plan"] = discovery.ready(ctx)  # New synthetic UUID, sealed/approved in test schema only.
    result = discovery.execute(ctx, ctx["plan"])
    assert result["status"] == "SUCCEEDED" and ctx["calls"] == 1
    ctx["reviews"] = ShopeeEvidenceReview(ctx["service"])
    ctx["base"] = "/api/v1/shopee-discovery/multistore-searches/" + ctx["plan"]["authorization_id"]
    initial = post(ctx, ctx["base"] + "/opportunities", {})
    assert initial["proven_comparisons"] == []
    assert initial["counts"]["human_review_required"] == 2
    assert collection.count(ctx, ProductPriceHistory) == 0


def document(ctx):
    for shop, item in (("101", "202"), ("102", "203")):
        path = ctx["base"] + f"/reviews/{shop}/{item}"
        response = ctx["client"].put(
            path,
            json=reviews.edit_data(ctx, shop, item),
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        draft = response.json()
        body = dict(
            actor="synthetic_reviewer",
            expected_revision=draft["revision"],
            confirmed_listing_binding=False,
        )
        post(ctx, path + "/confirm", body, status=422)
        body["confirmed_listing_binding"] = True
        assert post(ctx, path + "/confirm", body)["state"] == "DOCUMENTED"
    view = post(ctx, ctx["base"] + "/opportunities", {"target": multistore.FACTS})
    assert view["counts"]["human_review_required"] == 0
    assert view["counts"]["automatic_compared"] == 2
    assert Decimal(view["proven_comparisons"][0]["spread_amount"]) == Decimal("100.00")


def register(ctx, *, status=201, **changes):
    preview = post(
        ctx, ctx["base"] + "/candidates/102/203/commercial-preview", {"target": multistore.FACTS}
    )
    assert preview["final_delivered_price"] is preview["historical_discount"] is None
    assert not preview["publication_authorized"]
    if "product_id" not in ctx:
        ctx["product_id"] = handoff.product(ctx)
        ctx["destination_id"] = handoff.destination(ctx)
        with ctx["factory"]() as session:
            session.get(Product, ctx["product_id"]).variant_id = "synthetic-blue-sku"
            session.commit()
    data = (
        dict(
            product_id=ctx["product_id"],
            current_price="1000.00",
            actor="synthetic_operator",
            verified=True,
            availability="AVAILABLE",
            price_access="PUBLIC",
            multistore_context=preview["next_step"]["multistore_context"],
        )
        | changes
    )
    return post(ctx, "/api/v1/manual-offers", data, status=status)


def approved(ctx):
    discovered(ctx)
    # A high-scoring title is insufficient to create a bound commercial offer.
    register(ctx, status=409)
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 0
    document(ctx)
    offer = register(ctx)
    assert offer["coupon_id"] is offer["final_price"] is offer["previous_price"] is None
    assert offer["historical_price"] is offer["discount_amount"] is None
    assert offer["status"] == "PENDING_REVIEW"
    ctx["offer"] = offer
    path = f"/api/v1/manual-offers/{offer['id']}/preview"
    entry = post(ctx, path, {"destination_id": ctx["destination_id"]})
    assert post(ctx, path, {"destination_id": ctx["destination_id"]})["id"] == entry["id"]
    ctx["entry"] = entry
    with pytest.raises(IntegrationError) as unapproved:
        ctx["publication"].publish(ctx["factory"], entry["id"], entry["preview_hash"])
    assert unapproved.value.code == "OUTBOX_NOT_APPROVED" and not ctx["send_calls"]
    approval_path = f"/api/v1/telegram/publications/{entry['id']}/approve"
    body = dict(actor="synthetic_approver", confirmed=False, preview_hash=entry["preview_hash"])
    post(ctx, approval_path, body, status=422)
    body["confirmed"] = True
    assert post(ctx, approval_path, body)["status"] == "APPROVED"
    assert post(ctx, approval_path, body)["approved_by"] == "synthetic_approver"
    with ctx["factory"]() as session:
        row = session.get(TelegramPublication, entry["id"])
        assert row.preview_payload["product"]["external_id"] == "102:203"
        assert row.preview_payload["product"]["variant_id"] == "synthetic-blue-sku"
        assert Decimal(row.preview_payload["offer"]["current_price"]) == Decimal("1000.00")
        assert row.preview_payload["offer"]["previous_price"] is None
    return entry


def test_complete_multistore_workflow_without_coupon_and_no_duplicate(flow):
    ctx = flow
    entry = approved(ctx)
    result = ctx["publication"].publish(ctx["factory"], entry["id"], entry["preview_hash"])
    assert result.status.value == "SENT" and result.message_id == "701"
    assert result.approved_by == "synthetic_approver"
    assert "1.000,00" in ctx["send_calls"][0]["text"]
    assert "1.100,00" not in ctx["send_calls"][0]["text"]
    assert ctx["publication"].publish(ctx["factory"], entry["id"]).status.value == "SENT"
    assert len(ctx["send_calls"]) == ctx["calls"] == 1
    assert collection.count(ctx, PublicationLog) == 1
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 1
    assert not ctx["app"].state.settings.telegram_publisher_enabled
    assert not ctx["app"].state.settings.telegram_real_sends_authorized
    assert not ctx["app"].state.settings.scheduler_enabled


@pytest.mark.parametrize("mutation", ["observation", "document", "variant", "price"])
def test_changed_evidence_after_approval_never_reaches_transport(flow, mutation):
    ctx = flow
    entry = approved(ctx)
    if mutation == "observation":
        handoff.change_observation(ctx)
    elif mutation == "document":
        path = ctx["base"] + "/reviews/102/203"
        response = ctx["client"].put(
            path, json=reviews.edit_data(ctx, "102", "203"), headers=HEADERS
        )
        assert response.status_code == 200 and response.json()["state"] == "DRAFT"
    else:
        with ctx["factory"]() as session:
            if mutation == "variant":
                session.get(Product, ctx["product_id"]).variant_id = "other-sku"
            else:
                session.get(Offer, ctx["offer"]["id"]).current_price = Decimal("1.00")
            session.commit()
    with pytest.raises(IntegrationError):
        ctx["publication"].publish(ctx["factory"], entry["id"], entry["preview_hash"])
    assert not ctx["send_calls"] and collection.count(ctx, PublicationLog) == 0
    with ctx["factory"]() as session:
        row = session.get(TelegramPublication, entry["id"])
        assert row.status.value == "EXPIRED" and row.approved_by is None


def test_cooldown_requires_three_hours_and_new_commercial_observation(flow):
    ctx = flow
    entry = approved(ctx)
    ctx["publication"].publish(ctx["factory"], entry["id"])
    path = f"/api/v1/manual-offers/{ctx['offer']['id']}/preview"
    assert (
        post(ctx, path, {"destination_id": ctx["destination_id"]}, status=409)["detail"]["code"]
        == "OUTBOX_COOLDOWN"
    )
    ctx["clock"][0] += timedelta(hours=3, seconds=-1)
    new_offer = register(ctx)
    path = f"/api/v1/manual-offers/{new_offer['id']}/preview"
    assert (
        post(ctx, path, {"destination_id": ctx["destination_id"]}, status=409)["detail"]["code"]
        == "OUTBOX_COOLDOWN"
    )
    ctx["clock"][0] += timedelta(seconds=1)
    new_entry = post(ctx, path, {"destination_id": ctx["destination_id"]})
    assert new_entry["id"] != entry["id"] and new_entry["status"] == "PENDING"
    with pytest.raises(IntegrationError):
        ctx["publication"].publish(ctx["factory"], new_entry["id"])
    assert len(ctx["send_calls"]) == 1 and ctx["calls"] == 1


def test_concurrent_dispatch_cannot_duplicate_simulated_message(flow):
    ctx = flow
    entry = approved(ctx)
    entered, release = Event(), Event()

    def hold():
        entered.set()
        assert release.wait(10)

    ctx["send_hook"] = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(ctx["publication"].publish, ctx["factory"], entry["id"])
        try:
            assert entered.wait(10)
            second = ctx["publication"].publish(ctx["factory"], entry["id"])
            assert second.status.value == "SENDING"
        finally:
            release.set()
        assert first.result(timeout=10).status.value == "SENT"
    assert len(ctx["send_calls"]) == collection.count(ctx, PublicationLog) == 1


def test_uncertain_simulated_send_never_retries_or_releases_duplicate_slot(flow):
    ctx = flow
    entry = approved(ctx)
    ctx["send_failure"] = "timeout"
    first = ctx["publication"].publish(ctx["factory"], entry["id"])
    assert first.status.value == "UNCERTAIN" and first.active_slot == 1
    assert ctx["publication"].publish(ctx["factory"], entry["id"]).status.value == "UNCERTAIN"
    path = f"/api/v1/manual-offers/{ctx['offer']['id']}/preview"
    assert (
        post(ctx, path, {"destination_id": ctx["destination_id"]}, status=409)["detail"]["code"]
        == "OUTBOX_BLOCKED"
    )
    assert len(ctx["send_calls"]) == collection.count(ctx, PublicationLog) == 1
