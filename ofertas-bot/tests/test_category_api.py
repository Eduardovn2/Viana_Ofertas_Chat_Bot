"""Administrative category curation and conservative routing through the real API."""

import pytest
from test_api import HEADERS, PRODUCT
from test_api import application as application_fixture
from test_api import client as client_fixture

application = application_fixture
client = client_fixture


def seed(client):
    response = client.post("/api/v1/categories/seed", headers=HEADERS)
    assert response.status_code == 200, response.text
    categories = client.get("/api/v1/categories", headers=HEADERS).json()
    return {category["slug"]: category["id"] for category in categories}


def product(client, **updates):
    response = client.post("/api/v1/products", json={**PRODUCT, **updates}, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def assignments(client, product_id):
    response = client.get(f"/api/v1/products/{product_id}/categories", headers=HEADERS)
    assert response.status_code == 200
    return response.json()


def test_catalog_seed_and_house_preferences_are_idempotent_and_preserved(client):
    categories = seed(client)
    assert len(categories) == 9
    repeated = client.post("/api/v1/categories/seed", headers=HEADERS).json()
    assert repeated["categories_created"] == 0
    assert repeated["destination_created"] is False
    destinations = client.get("/api/v1/destinations", headers=HEADERS).json()
    assert len(destinations) == 1
    house = destinations[0]
    assert house["name"] == "Ofertas de Casa"
    assert house["enabled"] is False
    assert house["channel"] == "SIMULATED"
    prefs_url = f"/api/v1/destinations/{house['id']}/categories"
    preferences = client.get(prefs_url, headers=HEADERS).json()
    assert preferences["mode"] == "selected"
    assert len(preferences["category_ids"]) == 8
    assert categories["gerais"] not in preferences["category_ids"]
    client.put(prefs_url, json={"mode": "all", "category_ids": []}, headers=HEADERS)
    client.patch(
        f"/api/v1/categories/{categories['moda']}",
        json={"name": "Moda revisada", "active": False},
        headers=HEADERS,
    )
    client.post("/api/v1/categories/seed", headers=HEADERS)
    assert client.get(prefs_url, headers=HEADERS).json()["mode"] == "all"
    assert (
        client.get(f"/api/v1/categories/{categories['moda']}", headers=HEADERS).json()["active"]
        is False
    )


def test_category_crud_validation_and_referenced_delete(client):
    response = client.post(
        "/api/v1/categories", json={"slug": "esportes", "name": "Esportes"}, headers=HEADERS
    )
    assert response.status_code == 201
    category_id = response.json()["id"]
    assert (
        client.post(
            "/api/v1/categories", json={"slug": "esportes", "name": "Duplicada"}, headers=HEADERS
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/categories/{category_id}", json={"slug": "outro"}, headers=HEADERS
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/v1/categories/{category_id}", json={"active": False}, headers=HEADERS
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/categories?active=false", headers=HEADERS).json()[0]["id"]
        == category_id
    )
    assert client.delete(f"/api/v1/categories/{category_id}", headers=HEADERS).status_code == 204
    assert client.get(f"/api/v1/categories/{category_id}", headers=HEADERS).status_code == 404
    categories = seed(client)
    assert (
        client.delete(
            f"/api/v1/categories/{categories['casa-decoracao']}", headers=HEADERS
        ).status_code
        == 409
    )
    assert client.get("/api/v1/categories?limit=101", headers=HEADERS).status_code == 422


def test_manual_correction_survives_product_edit_and_automatic_reclassification(client):
    categories = seed(client)
    product_id = product(client, name="CAMISETA INFANTIL")
    assert {row["slug"] for row in assignments(client, product_id)["categories"]} == {
        "moda",
        "infantil",
    }
    url = f"/api/v1/products/{product_id}"
    response = client.put(
        url + "/categories", json={"category_ids": [categories["games"]]}, headers=HEADERS
    )
    assert response.status_code == 200
    client.patch(url, json={"name": "Sofá", "description": "Furadeira"}, headers=HEADERS)
    classified = client.post(url + "/reclassify", headers=HEADERS).json()
    assert classified["manual_override"] is True
    assert [row["slug"] for row in classified["categories"]] == ["games"]
    assert classified["categories"][0]["source"] == "manual"
    assert classified["categories"][0]["explanation"]
    assert (
        client.put(url + "/categories", json={"category_ids": []}, headers=HEADERS).json()[
            "manual_override"
        ]
        is True
    )
    assert client.post(url + "/reclassify", headers=HEADERS).json()["categories"] == []
    restored = client.delete(url + "/categories/manual-override", headers=HEADERS).json()
    assert restored["manual_override"] is False
    assert {row["slug"] for row in restored["categories"]} == {
        "casa-decoracao",
        "materiais-ferramentas",
    }


def test_store_precedence_fallback_and_metadata_limits(client):
    product_id = product(
        client,
        name="Furadeira",
        classification_metadata={
            "store_category_trusted": True,
            "store_category_slugs": ["beleza"],
        },
    )
    row = assignments(client, product_id)["categories"][0]
    assert row["slug"] == "beleza" and row["source"] == "store"
    url = f"/api/v1/products/{product_id}"
    assert (
        client.patch(
            url,
            json={
                "name": "Nome desconhecido",
                "description": None,
                "classification_metadata": None,
            },
            headers=HEADERS,
        ).status_code
        == 200
    )
    assert assignments(client, product_id)["categories"][0]["slug"] == "gerais"
    assert (
        client.patch(
            url, json={"classification_metadata": {"public_text": "x" * 20000}}, headers=HEADERS
        ).status_code
        == 422
    )
    assert (
        client.patch(
            url, json={"classification_metadata": {"public_text": "á" * 10000}}, headers=HEADERS
        ).status_code
        == 422
    )


def test_unconfigured_destination_does_not_queue_and_configuration_revokes_ready_item(client):
    categories = seed(client)
    product_id = product(client, name="Mouse gamer")
    url = f"/api/v1/products/{product_id}"
    client.put(url + "/affiliate-link", json={"url": "https://meli.la/teste"}, headers=HEADERS)
    destination = client.post(
        "/api/v1/destinations",
        json={"name": "Destino local", "external_id": "category-test"},
        headers=HEADERS,
    ).json()
    assert destination["category_mode"] == "unconfigured"
    configure = "/api/v1/monitoring/config"
    client.patch(
        configure,
        json={"provider_min_interval_seconds": "0", "mock_current_price": "100.00"},
        headers=HEADERS,
    )
    client.post(url + "/monitor", headers=HEADERS)
    client.patch(configure, json={"mock_current_price": "80.00"}, headers=HEADERS)
    client.post(url + "/monitor", headers=HEADERS)
    assert client.get("/api/v1/queue", headers=HEADERS).json() == []
    prefs = f"/api/v1/destinations/{destination['id']}/categories"
    response = client.put(
        prefs,
        json={"mode": "selected", "category_ids": [categories["games"], categories["tecnologia"]]},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert client.post(url + "/monitor", headers=HEADERS).status_code == 201
    queue = client.get("/api/v1/queue?state=READY", headers=HEADERS).json()
    assert len(queue) == 1
    # Removing one match still permits the other; removing both revokes the item.
    client.put(
        prefs, json={"mode": "selected", "category_ids": [categories["games"]]}, headers=HEADERS
    )
    assert len(client.get("/api/v1/queue?state=READY", headers=HEADERS).json()) == 1
    client.put(
        prefs, json={"mode": "selected", "category_ids": [categories["beleza"]]}, headers=HEADERS
    )
    assert client.get("/api/v1/queue?state=READY", headers=HEADERS).json() == []
    assert (
        client.post(f"/api/v1/queue/{queue[0]['id']}/simulate", headers=HEADERS).json()["status"]
        == "EXPIRED"
    )


def test_category_deactivation_excludes_existing_queue_and_manual_empty_is_safe(client):
    categories = seed(client)
    product_id = product(client, name="Perfume")
    url = f"/api/v1/products/{product_id}"
    client.put(url + "/affiliate-link", json={"url": "https://meli.la/teste"}, headers=HEADERS)
    client.post(
        "/api/v1/destinations",
        json={"name": "Teste", "external_id": "deactivation", "category_mode": "all"},
        headers=HEADERS,
    )
    client.patch(
        "/api/v1/monitoring/config",
        json={
            "provider_min_interval_seconds": "0",
            "mock_current_price": "80",
            "mock_previous_price": "100",
        },
        headers=HEADERS,
    )
    client.post(url + "/monitor", headers=HEADERS)
    assert len(client.get("/api/v1/queue?state=READY", headers=HEADERS).json()) == 1
    client.patch(
        f"/api/v1/categories/{categories['beleza']}", json={"active": False}, headers=HEADERS
    )
    assert client.get("/api/v1/queue?state=READY", headers=HEADERS).json() == []
    assert client.post(url + "/monitor", headers=HEADERS).status_code == 201
    assert client.get("/api/v1/queue?state=READY", headers=HEADERS).json() == []


@pytest.mark.parametrize(
    "path", ["/categories", "/products/1/categories", "/destinations/1/categories"]
)
def test_category_routes_require_admin_key(client, path):
    assert client.get("/api/v1" + path).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "unknown", "category_ids": []},
        {"mode": "all", "category_ids": [1]},
        {"mode": "selected", "category_ids": [1, 1]},
        {"mode": "selected", "category_ids": [0]},
    ],
)
def test_destination_preferences_validate_input(client, body):
    seed(client)
    destination = client.get("/api/v1/destinations", headers=HEADERS).json()[0]
    assert (
        client.put(
            f"/api/v1/destinations/{destination['id']}/categories", json=body, headers=HEADERS
        ).status_code
        == 422
    )


def test_unknown_ids_and_null_curation_fields_have_coherent_errors(client):
    categories = seed(client)
    product_id = product(client)
    url = f"/api/v1/products/{product_id}/categories"
    assert client.put(url, json={"category_ids": [999999]}, headers=HEADERS).status_code == 404
    assert (
        client.put(
            url, json={"category_ids": [categories["moda"], categories["moda"]]}, headers=HEADERS
        ).status_code
        == 422
    )
    assert client.get("/api/v1/products/999999/categories", headers=HEADERS).status_code == 404
    assert (
        client.patch(
            f"/api/v1/categories/{categories['moda']}", json={"active": None}, headers=HEADERS
        ).status_code
        == 422
    )
