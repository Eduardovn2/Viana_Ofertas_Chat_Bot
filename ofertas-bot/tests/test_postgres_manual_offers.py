"""Manual workflow on PostgreSQL, only within a fresh *_test UUID schema."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier

import pytest
import test_postgres_categories as postgres_fixtures
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.db import build_session_factory
from app.main import create_app
from app.models import (
    AffiliateLink,
    CategoryMode,
    DestinationChannel,
    ManualOfferDetails,
    Platform,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    TelegramPublication,
    TelegramPublicationStatus,
)
from app.services.manual_offers import create_manual_offer
from app.services.telegram_publication import TelegramPublicationService

categories_postgres = postgres_fixtures.categories_postgres
KEY = "phase4-postgres-synthetic-administrative-key"
HEADERS = {"X-API-Key": KEY}


def setup_context(engine, url):
    factory = build_session_factory(engine)
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=url.render_as_string(hide_password=False),
        admin_api_key=KEY,
        scheduler_enabled=False,
        telegram_publisher_enabled=False,
        telegram_real_sends_authorized=False,
        mercado_livre_enabled=False,
        shopee_enabled=False,
    )
    with factory() as session:
        product = Product(
            platform=Platform.MERCADO_LIVRE,
            external_id="MLB9999999999",
            name="Sofá sintético PostgreSQL",
            original_url="https://mercadolivre.com.br/MLB9999999999",
            monitoring_enabled=False,
        )
        session.add(product)
        session.flush()
        session.add(
            AffiliateLink(
                product_id=product.id,
                source="MANUAL_LINKBUILDER_VERIFIED",
                url="https://meli.la/syntheticFixture",
            )
        )
        destination = PublicationDestination(
            name="Grupo sintético PostgreSQL",
            channel=DestinationChannel.TELEGRAM,
            external_id="-1009999999999",
            enabled=True,
            category_mode=CategoryMode.ALL,
        )
        session.add(destination)
        session.commit()
        return factory, settings, product.id, destination.id


@pytest.mark.postgres
def test_postgres_manual_api_registration_preview_approval(categories_postgres, monkeypatch):
    from app.integrations.telegram import TelegramPublisher

    def forbidden(*args, **kwargs):
        pytest.fail("Prévia/aprovação não pode enviar mensagens")

    monkeypatch.setattr(TelegramPublisher, "send", forbidden)
    engine, url = categories_postgres
    factory, settings, product_id, destination_id = setup_context(engine, url)
    application = create_app(settings)
    with TestClient(
        application, base_url="http://localhost", client=("127.0.0.1", 50000)
    ) as client:
        result = client.post(
            "/api/v1/manual-offers",
            headers=HEADERS,
            json={
                "product_id": product_id,
                "current_price": "10.01",
                "previous_price": "15.00",
                "actor": "Auditor sintético PostgreSQL",
                "verified": True,
                "availability": "AVAILABLE",
                "price_access": "PUBLIC",
            },
        )
        assert result.status_code == 201, result.text
        offer = result.json()
        result = client.post(
            f"/api/v1/manual-offers/{offer['id']}/preview",
            headers=HEADERS,
            json={"destination_id": destination_id},
        )
        assert result.status_code == 200, result.text
        entry = result.json()
        result = client.post(
            f"/api/v1/telegram/publications/{entry['id']}/approve",
            headers=HEADERS,
            json={"preview_hash": entry["preview_hash"], "actor": "Auditor", "confirmed": True},
        )
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "APPROVED"
        assert client.get("/api/v1/manual-offers", headers=HEADERS).json() == [offer]
    with factory() as session:
        assert session.get(ProductPriceHistory, offer["price_history_id"]).current_price == Decimal(
            "10.01"
        )
        assert (
            session.get(ManualOfferDetails, offer["id"]).verified_by
            == "Auditor sintético PostgreSQL"
        )
        assert session.get(TelegramPublication, entry["id"]).approved_by == "Auditor"
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 0
        assert session.get(Product, product_id).monitoring_enabled is False


@pytest.mark.postgres
def test_postgres_concurrent_previews_use_one_slot(categories_postgres):
    engine, url = categories_postgres
    factory, settings, product_id, destination_id = setup_context(engine, url)
    now = datetime.now(UTC)
    with factory() as session:
        bundle = create_manual_offer(
            session,
            settings,
            product_id=product_id,
            current_price=Decimal("10.01"),
            actor="Administrador sintético",
            verified=True,
            availability="AVAILABLE",
            price_access="PUBLIC",
            now=now,
        )
        session.commit()
        offer_id = bundle.offer.id
    barrier = Barrier(2)

    def worker():
        barrier.wait(timeout=10)
        with factory() as session:
            entry = TelegramPublicationService(settings, clock=lambda: now).preview(
                session, offer_id, destination_id
            )
            session.commit()
            return entry.id, entry.preview_hash

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: worker(), range(2)))
    assert results[0] == results[1]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(TelegramPublication)) == 1
        entry = session.get(TelegramPublication, results[0][0])
        assert entry.status == TelegramPublicationStatus.PENDING
        assert entry.active_slot == 1


@pytest.mark.postgres
def test_postgres_concurrent_dispatch_persists_one_attempt_with_official_mock(categories_postgres):
    import json
    from threading import Event

    import httpx
    from pydantic import SecretStr

    from app.integrations.telegram import TelegramPublisher
    from app.models import PublicationStatus

    engine, url = categories_postgres
    factory, settings, product_id, destination_id = setup_context(engine, url)
    settings = settings.model_copy(
        update={
            "telegram_publisher_enabled": True,
            "telegram_real_sends_authorized": True,
            "telegram_bot_token": SecretStr("123456789:SYNTHETIC_ONLY_NOT_A_LIVE_TOKEN_123456789"),
            "telegram_allowed_chat_ids": ("-1009999999999",),
        }
    )
    now = datetime.now(UTC)
    with factory() as session:
        bundle = create_manual_offer(
            session,
            settings,
            product_id=product_id,
            current_price=Decimal("10.01"),
            actor="Auditor sintético",
            verified=True,
            availability="AVAILABLE",
            price_access="PUBLIC",
            now=now,
        )
        session.commit()
        offer_id = bundle.offer.id
    entered, release = Event(), Event()
    calls = []

    def handler(request):
        calls.append(request)
        with factory() as session:
            assert session.scalar(select(PublicationLog)).status == PublicationStatus.PENDING
            assert (
                session.scalar(select(TelegramPublication)).status
                == TelegramPublicationStatus.SENDING
            )
        entered.set()
        assert release.wait(timeout=10)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 321,
                    "chat": {"id": int(json.loads(request.content)["chat_id"]), "type": "group"},
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        service = TelegramPublicationService(
            settings, TelegramPublisher(settings, client=transport), clock=lambda: now
        )
        with factory() as session:
            entry = service.preview(session, offer_id, destination_id)
            session.commit()
            entry_id, digest = entry.id, entry.preview_hash
        service.approve(factory, entry_id, digest, "Auditor")
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(service.publish, factory, entry_id)
            assert entered.wait(timeout=10)
            try:
                assert (
                    workers.submit(service.publish, factory, entry_id).result(timeout=10).status
                    == TelegramPublicationStatus.SENDING
                )
            finally:
                release.set()
            assert first.result(timeout=10).status == TelegramPublicationStatus.SENT
    assert len(calls) == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PublicationLog)) == 1


@pytest.mark.postgres
def test_postgres_prepare_known_group_paused_without_rewriting_existing_destinations(
    categories_postgres,
):
    from app.services.telegram_destinations import prepare_test_group

    engine, url = categories_postgres
    factory, _, _, existing_id = setup_context(engine, url)
    with factory() as session:
        prepared = prepare_test_group(session)
        session.commit()
        prepared_id = prepared.id
        assert prepared.external_id == "-5309308689" and prepared.enabled is False
        assert prepared.manual_approval_required is True
    with factory() as session:
        assert prepare_test_group(session).id == prepared_id
        assert session.get(PublicationDestination, existing_id).enabled is True
        session.commit()
