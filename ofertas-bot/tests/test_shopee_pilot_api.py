from uuid import uuid4

import test_api as api
import test_shopee_collection as collection

application = api.application
client = api.client
no_network = collection.no_network


def test_pilot_routes_auth_validation_and_isolation(client):
    route = "/api/v1/shopee-collection/recurring-pilots"
    identity = str(uuid4())
    data = dict(
        authorization_id=identity,
        product_id=2,
        shop_id="1904573791",
        item_id="23199869203",
        actor="responsavel",
        valid_from="2026-10-30T15:00:00+00:00",
        expires_at="2026-10-31T15:00:00+00:00",
        max_requests=2,
        min_interval_seconds=21600,
    )
    confirmation = dict(
        scope_sha256="0" * 64,
        actor="responsavel",
        confirmed_experimental_pilot=True,
        acknowledge_unknown_quotas=True,
    )
    assert client.post(route, json=data).status_code == 401
    assert client.post(route, json=data, headers=api.HEADERS).status_code == 409
    assert (
        client.post(route, json={**data, "offline": True}, headers=api.HEADERS).status_code == 422
    )
    assert client.get(route + "/" + identity, headers=api.HEADERS).status_code == 404
    for tail in ("approve", "cycles/" + str(uuid4()) + "/execute"):
        path = route + "/" + identity + "/" + tail
        assert client.post(path, json=confirmation).status_code == 401
        assert client.post(path, json=confirmation, headers=api.HEADERS).status_code == 409
        assert (
            client.post(
                path,
                json={**confirmation, "acknowledge_unknown_quotas": False},
                headers=api.HEADERS,
            ).status_code
            == 422
        )
