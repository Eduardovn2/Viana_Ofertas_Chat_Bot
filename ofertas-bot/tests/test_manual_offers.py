"""Synthetic administrator inputs only; HTTP and publishers are prohibited here."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import test_api as api_fixtures
from sqlalchemy import func, select

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.errors import IntegrationError
from app.integrations.telegram import TelegramPublisher
from app.models import (
    AffiliateLink,
    Availability,
    Base,
    Category,
    CategoryMode,
    Coupon,
    CouponDiscountType,
    CouponValidationState,
    DestinationChannel,
    ManualOfferDetails,
    Offer,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    PublicationQueueItem,
    SourceType,
    TelegramPublication,
    TelegramPublicationStatus,
)
from app.services.categories import CategoryService
from app.services.manual_offers import create_manual_offer, read_manual_offer
from app.services.telegram_publication import TelegramPublicationService

application = api_fixtures.application
client = api_fixtures.client
HEADERS = api_fixtures.HEADERS
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Oferta manual não deve consultar APIs nem enviar mensagens")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(TelegramPublisher, "send", forbidden)
    monkeypatch.setenv("TELEGRAM_PUBLISHER_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_REAL_SENDS_AUTHORIZED", "false")


@pytest.fixture
def context(tmp_path):
    url = f"sqlite:///{(tmp_path / 'manual.db').as_posix()}"
    engine = build_engine(url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=url,
        telegram_publisher_enabled=False,
        telegram_real_sends_authorized=False,
    )
    with factory() as session:
        product = Product(
            platform=Platform.MERCADO_LIVRE,
            external_id="MLB9999999999",
            name="Sofá sintético de testes",
            original_url="https://mercadolivre.com.br/MLB9999999999",
            monitoring_enabled=False,
        )
        session.add(product)
        session.flush()
        session.add(
            AffiliateLink(
                product_id=product.id,
                url="https://meli.la/syntheticFixture",
                source="MANUAL_LINKBUILDER_VERIFIED",
            )
        )
        CategoryService().seed(session)
        destination = PublicationDestination(
            name="Grupo sintético",
            channel=DestinationChannel.TELEGRAM,
            external_id="-1009999999999",
            enabled=True,
            category_mode=CategoryMode.ALL,
        )
        session.add(destination)
        session.commit()
        ids = product.id, destination.id
    yield factory, settings, *ids
    engine.dispose()


def register(context, **changes):
    factory, settings, product_id, _ = context
    with factory() as session:
        bundle = create_manual_offer(
            session,
            settings,
            **{
                "product_id": product_id,
                "current_price": Decimal("100.01"),
                "actor": "Administrador sintético",
                "verified": True,
                "availability": Availability.AVAILABLE,
                "price_access": PriceAccess.PUBLIC,
                "now": NOW,
                **changes,
            },
        )
        session.commit()
        return bundle.offer.id


def preview(context, offer_id):
    factory, settings, _, destination_id = context
    with factory() as session:
        entry = TelegramPublicationService(settings, clock=lambda: NOW).preview(
            session, offer_id, destination_id
        )
        session.commit()
        return entry.id, entry.preview_hash


def test_manual_evidence_optional_fields_no_monitoring_or_simulated_queue(context):
    offer_id = register(context)
    factory, settings, product_id, _ = context
    with factory() as session:
        bundle = read_manual_offer(session, offer_id, settings, NOW)
        assert bundle.history.current_price == Decimal("100.01")
        assert bundle.history.source_type == SourceType.MANUAL
        assert bundle.history.is_simulated is False
        assert bundle.details.verified_by == "Administrador sintético"
        assert bundle.product.monitoring_enabled is False
        assert bundle.offer.previous_price is bundle.offer.historical_price is None
        assert bundle.offer.discount_amount is bundle.offer.discount_percentage is None
        assert bundle.offer.coupon_id is bundle.offer.final_price is None
        assert bundle.details.image_url is bundle.history.installment_text is None
        assert session.scalar(select(func.count()).select_from(PublicationQueueItem)) == 0
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0
        assert (
            settings.telegram_publisher_enabled is settings.telegram_real_sends_authorized is False
        )
        assert (
            session.scalar(select(Product.monitoring_enabled).where(Product.id == product_id))
            is False
        )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"current_price": Decimal("0")}, "MANUAL_INPUT_INVALID"),
        ({"current_price": Decimal("NaN")}, "MANUAL_INPUT_INVALID"),
        ({"current_price": Decimal("1.001")}, "MANUAL_INPUT_INVALID"),
        ({"current_price": 1.01}, "MANUAL_INPUT_INVALID"),
        ({"previous_price": Decimal("-1")}, "MANUAL_INPUT_INVALID"),
        ({"verified": False}, "MANUAL_INPUT_INVALID"),
        ({"verified": 1}, "MANUAL_INPUT_INVALID"),
        ({"actor": "\nforged"}, "MANUAL_INPUT_INVALID"),
        ({"installment_text": "\x00"}, "MANUAL_INPUT_INVALID"),
        ({"availability": Availability.UNKNOWN}, "MANUAL_INPUT_INVALID"),
        ({"availability": Availability.UNAVAILABLE}, "MANUAL_INPUT_INVALID"),
        ({"price_access": PriceAccess.EXCLUSIVE}, "MANUAL_INPUT_INVALID"),
        ({"price_access": PriceAccess.CONDITIONAL}, "MANUAL_INPUT_INVALID"),
        ({"image_url": "http://localhost/image"}, "MANUAL_INPUT_INVALID"),
        ({"verified_at": NOW + timedelta(seconds=1)}, "MANUAL_EVIDENCE_STALE"),
        ({"verified_at": NOW - timedelta(seconds=601)}, "MANUAL_EVIDENCE_STALE"),
        ({"verified_at": NOW.replace(tzinfo=None)}, "MANUAL_EVIDENCE_STALE"),
        ({"expires_at": NOW}, "MANUAL_EVIDENCE_STALE"),
        ({"expires_at": NOW.replace(tzinfo=None)}, "MANUAL_INPUT_INVALID"),
        ({"coupon_id": 9999}, "MANUAL_COUPON_INVALID"),
        ({"product_id": 9999}, "MANUAL_PRODUCT_NOT_FOUND"),
    ],
)
def test_invalid_manual_input_is_not_persisted(context, changes, code):
    with pytest.raises(IntegrationError) as error:
        register(context, **changes)
    assert error.value.code == code
    with context[0]() as session:
        for model in (Offer, ManualOfferDetails, ProductPriceHistory):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_advertised_previous_and_own_history_remain_distinct(context):
    first = register(context, current_price=Decimal("120.00"), now=NOW - timedelta(minutes=1))
    second = register(context, current_price=Decimal("100.00"), previous_price=Decimal("150.00"))
    factory, settings, _, _ = context
    with factory() as session:
        bundle = read_manual_offer(session, second, settings, NOW)
        assert bundle.offer.historical_price == Decimal("120.00")
        assert bundle.offer.previous_price == Decimal("150.00")
        assert bundle.offer.discount_amount == Decimal("50.00")
        assert bundle.offer.discount_percentage == Decimal("33.33")
        with pytest.raises(IntegrationError, match="verificação manual"):
            read_manual_offer(session, first, settings, NOW)
        assert session.scalar(select(func.count()).select_from(ManualOfferDetails)) == 2


def test_unverified_or_foreign_affiliate_link_cannot_be_published(context):
    with context[0]() as session:
        link = session.scalar(select(AffiliateLink))
        link.source = "MANUAL_LINKBUILDER"
        session.commit()
    with pytest.raises(IntegrationError) as error:
        register(context)
    assert error.value.code == "MANUAL_LINK_REQUIRED"


@pytest.mark.parametrize("coupon_age", [0, 600])
def test_confirmed_coupon_revalidated_and_optional_image(context, coupon_age):
    factory, settings, product_id, _ = context
    with factory() as session:
        coupon = Coupon(
            code="SYNTHETIC10",
            platform=Platform.MERCADO_LIVRE,
            discount_type=CouponDiscountType.PERCENTAGE,
            discount_value=Decimal("10"),
            validation_state=CouponValidationState.CONFIRMED,
            price_access=PriceAccess.PUBLIC,
            last_verified_at=NOW - timedelta(seconds=coupon_age),
            expires_at=NOW + timedelta(hours=1),
            eligible_products=[session.get(Product, product_id)],
        )
        session.add(coupon)
        session.commit()
        coupon_id = coupon.id
    if coupon_age == 600:
        with pytest.raises(IntegrationError) as error:
            register(context, coupon_id=coupon_id)
        assert error.value.code == "MANUAL_EVIDENCE_STALE"
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 0
        return
    offer_id = register(
        context,
        coupon_id=coupon_id,
        installment_text="2x de R$ 50,01",
        image_url="https://images.example.com/synthetic.jpg",
    )
    with factory() as session:
        bundle = read_manual_offer(session, offer_id, settings, NOW)
        assert bundle.offer.final_price == Decimal("90.01")
        session.get(Coupon, coupon_id).discount_value = Decimal("20")
        session.commit()
    with factory() as session, pytest.raises(IntegrationError) as error:
        read_manual_offer(session, offer_id, settings, NOW)
    assert error.value.code == "MANUAL_COUPON_INVALID"


def test_missing_or_stale_manual_offer_cannot_be_approved(context):
    offer_id = register(context)
    factory, settings, _, _ = context
    entry_id, digest = preview(context, offer_id)
    with factory() as session, pytest.raises(IntegrationError) as missing:
        read_manual_offer(session, 9999, settings, NOW)
    assert missing.value.code == "MANUAL_OFFER_NOT_FOUND"
    service = TelegramPublicationService(settings, clock=lambda: NOW + timedelta(minutes=11))
    with pytest.raises(IntegrationError) as error:
        service.approve(factory, entry_id, digest, "Auditor sintético")
    assert error.value.code == "MANUAL_EVIDENCE_STALE"
    with factory() as session:
        entry = session.get(TelegramPublication, entry_id)
        assert entry.status == TelegramPublicationStatus.EXPIRED
        assert entry.active_slot is entry.approved_by is None


def test_preview_idempotent_and_approval_bound_to_exact_destination(context):
    offer_id = register(context)
    entry_id, digest = preview(context, offer_id)
    assert preview(context, offer_id) == (entry_id, digest)
    factory, settings, _, destination_id = context
    service = TelegramPublicationService(settings, clock=lambda: NOW)
    entry = service.approve(factory, entry_id, digest, "Auditor sintético")
    assert entry.status == TelegramPublicationStatus.APPROVED
    assert entry.approved_at == NOW
    assert preview(context, offer_id) == (entry_id, digest)
    with factory() as session:
        session.get(PublicationDestination, destination_id).external_id = "-1009999999998"
        session.commit()
    with pytest.raises(IntegrationError) as error:
        service.approve(factory, entry_id, digest, "Auditor sintético")
    assert error.value.code == "OUTBOX_REVIEW_REQUIRED"
    with factory() as session:
        entry = session.get(TelegramPublication, entry_id)
        assert entry.status == TelegramPublicationStatus.PENDING
        assert entry.approved_by is entry.approved_at is None
        assert entry.preview_hash != digest


@pytest.mark.parametrize("change", ["price", "name", "affiliate", "category", "destination"])
def test_changed_evidence_or_routing_invalidates_approval(context, change):
    offer_id = register(context)
    entry_id, digest = preview(context, offer_id)
    factory, settings, product_id, destination_id = context
    service = TelegramPublicationService(settings, clock=lambda: NOW)
    service.approve(factory, entry_id, digest, "Auditor sintético")
    with factory() as session:
        if change == "price":
            session.get(Offer, offer_id).current_price = Decimal("1.00")
        elif change == "name":
            session.get(Product, product_id).name = "Sofá sintético ALTERADO"
        elif change == "affiliate":
            session.scalar(select(AffiliateLink)).url = "https://meli.la/changedSynthetic"
        elif change == "category":
            for category in session.scalars(select(Category)):
                category.active = False
        else:
            session.get(PublicationDestination, destination_id).enabled = False
        session.commit()
    with pytest.raises(IntegrationError):
        service.approve(factory, entry_id, digest, "Auditor sintético")
    with factory() as session:
        assert session.get(TelegramPublication, entry_id).approved_by is None
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0


def test_reverification_preserves_old_evidence_and_requires_new_approval(context):
    first = register(context)
    entry_id, digest = preview(context, first)
    TelegramPublicationService(context[1], clock=lambda: NOW).approve(
        context[0], entry_id, digest, "Auditor"
    )
    second = register(context, current_price=Decimal("99.99"))
    assert first != second
    new_id, new_digest = preview(context, second)
    assert new_id == entry_id and new_digest != digest
    with context[0]() as session:
        assert (
            session.get(TelegramPublication, entry_id).status == TelegramPublicationStatus.PENDING
        )
        assert session.get(Offer, first).current_price == Decimal("100.01")
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 2


def test_approval_replay_keeps_original_auditor(context):
    offer_id = register(context)
    entry_id, digest = preview(context, offer_id)
    service = TelegramPublicationService(context[1], clock=lambda: NOW)
    first = service.approve(context[0], entry_id, digest, "Primeiro auditor")
    replay = service.approve(context[0], entry_id, digest, "Outro auditor")
    assert replay.approved_by == first.approved_by == "Primeiro auditor"
    assert replay.approved_at == first.approved_at


@pytest.mark.parametrize("elapsed,allowed", [(10799, False), (10800, True), (10801, True)])
def test_preview_republication_three_hours_and_new_manual_evidence(context, elapsed, allowed):
    from app.models import PublicationStatus

    sent_at = NOW - timedelta(seconds=elapsed)
    previous = register(context, now=sent_at - timedelta(seconds=1))
    factory, _, product_id, destination_id = context
    with factory() as session:
        session.add(
            PublicationLog(
                offer_id=previous,
                product_id=product_id,
                destination_id=destination_id,
                status=PublicationStatus.SENT,
                attempt_number=1,
                sent_at=sent_at,
                message_id="synthetic-only",
            )
        )
        session.commit()
    current = register(context)
    if allowed:
        entry_id, _ = preview(context, current)
        with factory() as session:
            assert (
                session.get(TelegramPublication, entry_id).status
                == TelegramPublicationStatus.PENDING
            )
    else:
        with pytest.raises(IntegrationError) as error:
            preview(context, current)
        assert error.value.code == "OUTBOX_COOLDOWN"


def test_simulated_history_never_becomes_manual_baseline(context):
    factory, settings, product_id, _ = context
    with factory() as session:
        session.add(
            ProductPriceHistory(
                product_id=product_id,
                current_price=Decimal("9999"),
                source="simulated",
                source_type=SourceType.SIMULATED,
                is_simulated=True,
                observed_at=NOW - timedelta(seconds=1),
                availability=Availability.AVAILABLE,
                price_access=PriceAccess.PUBLIC,
            )
        )
        session.commit()
    offer_id = register(context)
    with factory() as session:
        bundle = read_manual_offer(session, offer_id, settings, NOW)
        assert bundle.offer.historical_price is None
        assert bundle.offer.previous_price is None


def test_manual_observation_older_than_latest_is_rejected(context):
    register(context)
    with pytest.raises(IntegrationError) as error:
        register(context, verified_at=NOW - timedelta(seconds=1))
    assert error.value.code == "MANUAL_EVIDENCE_STALE"
    with context[0]() as session:
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1


def test_persisted_freshness_configuration_is_used_on_revalidation(context):
    from app.services.configuration import save_monitoring_configuration

    offer_id = register(context)
    factory, settings, _, _ = context
    with factory() as session:
        save_monitoring_configuration(session, settings, {"price_max_age_seconds": 30})
        session.commit()
    with factory() as session:
        assert (
            read_manual_offer(session, offer_id, settings, NOW + timedelta(seconds=29)).offer.id
            == offer_id
        )
        with pytest.raises(IntegrationError) as error:
            read_manual_offer(session, offer_id, settings, NOW + timedelta(seconds=31))
    assert error.value.code == "MANUAL_EVIDENCE_STALE"


def api_register(client, **changes):
    product = api_fixtures.create(client, name="Sofá sintético de API", monitoring_enabled=False)
    response = client.put(
        f"/api/v1/products/{product['id']}/affiliate-link",
        headers=HEADERS,
        json={"url": "https://meli.la/syntheticFixture", "operationally_verified": True},
    )
    assert response.status_code == 200
    return client.post(
        "/api/v1/manual-offers",
        headers=HEADERS,
        json={
            "product_id": product["id"],
            "current_price": "100.01",
            "actor": "Administrador sintético",
            "verified": True,
            "availability": "AVAILABLE",
            "price_access": "PUBLIC",
            **changes,
        },
    )


def test_api_registration_listing_read_and_admin_auth(client, application):
    response = api_register(client)
    assert response.status_code == 201, response.text
    offer = response.json()
    assert offer["current_price"] == "100.01" and offer["status"] == "PENDING_REVIEW"
    assert offer["category_ids"] and offer["coupon_id"] is None
    assert client.get("/api/v1/manual-offers", headers=HEADERS).json() == [offer]
    assert client.get(f"/api/v1/manual-offers/{offer['id']}", headers=HEADERS).json() == offer
    assert client.get("/api/v1/manual-offers?product_id=9999", headers=HEADERS).json() == []
    assert client.get("/api/v1/manual-offers?offset=1", headers=HEADERS).json() == []
    assert client.get("/api/v1/manual-offers?limit=101", headers=HEADERS).status_code == 422
    assert client.get("/api/v1/manual-offers/9999", headers=HEADERS).status_code == 404
    for path in ("/api/v1/manual-offers", "/api/v1/telegram/publications"):
        assert client.get(path).status_code == 401
    assert client.post("/api/v1/manual-offers", json={}).status_code == 401
    assert application.state.settings.telegram_publisher_enabled is False
    assert application.state.settings.telegram_real_sends_authorized is False


@pytest.mark.parametrize(
    "changes",
    [
        {"current_price": "1.001"},
        {"current_price": "NaN"},
        {"previous_price": "0"},
        {"verified": False},
        {"verified": 1},
        {"price_access": "EXCLUSIVE"},
        {"availability": "UNKNOWN"},
        {"actor": "\u0000fake"},
        {"image_url": "https://127.0.0.1/photo"},
        {"product_id": True},
        {"token": "not-accepted"},
    ],
)
def test_api_invalid_data_rejected_atomically(client, application, changes):
    response = api_register(client, **changes)
    assert response.status_code == 422, response.text
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Offer)) == 0
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 0


def test_api_preview_and_explicit_approval_no_send(client, application):
    offer = api_register(client).json()
    with application.state.session_factory() as session:
        destination = PublicationDestination(
            name="Grupo sintético",
            channel=DestinationChannel.TELEGRAM,
            external_id="-1009999999999",
            enabled=True,
            category_mode=CategoryMode.ALL,
        )
        session.add(destination)
        session.commit()
        destination_id = destination.id
    path = f"/api/v1/manual-offers/{offer['id']}/preview"
    assert client.post(path, json={"destination_id": destination_id}).status_code == 401
    response = client.post(path, headers=HEADERS, json={"destination_id": destination_id})
    assert response.status_code == 200, response.text
    entry = response.json()
    assert entry["status"] == "PENDING"
    assert "Publicidade" in entry["message"]["text"]
    assert "Cupom" not in entry["message"]["text"]
    assert "Preço anterior" not in entry["message"]["text"]
    assert "100,01" in entry["message"]["text"]
    approval = f"/api/v1/telegram/publications/{entry['id']}/approve"
    payload = {"preview_hash": entry["preview_hash"], "actor": "Auditor", "confirmed": True}
    assert client.post(approval, json=payload).status_code == 401
    assert (
        client.post(approval, headers=HEADERS, json={**payload, "confirmed": False}).status_code
        == 422
    )
    assert (
        client.post(
            approval, headers=HEADERS, json={**payload, "preview_hash": "0" * 64}
        ).status_code
        == 409
    )
    response = client.post(approval, headers=HEADERS, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "APPROVED"
    assert response.json()["approved_by"] == "Auditor"
    assert client.get("/api/v1/telegram/publications", headers=HEADERS).json() == [response.json()]
    assert (
        client.get("/api/v1/telegram/publications?destination_id=9999", headers=HEADERS).json()
        == []
    )
    assert (
        client.post(
            f"/api/v1/telegram/publications/{entry['id']}/publish", headers=HEADERS
        ).status_code
        == 404
    )
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0
    assert application.state.settings.telegram_publisher_enabled is False


@pytest.mark.parametrize(
    "mode,enabled",
    [(CategoryMode.UNCONFIGURED, True), (CategoryMode.SELECTED, True), (CategoryMode.ALL, False)],
)
def test_api_unconfigured_filtered_or_paused_destination_rejects_preview(
    client, application, mode, enabled
):
    offer = api_register(client).json()
    with application.state.session_factory() as session:
        destination = PublicationDestination(
            name="Bloqueado sintético",
            channel=DestinationChannel.TELEGRAM,
            external_id="-1009999999999",
            enabled=enabled,
            category_mode=mode,
        )
        session.add(destination)
        session.commit()
        destination_id = destination.id
    response = client.post(
        f"/api/v1/manual-offers/{offer['id']}/preview",
        headers=HEADERS,
        json={"destination_id": destination_id},
    )
    assert response.status_code == 409
    assert client.get("/api/v1/telegram/publications", headers=HEADERS).json() == []
