"""End-to-end synthetic official snapshots. No real prices or external API requests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import test_api as api_fixtures
from sqlalchemy import func, select
from test_api import HEADERS, create

from app.api.integrations import CouponEvidenceWrite
from app.integrations.contracts import ProductSnapshot, fingerprint
from app.integrations.errors import AuthorizationDenied, IntegrationError
from app.integrations.http import JsonTransport
from app.models import (
    Availability,
    Category,
    CategorySource,
    Coupon,
    IntegrationEvent,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    SourceType,
)
from app.services.categories import CategoryService
from app.services.coupon_evidence import save_manual_evidence
from app.services.monitoring import record_price
from app.services.scoring import evaluate_coupon

NOW = datetime(2026, 10, 18, 15, tzinfo=UTC)
application = api_fixtures.application
client = api_fixtures.client


class SyntheticConnector:
    """Explicitly synthetic contract fixture, not an actual marketplace response."""

    def __init__(self, clock):
        self.clock, self.calls = clock, 0
        self.error = None

    def fetch_product(self, ref):
        self.calls += 1
        if self.error:
            raise self.error
        return ProductSnapshot(
            platform=ref.platform,
            external_item_id=ref.external_item_id,
            canonical_url=ref.canonical_url,
            name="Furadeira sintética para testes",
            variant_id=ref.variant_id,
            current_price=Decimal("99.90"),
            previous_price=Decimal("120.00"),
            availability=Availability.AVAILABLE,
            price_access=PriceAccess.PUBLIC,
            source="synthetic:official_contract",
            observed_at=self.clock(),
            is_simulated=False,
            source_type=SourceType.OFFICIAL_API,
            source_reference="https://api.mercadolibre.com/items/MLB3810190843/sale_price",
            raw_fingerprint=fingerprint({"price": "99.90", "variant": ref.variant_id}),
            category_external_id="MLB_SYNTHETIC",
        )


def synthetic(application):
    now = [NOW]
    runtime = application.state.monitoring
    runtime.clock = lambda: now[0]
    connector = SyntheticConnector(runtime.clock)
    application.state.integrations._connectors[Platform.MERCADO_LIVRE] = connector
    return connector, now


def test_disabled_status_auth_and_refresh_never_call_network(client, application, monkeypatch):
    monkeypatch.setattr(
        JsonTransport, "request", lambda *a, **k: pytest.fail("external network forbidden")
    )
    assert client.get("/api/v1/integrations/status").status_code == 401
    result = client.get("/api/v1/integrations/status", headers=HEADERS)
    assert result.status_code == 200
    assert all(item["state"] == "DISABLED" for item in result.json())
    assert "secret" not in result.text.lower()
    product = create(client)
    result = client.post(f"/api/v1/products/{product['id']}/refresh-real", headers=HEADERS)
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "CONNECTOR_DISABLED"
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 0
        assert session.scalar(select(IntegrationEvent.error_code)) == "CONNECTOR_DISABLED"


def test_synthetic_real_pipeline_provenance_affiliate_dedup_and_three_hours(client, application):
    connector, now = synthetic(application)
    client.post(
        "/api/v1/destinations",
        headers=HEADERS,
        json={"name": "Synthetic dry run", "external_id": "synthetic", "category_mode": "all"},
    )
    product = create(client)
    base = f"/api/v1/products/{product['id']}"
    first = client.post(base + "/refresh-real", headers=HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["source_type"] == "OFFICIAL_API"
    assert first.json()["is_simulated"] is False
    repeated = client.post(base + "/refresh-real", headers=HEADERS)
    assert repeated.json()["id"] == first.json()["id"]
    offers = client.get("/api/v1/offers", headers=HEADERS).json()
    assert offers[0]["affiliate_pending"] is True
    queue = client.get("/api/v1/queue", headers=HEADERS).json()
    assert len(queue) == 1 and queue[0]["status"] == "PENDING"
    assert queue[0]["blocked_reason"] == "AFFILIATE_PENDING"
    assert queue[0]["source_is_simulated"] is False and queue[0]["is_simulated"] is True
    assert (
        client.put(
            base + "/affiliate-link",
            headers=HEADERS,
            json={"url": "https://meli.la/synthetic", "operationally_verified": True},
        ).status_code
        == 200
    )
    now[0] += timedelta(seconds=2)
    client.post(base + "/refresh-real", headers=HEADERS)
    queue = client.get("/api/v1/queue", headers=HEADERS).json()
    assert len(queue) == 1 and queue[0]["status"] == "READY"
    from app.services.publication_queue import SimulatedQueueService

    with application.state.session_factory() as session:
        result = SimulatedQueueService(application.state.settings, clock=lambda: now[0]).publish(
            session, queue[0]["id"]
        )
        assert result.status == "PUBLISHED"
        assert result.next_eligible_at == now[0] + timedelta(hours=3)
        session.commit()
    now[0] += timedelta(seconds=60)
    client.post(base + "/refresh-real", headers=HEADERS)
    assert len(client.get("/api/v1/queue", headers=HEADERS).json()) == 1
    now[0] += timedelta(hours=3)
    refreshed = client.post(base + "/refresh-real", headers=HEADERS)
    assert refreshed.status_code == 200
    queue = client.get("/api/v1/queue", headers=HEADERS).json()
    assert len(queue) == 2
    assert sum(item["status"] == "READY" for item in queue) == 1


def test_external_auth_failure_preserves_history_and_pauses_connector(client, application):
    connector, _ = synthetic(application)
    product = create(client)
    base = f"/api/v1/products/{product['id']}/refresh-real"
    assert client.post(base, headers=HEADERS).status_code == 200
    connector.error = AuthorizationDenied()
    assert client.post(base, headers=HEADERS).status_code == 403
    assert client.post(base, headers=HEADERS).status_code == 403
    assert connector.calls == 2
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1
        assert session.scalar(select(ProductPriceHistory.current_price)) == Decimal("99.90")
        assert (
            session.scalar(
                select(func.count())
                .select_from(IntegrationEvent)
                .where(IntegrationEvent.status == "FAILED")
            )
            == 2
        )


def test_failed_check_does_not_clear_durable_block(client, application):
    product = create(client, monitoring_enabled=False)
    registry = application.state.integrations
    registry.block(Platform.MERCADO_LIVRE)
    result = client.post(
        "/api/v1/integrations/MERCADO_LIVRE/check",
        headers=HEADERS,
        json={"product_id": product["id"]},
    )
    assert result.status_code == 409
    assert registry.is_blocked(Platform.MERCADO_LIVRE)


def test_successful_controlled_check_clears_block_after_valid_snapshot(client, application):
    connector, _ = synthetic(application)
    product = create(client)
    registry = application.state.integrations
    registry.block(Platform.MERCADO_LIVRE)
    result = client.post(
        "/api/v1/integrations/MERCADO_LIVRE/check",
        headers=HEADERS,
        json={"product_id": product["id"]},
    )
    assert result.status_code == 200, result.text
    assert connector.calls == 1
    assert not registry.is_blocked(Platform.MERCADO_LIVRE)


def test_partial_price_permission_denial_keeps_durable_block(client, application, monkeypatch):
    connector, _ = synthetic(application)
    product = create(client)
    registry = application.state.integrations
    original_fetch = connector.fetch_product

    def partial(ref):
        return replace(
            original_fetch(ref),
            current_price=None,
            previous_price=None,
            price_access=PriceAccess.UNKNOWN,
            evidence_metadata={
                "price_access_reason": "price_permission_denied",
                "price_http_status": 403,
            },
        )

    monkeypatch.setattr(connector, "fetch_product", partial)
    registry.block(Platform.MERCADO_LIVRE)
    result = client.post(
        "/api/v1/integrations/MERCADO_LIVRE/check",
        headers=HEADERS,
        json={"product_id": product["id"]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["current_price"] is None
    assert registry.is_blocked(Platform.MERCADO_LIVRE)
    with application.state.session_factory() as session:
        event = session.scalar(select(IntegrationEvent))
        assert event.status == "PARTIAL" and event.error_code == "PRICE_PERMISSION_DENIED"


@pytest.mark.parametrize("code", ["TIMEOUT", "UPSTREAM_ERROR"])
def test_transient_external_failure_never_overwrites_last_valid_price(client, application, code):
    connector, now = synthetic(application)
    product = create(client)
    base = f"/api/v1/products/{product['id']}/refresh-real"
    assert client.post(base, headers=HEADERS).status_code == 200
    connector.error = IntegrationError(code, "Falha sintética segura.")
    now[0] += timedelta(minutes=5)
    assert client.post(base, headers=HEADERS).status_code in {502, 504}
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1
        assert session.scalar(select(ProductPriceHistory.current_price)) == Decimal("99.90")
        assert (
            session.scalar(
                select(IntegrationEvent.error_code).where(IntegrationEvent.status == "FAILED")
            )
            == code
        )


def test_real_scheduler_skips_disabled_platforms_without_fallback_or_network(
    client, application, monkeypatch
):
    create(client)
    application.state.settings.price_provider = "marketplaces"
    monkeypatch.setattr(
        JsonTransport, "request", lambda *a, **k: pytest.fail("no network without credentials")
    )
    assert application.state.monitoring.run_due()["attempted"] == 0
    assert application.state.monitoring.status()["threads_blocked"] is True


def test_affiliate_cannot_claim_recognizable_different_listing(client):
    product = create(client)
    result = client.put(
        f"/api/v1/products/{product['id']}/affiliate-link",
        headers=HEADERS,
        json={
            "url": "https://produto.mercadolivre.com.br/MLB-999-outro",
            "operationally_verified": True,
        },
    )
    assert result.status_code == 422


def test_shopee_discovery_api_ingests_concrete_contract_without_claiming_stock_or_offer(
    client, application
):
    from test_marketplace_pipeline import shopee_provider

    provider, transport = shopee_provider()
    application.state.integrations._connectors[Platform.SHOPEE] = provider.connector
    result = client.post(
        "/api/v1/integrations/SHOPEE/discover", headers=HEADERS, json={"query": "SYNTHETIC"}
    )
    assert result.status_code == 200, result.text
    assert len(result.json()["product_ids"]) == 1
    assert result.json()["publication_mode"] == "dry-run"
    product_id = result.json()["product_ids"][0]
    history = client.get(f"/api/v1/products/{product_id}/price-history", headers=HEADERS).json()[0]
    assert history["price_access"] == history["availability"] == "UNKNOWN"
    assert history["current_price"] == "99.90" and history["previous_price"] is None
    assert history["source_reference"] == "productOfferV2:101:202"
    assert history["evidence_metadata"]["commission_rate"] == "0.1234"
    assert client.get("/api/v1/offers", headers=HEADERS).json() == []
    assert len(transport.calls) == 1


def test_explicit_automatic_shopee_discovery_keeps_interval_and_deduplicates(client, application):
    from pydantic import SecretStr
    from test_marketplace_pipeline import NOW as SOURCE_TIME
    from test_marketplace_pipeline import shopee_provider

    settings = application.state.settings
    settings.price_provider = "marketplaces"
    settings.shopee_enabled = True
    settings.shopee_app_id = SecretStr("SYNTHETIC_APP")
    settings.shopee_secret = SecretStr("SYNTHETIC_SECRET_NOT_LIVE")
    settings.shopee_discovery_enabled = True
    settings.shopee_discovery_query = "SYNTHETIC"
    provider, transport = shopee_provider()
    application.state.integrations._connectors[Platform.SHOPEE] = provider.connector
    runtime = application.state.monitoring
    runtime.clock = lambda: SOURCE_TIME
    assert runtime.run_due()["attempted"] == 0
    assert len(transport.calls) == 1
    assert runtime.run_due()["succeeded"] == 1
    assert len(transport.calls) == 2  # one discovery plus one product refresh; no second discovery
    runtime.run_due()
    assert len(transport.calls) == 2
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1


def test_variant_identity_separate_and_immutable(client):
    first = create(client, variant_id="220V")
    second = create(client, variant_id="110V")
    assert first["id"] != second["id"]
    assert (
        client.post(
            "/api/v1/products",
            headers=HEADERS,
            json={
                "platform": "MERCADO_LIVRE",
                "name": "synthetic duplicate",
                "original_url": first["original_url"],
                "variant_id": "220V",
            },
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/products/{first['id']}", headers=HEADERS, json={"variant_id": "110V"}
        ).status_code
        == 422
    )


def test_unknown_coupon_and_free_shipping_do_not_create_monetary_discount(client, application):
    product = create(client)
    base = f"/api/v1/products/{product['id']}/coupon-evidence"
    assert client.post(base, headers=HEADERS, json={"type": "FREE_SHIPPING"}).status_code == 201
    response = client.post(
        base,
        headers=HEADERS,
        json={
            "type": "PERCENT",
            "code": "SYNTH10",
            "value": "10.00",
            "validation_state": "CONFIRMED",
        },
    )
    assert response.status_code == 422
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Coupon)) == 0


def test_manual_confirmed_coupon_uses_existing_decimal_minimum_cap_and_expiration(
    application, client
):
    product_id = create(client)["id"]
    with application.state.session_factory() as session:
        product = session.get(Product, product_id)
        data = CouponEvidenceWrite(
            code="SYNTH20",
            type="PERCENT",
            value="20",
            minimum_purchase="50",
            max_discount="12.34",
            validation_state="CONFIRMED",
            price_access="PUBLIC",
            verified_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            source_reference=product.original_url,
            product_eligibility_confirmed=True,
            conditions_complete=True,
        )
        evidence = save_manual_evidence(session, product, data, now=NOW)
        coupon = session.get(Coupon, evidence.coupon_id)
        assert evidence.source_type == SourceType.MANUAL
        assert evaluate_coupon(coupon, product, Decimal("49.99"), NOW) is None
        result = evaluate_coupon(coupon, product, Decimal("99.90"), NOW)
        assert result.final_price == Decimal("87.56")
        assert result.discount_amount == Decimal("12.34")
        assert evaluate_coupon(coupon, product, Decimal("99.90"), NOW + timedelta(hours=2)) is None
        recheck_time = NOW + timedelta(minutes=11)
        assert evaluate_coupon(coupon, product, Decimal("99.90"), recheck_time) is None
        renewed = save_manual_evidence(
            session,
            product,
            data.model_copy(update={"verified_at": recheck_time}),
            now=recheck_time,
        )
        assert renewed.id != evidence.id and renewed.coupon_id == evidence.coupon_id
        assert evaluate_coupon(
            coupon, product, Decimal("99.90"), recheck_time
        ).final_price == Decimal("87.56")
        with pytest.raises(ValueError, match="outra origem, produto ou condições"):
            save_manual_evidence(
                session,
                product,
                data.model_copy(update={"value": Decimal("30.00")}),
                now=recheck_time,
            )
        assert coupon.discount_value == Decimal("20.00")


def test_manual_category_preserved_during_real_ingestion_and_house_disabled(client, application):
    synthetic(application)
    product_id = create(client)["id"]
    with application.state.session_factory() as session:
        service = CategoryService()
        service.seed(session)
        product = session.get(Product, product_id)
        service.set_manual(
            session, product, [session.scalar(select(Category.id).where(Category.slug == "moda"))]
        )
        session.commit()
    assert (
        client.post(f"/api/v1/products/{product_id}/refresh-real", headers=HEADERS).status_code
        == 200
    )
    with application.state.session_factory() as session:
        product = session.get(Product, product_id)
        assert {link.category.slug for link in product.category_links} == {"moda"}
        assert product.category_links[0].source == CategorySource.MANUAL
        house = session.scalar(
            select(PublicationDestination).where(
                PublicationDestination.external_id == "local:ofertas-de-casa"
            )
        )
        assert house.enabled is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_reference", "https://api.mercadolibre.com/x?token=secret"),
        ("raw_fingerprint", "invalid"),
    ],
)
def test_provenance_rejects_unsafe_reference_and_invalid_fingerprint(
    application, client, key, value
):
    from dataclasses import replace

    connector, _ = synthetic(application)
    product_id = create(client)["id"]
    with application.state.session_factory() as session:
        product = session.get(Product, product_id)
        from app.integrations.contracts import ProductRef

        snapshot = connector.fetch_product(
            ProductRef(product.platform, product.external_id, product.original_url)
        )
        with pytest.raises(ValueError):
            record_price(session, product, replace(snapshot, **{key: value}), now=NOW)
