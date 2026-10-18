from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from alembic import command
from app.core.config import Settings, get_settings
from app.main import create_app
from app.models import AffiliateLink, ProductPriceHistory

KEY = "test-only-administrative-key-1234567890"
HEADERS = {"X-API-Key": KEY}
PRODUCT = {
    "platform": "MERCADO_LIVRE",
    "name": "Produto SIMULADO para teste",
    "original_url": "https://produto.mercadolivre.com.br/MLB-3810190843-exemplo_JM",
}


@pytest.fixture
def application(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    application = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            database_url=database_url,
            admin_api_key=KEY,
        )
    )
    yield application
    application.state.engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def client(application):
    with TestClient(
        application, base_url="http://localhost", client=("127.0.0.1", 50000)
    ) as client:
        yield client


def create(client, **changes):
    response = client.post("/api/v1/products", json={**PRODUCT, **changes}, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_docs_and_admin_protection(client):
    assert client.get("/health").json() == {
        "status": "ok",
        "database": "connected",
        "price_provider": "simulated",
    }
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
    assert client.get("/api/v1/products").status_code == 401
    assert client.get("/api/v1/products", headers={"X-API-Key": "incorrect"}).status_code == 401


def test_remote_access_and_untrusted_host_are_rejected(application):
    with TestClient(
        application, base_url="http://localhost", client=("192.0.2.1", 50000)
    ) as remote:
        assert remote.get("/health", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    with TestClient(
        application, base_url="http://untrusted.example", client=("127.0.0.1", 50000)
    ) as untrusted:
        assert untrusted.get("/api/v1/products", headers=HEADERS).status_code == 400


def test_no_key_configuration_fails_closed(application, client):
    application.state.settings.admin_api_key = None
    assert client.get("/api/v1/products", headers=HEADERS).status_code == 503
    assert client.get("/health").status_code == 200


def test_product_crud_and_affiliate_link_reuse(client, application):
    product = create(client)
    product_id = product["id"]
    base = f"/api/v1/products/{product_id}"
    assert product["external_id"] == "MLB3810190843"
    assert product["affiliate_pending"] is True
    assert client.get(base + "/affiliate-link", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/products", headers=HEADERS).json()[0]["id"] == product_id
    assert client.post("/api/v1/products", json=PRODUCT, headers=HEADERS).status_code == 409
    assert (
        client.patch(base, json={"name": "Novo nome", "priority": True}, headers=HEADERS).json()[
            "priority"
        ]
        is True
    )
    link_url = "https://meli.la/1JQSATy"
    first = client.put(base + "/affiliate-link", json={"url": link_url}, headers=HEADERS)
    assert first.status_code == 200
    second = client.put(base + "/affiliate-link", json={"url": link_url}, headers=HEADERS)
    assert second.json()["id"] == first.json()["id"]
    assert client.get(base + "/affiliate-link", headers=HEADERS).json()["url"] == link_url
    assert client.get(base, headers=HEADERS).json()["affiliate_pending"] is False
    assert client.delete(base + "/affiliate-link", headers=HEADERS).status_code == 204
    assert client.get(base, headers=HEADERS).json()["affiliate_pending"] is True
    client.put(base + "/affiliate-link", json={"url": link_url}, headers=HEADERS)
    assert client.post(base + "/monitor", headers=HEADERS).status_code == 201
    assert client.delete(base, headers=HEADERS).status_code == 204
    assert client.get(base, headers=HEADERS).status_code == 404
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AffiliateLink)) == 0
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 0


def test_monitoring_is_independent_from_affiliate_link(client):
    product = create(client)
    base = f"/api/v1/products/{product['id']}"
    result = client.post(base + "/monitor", headers=HEADERS)
    assert result.status_code == 201
    observation = result.json()
    assert Decimal(observation["current_price"]) == Decimal("99.90")
    assert observation["previous_price"] is None
    assert observation["is_simulated"] is True
    assert observation["source"] == "simulated"
    assert observation["observed_at"].endswith("Z")
    assert client.get(base, headers=HEADERS).json()["affiliate_pending"] is True
    second = client.post(base + "/monitor", headers=HEADERS).json()
    history = client.get(base + "/price-history", headers=HEADERS).json()
    assert len(history) == 2
    assert history[0]["id"] == second["id"]
    assert len(client.get(base + "/price-history?limit=1&offset=1", headers=HEADERS).json()) == 1
    assert (
        client.patch(base + "/monitoring", json={"enabled": False}, headers=HEADERS).status_code
        == 200
    )
    assert client.post(base + "/monitor", headers=HEADERS).status_code == 409
    assert len(client.get(base + "/price-history", headers=HEADERS).json()) == 2
    client.patch(base + "/monitoring", json={"enabled": True}, headers=HEADERS)
    assert client.post(base + "/monitor", headers=HEADERS).status_code == 201


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "   "},
        {"original_url": "https://mercadolivre.com.br.attacker.example/MLB-12345"},
        {"original_url": "http://produto.mercadolivre.com.br/MLB-3810190843-x"},
        {"external_id": "MLB0000"},
        {"unknown_field": True},
    ],
)
def test_invalid_product_input(client, changes):
    response = client.post("/api/v1/products", json={**PRODUCT, **changes}, headers=HEADERS)
    assert response.status_code == 422
    assert client.get("/api/v1/products", headers=HEADERS).json() == []


def test_patch_does_not_reassign_existing_product_identity(client):
    product = create(client)
    base = f"/api/v1/products/{product['id']}"
    assert client.patch(base, json={"name": None}, headers=HEADERS).status_code == 422
    assert (
        client.patch(
            base,
            json={"original_url": "https://produto.mercadolivre.com.br/MLB-999999-outro_JM"},
            headers=HEADERS,
        ).status_code
        == 422
    )
    assert client.get(base, headers=HEADERS).json()["external_id"] == "MLB3810190843"


def test_invalid_affiliate_link_and_not_found(client):
    product = create(client)
    base = f"/api/v1/products/{product['id']}"
    for url in (
        "https://example.org/offer",
        "https://shope.ee/example",
        "https://user:pass@meli.la/example",
    ):
        assert (
            client.put(base + "/affiliate-link", json={"url": url}, headers=HEADERS).status_code
            == 422
        )
    assert client.get(base, headers=HEADERS).json()["affiliate_pending"] is True
    assert client.get("/api/v1/products/999999/price-history", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/products?limit=101", headers=HEADERS).status_code == 422


def test_database_failures_are_sanitized(client, application, monkeypatch):
    def fail(*args, **kwargs):
        raise OperationalError("secret-db-url", {}, Exception("secret-password"))

    monkeypatch.setattr(application.state.engine, "connect", fail)
    health = client.get("/health")
    assert health.status_code == 503
    response = client.get("/api/v1/products", headers=HEADERS)
    assert response.status_code == 503
    assert "secret" not in response.text


@pytest.mark.parametrize("value", ["too-short", " key-that-is-long-enough-with-leading-space"])
def test_configuration_rejects_weak_or_padded_keys(value):
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None, admin_api_key=value)
    assert value not in str(error.value)
