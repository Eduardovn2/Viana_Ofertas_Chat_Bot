"""Exercise the complete simulated pipeline through authenticated HTTP routes."""

from decimal import Decimal

from fastapi.testclient import TestClient
from test_api import HEADERS, PRODUCT
from test_api import application as application_fixture
from test_api import client as client_fixture

from app.main import create_app

application = application_fixture
client = client_fixture


def configure(client, **updates):
    response = client.patch("/api/v1/monitoring/config", json=updates, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def create_product(client):
    # Keep the previous queue scenarios explicitly opted in to all categories.
    destination = client.post(
        "/api/v1/destinations",
        json={
            "name": "Simulação",
            "external_id": "simulation",
            "category_mode": "all",
        },
        headers=HEADERS,
    )
    assert destination.status_code == 201
    response = client.post("/api/v1/products", json=PRODUCT, headers=HEADERS)
    assert response.status_code == 201
    return response.json()["id"]


def test_monitoring_controls_never_start_scheduler_in_tests(client):
    status = client.get("/api/v1/monitoring/status", headers=HEADERS)
    assert status.status_code == 200
    assert status.json()["running"] is False
    assert status.json()["threads_blocked"] is True
    assert status.json()["configuration"]["priority_interval_seconds"] == 60
    assert status.json()["configuration"]["general_interval_seconds"] == 300
    assert (
        client.post("/api/v1/monitoring/pause", headers=HEADERS).json()["scheduler_enabled"]
        is False
    )
    assert (
        client.post("/api/v1/monitoring/resume", headers=HEADERS).json()["scheduler_enabled"]
        is True
    )
    assert client.get("/api/v1/monitoring/status").status_code == 401


def test_full_simulated_pipeline_and_queue_deduplication(client):
    product_id = create_product(client)
    product_url = f"/api/v1/products/{product_id}"
    client.put(
        product_url + "/affiliate-link", json={"url": "https://meli.la/1JQSATy"}, headers=HEADERS
    )
    client.patch(product_url, json={"priority": True}, headers=HEADERS)
    configure(client, provider_min_interval_seconds="0", mock_current_price="100.00")
    assert client.post(product_url + "/monitor", headers=HEADERS).status_code == 201
    configure(client, mock_current_price="80.00")
    response = client.post(product_url + "/monitor", headers=HEADERS)
    assert response.status_code == 201, response.text
    offers = client.get("/api/v1/offers", headers=HEADERS).json()
    assert len(offers) == 1
    assert Decimal(offers[0]["current_price"]) == Decimal("80")
    assert Decimal(offers[0]["historical_price"]) == Decimal("100")
    assert offers[0]["previous_price"] is None
    assert offers[0]["coupon_id"] is None
    assert Decimal(offers[0]["score"]) > 0
    variations = client.get(product_url + "/variations", headers=HEADERS).json()
    assert variations[0]["direction"] == "DROP"
    assert Decimal(variations[0]["percentage"]) == Decimal("-20.00")
    assert variations[0]["store_previous_price"] is None
    queue = client.get("/api/v1/queue?state=READY", headers=HEADERS).json()
    assert len(queue) == 1
    assert queue[0]["is_simulated"] is True
    assert len(client.get("/api/v1/destinations", headers=HEADERS).json()) == 1
    configure(client, mock_current_price="75.00")
    assert client.post(product_url + "/monitor", headers=HEADERS).status_code == 201
    assert len(client.get("/api/v1/queue?state=READY", headers=HEADERS).json()) == 1
    item_id = queue[0]["id"]
    sent = client.post(f"/api/v1/queue/{item_id}/simulate", headers=HEADERS)
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "PUBLISHED"
    assert sent.json()["is_simulated"] is True
    # An attempted repeat cannot produce a second publication.
    client.post(f"/api/v1/queue/{item_id}/simulate", headers=HEADERS)
    terminal = client.get("/api/v1/queue", headers=HEADERS).json()
    assert len([item for item in terminal if item["status"] == "PUBLISHED"]) == 1
    assert (
        client.get("/api/v1/monitoring/jobs", headers=HEADERS).json()[0]["product_id"] == product_id
    )


def test_pending_without_affiliate_and_exclusive_prices_are_excluded(client):
    product_id = create_product(client)
    product_url = f"/api/v1/products/{product_id}"
    configure(client, provider_min_interval_seconds="0", mock_current_price="100.00")
    client.post(product_url + "/monitor", headers=HEADERS)
    configure(client, mock_current_price="90.00")
    client.post(product_url + "/monitor", headers=HEADERS)
    queue = client.get("/api/v1/queue", headers=HEADERS).json()
    assert queue[0]["status"] == "PENDING"
    configure(client, mock_current_price="50.00", mock_price_access="EXCLUSIVE")
    client.post(product_url + "/monitor", headers=HEADERS)
    assert client.get("/api/v1/queue?state=READY", headers=HEADERS).json() == []
    assert (
        client.get(product_url + "/variations", headers=HEADERS).json()[0]["direction"]
        == "EXCLUDED"
    )


def test_configuration_validation_and_simulated_destinations(client):
    for data in (
        {"priority_interval_seconds": 0},
        {"score_price_drop": -1},
        {"mock_current_price": "0"},
        {"scheduler_enabled": None},
        {"telegram_token": "not-accepted"},
        {"mock_current_price": "1.001"},
    ):
        assert (
            client.patch("/api/v1/monitoring/config", json=data, headers=HEADERS).status_code == 422
        )
    configure(client, mock_previous_price="120.00")
    assert configure(client, mock_previous_price=None)["mock_previous_price"] is None
    response = client.post(
        "/api/v1/destinations",
        json={"name": "Simulação", "external_id": "simulation-2"},
        headers=HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["channel"] == "SIMULATED"
    assert (
        client.post(
            "/api/v1/destinations",
            json={"name": "Não permitido", "external_id": "123", "channel": "TELEGRAM"},
            headers=HEADERS,
        ).status_code
        == 422
    )
    assert client.post("/api/v1/queue/999999/simulate", headers=HEADERS).status_code == 404


def test_manual_monitoring_respects_provider_limit_even_when_scheduler_paused(client):
    product_id = create_product(client)
    configure(client, provider_min_interval_seconds="30", scheduler_enabled=False)
    url = f"/api/v1/products/{product_id}/monitor"
    assert client.post(url, headers=HEADERS).status_code == 201
    assert client.post(url, headers=HEADERS).status_code == 429


def test_configuration_survives_application_restart(client, application):
    configure(
        client, priority_interval_seconds=17, mock_current_price="42.12", scheduler_enabled=False
    )
    restarted = create_app(application.state.settings)
    with TestClient(restarted, base_url="http://localhost", client=("127.0.0.1", 50001)) as other:
        configuration = other.get("/api/v1/monitoring/config", headers=HEADERS).json()
        assert configuration["priority_interval_seconds"] == 17
        assert Decimal(configuration["mock_current_price"]) == Decimal("42.12")
        assert configuration["scheduler_enabled"] is False
        status = other.get("/api/v1/monitoring/status", headers=HEADERS).json()
        assert status["enabled"] is False
        assert status["paused"] is True
