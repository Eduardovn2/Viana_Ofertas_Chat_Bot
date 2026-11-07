"""Synthetic commercial integration. Never imports to an operational database."""

import copy
from datetime import timedelta
from decimal import Decimal

import pytest
import test_shopee_collection as collection
import test_shopee_evidence_review as reviews
import test_shopee_multistore as multistore
import test_shopee_opportunities as opportunities
import test_shopee_replay as replay
from sqlalchemy import select

from app.integrations.errors import IntegrationError
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
    PublicationQueueItem,
    TelegramPublication,
)
from app.services.manual_offers import create_manual_offer, read_manual_offer
from app.services.shopee_multistore import ShopeeMultistoreAdapter
from app.services.telegram_publication import TelegramPublicationService

base_context = collection.context
no_network = collection.no_network
application = collection.application
client = collection.client


@pytest.fixture
def context(base_context):
    return multistore.setup(base_context)


def preview(ctx, identity="102:203", **changes):
    return ctx["discovery"].multistore_commercial_preview(
        ctx["plan"]["authorization_id"], identity, target=multistore.FACTS, **changes
    )


def product(ctx, identity="102:203"):
    with ctx["factory"]() as session:
        existing = session.scalar(
            select(Product).where(
                Product.platform == Platform.SHOPEE,
                Product.external_id == identity,
            )
        )
        if existing:
            return existing.id
        row = Product(
            platform=Platform.SHOPEE,
            external_id=identity,
            name="FICTICIO Cockpit Brave Bull",
            original_url="https://shopee.com.br/product/102/203",
            monitoring_enabled=False,
        )
        session.add(row)
        session.flush()
        session.add(
            AffiliateLink(
                product_id=row.id,
                url="https://s.shopee.com.br/syntheticFixture",
                source="MANUAL_SHOPEE_VERIFIED",
            )
        )
        session.commit()
        return row.id


def destination(ctx):
    with ctx["factory"]() as session:
        row = PublicationDestination(
            name="FICTICIO destino",
            channel=DestinationChannel.TELEGRAM,
            external_id="-1009999999999",
            enabled=True,
            category_mode=CategoryMode.ALL,
        )
        session.add(row)
        session.commit()
        return row.id


def register(ctx, **changes):
    data = (
        dict(
            product_id=product(ctx),
            current_price=Decimal("1000.00"),
            actor="synthetic_administrator",
            verified=True,
            availability=Availability.AVAILABLE,
            price_access=PriceAccess.PUBLIC,
            multistore_context=preview(ctx)["next_step"]["multistore_context"],
            now=ctx["clock"][0],
        )
        | changes
    )
    with ctx["factory"]() as session:
        bundle = create_manual_offer(session, ctx["settings"], **data)
        session.commit()
        return bundle.offer.id


def test_preview_is_read_only_informative_and_reuses_identity_evidence(context):
    ctx = opportunities.documented(context)
    before = ctx["discovery"].multistore_search_status(ctx["plan"]["authorization_id"])
    result = preview(ctx)
    assert result["commercial_handoff_selected"]
    assert result["name"] == multistore.QUERY
    assert result["seller"]["shop_name"] == "FICTICIA Loja 102"
    assert result["announced_price"] == Decimal("1000.00")
    assert result["attribute_states"]["brand"] == "DOCUMENTED"
    assert result["documentary_sources"]
    assert result["commercial_evidence"] is None
    assert result["final_delivered_price"] is result["historical_discount"] is None
    assert not result["can_create_offer"] and not result["can_approve_offer"]
    assert not result["publication_authorized"]
    assert result["next_step"]["endpoint"] == "/api/v1/manual-offers"
    assert len(result["preview_sha256"]) == 64
    assert result == preview(ctx)
    assert ctx["discovery"].multistore_search_status(ctx["plan"]["authorization_id"]) == before
    for model in (Offer, ProductPriceHistory, PublicationQueueItem, TelegramPublication):
        assert collection.count(ctx, model) == 0
    assert ctx["calls"] == 1


def test_partial_incompatible_and_nonselected_candidates_cannot_be_registered(context):
    ctx = reviews.setup(context)
    assert not preview(ctx)["commercial_handoff_selected"]
    assert preview(ctx)["missing_identity_evidence"]
    with pytest.raises(IntegrationError, match="prévia multiloja"):
        register(ctx)
    reviews.document(ctx)
    reviews.document(ctx, "102", "203")
    assert not preview(ctx, "101:202")["commercial_handoff_selected"]  # Higher price.
    assert not preview(ctx, "103:204")["commercial_handoff_selected"]  # Accessory.
    assert collection.count(ctx, Offer) == 0
    assert collection.count(ctx, ProductPriceHistory) == 0


def test_existing_manual_offer_reuses_commercial_evidence_without_publication(context):
    ctx = opportunities.documented(context)
    offer_id = register(ctx)
    result = preview(ctx, offer_id=offer_id)
    assert preview(ctx)["commercial_evidence"]["offer_id"] == offer_id
    evidence = result["commercial_evidence"]
    assert evidence["offer_id"] == offer_id and evidence["evidence_reused"]
    assert evidence["applicable_public_price"] == Decimal("1000.00")
    assert evidence["availability"] == "AVAILABLE" and evidence["price_access"] == "PUBLIC"
    assert evidence["coupon_adjusted_price"] is None
    assert result["next_step"]["endpoint"] == f"/api/v1/manual-offers/{offer_id}/preview"
    assert not result["next_step"]["explicit_commercial_verification_required"]
    assert not result["publication_authorized"] and result["blockers"]
    assert result["final_delivered_price"] is None and result["historical_discount"] is None
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 1
    assert (
        collection.count(ctx, TelegramPublication)
        == collection.count(ctx, PublicationQueueItem)
        == 0
    )
    assert ctx["calls"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"current_price": Decimal("999.00")},
        {"previous_price": Decimal("1100.00")},
        {"verified": False},
        {"availability": Availability.UNAVAILABLE},
        {"price_access": PriceAccess.EXCLUSIVE},
    ],
)
def test_existing_commercial_gates_are_not_bypassed(context, change):
    ctx = opportunities.documented(context)
    with pytest.raises(IntegrationError):
        register(ctx, **change)
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 0


def change_observation(ctx):
    with ctx["factory"]() as session:
        record = session.scalar(
            select(AppSetting).where(
                AppSetting.key == ShopeeMultistoreAdapter.key(ctx["plan"]["authorization_id"])
            )
        )
        value = copy.deepcopy(record.value)
        value["result"]["comparison"]["candidates"][1]["announced_price"] = "950.00"
        record.value = value
        session.commit()


@pytest.mark.parametrize("change", ["observation", "draft", "expired", "variant"])
def test_changed_evidence_blocks_existing_offer_and_publication_context(context, change):
    ctx = opportunities.documented(context)
    offer_id = register(ctx)
    destination_id = destination(ctx)
    if change == "observation":
        change_observation(ctx)
    elif change == "draft":
        ctx["reviews"].edit(
            ctx["plan"]["authorization_id"], "102", "203", reviews.edit_data(ctx, "102", "203")
        )
    elif change == "expired":
        ctx["clock"][0] += timedelta(days=2)
    else:
        with ctx["factory"]() as session:
            row = session.get(Product, session.get(Offer, offer_id).product_id)
            row.variant_id = "different-sku"
            session.commit()
    with ctx["factory"]() as session:
        with pytest.raises(IntegrationError):
            read_manual_offer(session, offer_id, ctx["settings"], ctx["clock"][0])
        # Existing publication path calls the same reader; no alternate approval path.
        with pytest.raises(IntegrationError):
            TelegramPublicationService(ctx["settings"], clock=lambda: ctx["clock"][0]).preview(
                session, offer_id, destination_id
            )
    result = preview(ctx, offer_id=offer_id)
    assert result["commercial_evidence"] is None and result["blockers"]
    assert collection.count(ctx, TelegramPublication) == 0


def test_existing_explicit_approval_flow_revalidates_multistore_evidence(context):
    ctx = opportunities.documented(context)
    offer_id, destination_id = register(ctx), destination(ctx)
    service = TelegramPublicationService(ctx["settings"], clock=lambda: ctx["clock"][0])
    with ctx["factory"]() as session:
        entry = service.preview(session, offer_id, destination_id)
        session.commit()
        entry_id, digest = entry.id, entry.preview_hash
    approved = service.approve(ctx["factory"], entry_id, digest, "synthetic_administrator")
    assert approved.status.value == "APPROVED"
    change_observation(ctx)
    with pytest.raises(IntegrationError, match="prévia multiloja"):
        service.approve(ctx["factory"], entry_id, digest, "synthetic_administrator")
    with ctx["factory"]() as session:
        row = session.get(TelegramPublication, entry_id)
        assert row.status.value == "EXPIRED" and row.approved_by is None
    assert ctx["calls"] == 1


def test_stale_context_and_wrong_product_block_before_history_write(context):
    ctx = opportunities.documented(context)
    binding = preview(ctx)["next_step"]["multistore_context"]
    wrong_product = product(ctx, "999:888")
    with pytest.raises(IntegrationError):
        register(ctx, product_id=wrong_product)
    change_observation(ctx)
    with pytest.raises(IntegrationError):
        register(ctx, multistore_context=binding)
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 0


def test_preview_api_is_authenticated_and_has_no_write_side_effects(context, application, client):
    ctx = opportunities.documented(context)
    application.state.shopee_discovery = ctx["discovery"]
    url = (
        "/api/v1/shopee-discovery/multistore-searches/"
        + ctx["plan"]["authorization_id"]
        + "/candidates/102/203/commercial-preview"
    )
    assert client.post(url, json={}).status_code == 401
    response = client.post(url, json={}, headers=collection.api.HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["announced_price"] == "1000.00"
    assert response.json()["commercial_handoff_selected"]
    assert (
        client.post(url, json={"offer_id": True}, headers=collection.api.HEADERS).status_code == 422
    )
    assert (
        client.post(
            url.replace("102/203", "999/888"), json={}, headers=collection.api.HEADERS
        ).status_code
        == 404
    )
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 0


def test_previous_real_response_remains_inconclusive_without_new_calls(context):
    ctx = context
    ctx["plan"] = replay.seed_recorded_result(ctx)
    analysis = ctx["discovery"].analyze_multistore_search(ctx["plan"]["authorization_id"])
    for candidate in analysis["candidates"]:
        result = ctx["discovery"].multistore_commercial_preview(
            ctx["plan"]["authorization_id"],
            candidate["identity"],
        )
        assert not result["commercial_handoff_selected"]
        assert result["commercial_evidence"] is None
        assert result["final_delivered_price"] is None
        assert result["missing_identity_evidence"]
    assert ctx["calls"] == 0
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 0


def test_tied_minimum_prices_keep_both_administrative_previews(context):
    ctx = reviews.setup(context)
    with ctx["factory"]() as session:
        record = session.scalar(
            select(AppSetting).where(
                AppSetting.key == ShopeeMultistoreAdapter.key(ctx["plan"]["authorization_id"])
            )
        )
        value = copy.deepcopy(record.value)
        value["result"]["comparison"]["candidates"][1]["announced_price"] = "1100.00"
        record.value = value
        session.commit()
    reviews.document(ctx)
    reviews.document(ctx, "102", "203")
    assert preview(ctx, "101:202")["commercial_handoff_selected"]
    assert preview(ctx, "102:203")["commercial_handoff_selected"]


def test_mock_source_cannot_enter_commercial_workflow_outside_test_environment(context):
    ctx = opportunities.documented(context)
    assert "SIMULATED_SOURCE_NOT_COMMERCIAL_EVIDENCE" in preview(ctx)["blockers"]
    ctx["settings"] = ctx["settings"].model_copy(update={"app_env": "production"})
    with pytest.raises(IntegrationError, match="prévia multiloja"):
        register(ctx)
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 0


def test_manual_offer_api_accepts_bound_context_and_keeps_explicit_verification(
    context,
    application,
    client,
    monkeypatch,
):
    import app.api.manual_offers as routes

    ctx = opportunities.documented(context)
    monkeypatch.setattr(application.state, "session_factory", ctx["factory"])

    # The existing endpoint's clock is fixed for the synthetic quote, never the real account.
    def fixed_create(session, settings, **kwargs):
        return create_manual_offer(session, settings, now=ctx["clock"][0], **kwargs)

    monkeypatch.setattr(routes, "create_manual_offer", fixed_create)
    payload = dict(
        product_id=product(ctx),
        current_price="1000.00",
        actor="synthetic_administrator",
        verified=False,
        availability="AVAILABLE",
        price_access="PUBLIC",
        multistore_context=ctx["discovery"]._json(preview(ctx)["next_step"]["multistore_context"]),
    )
    url = "/api/v1/manual-offers"
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers=collection.api.HEADERS).status_code == 422
    payload["verified"] = True
    response = client.post(url, json=payload, headers=collection.api.HEADERS)
    assert response.status_code == 201, response.text
    assert response.json()["previous_price"] is None
    assert response.json()["final_price"] is None
    assert response.json()["status"] == "PENDING_REVIEW"
    assert collection.count(ctx, Offer) == collection.count(ctx, ProductPriceHistory) == 1
    assert collection.count(ctx, TelegramPublication) == 0
