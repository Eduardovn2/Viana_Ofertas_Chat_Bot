"""Strict product identity and opaque tracking preservation; synthetic HTTP only."""

import logging

import httpx
import pytest
import test_api as api_fixtures
from pydantic import SecretStr
from sqlalchemy import func, select
from test_api import HEADERS, create

from app.integrations.contracts import CapabilityStatus
from app.integrations.errors import InvalidPayload
from app.integrations.http import JsonTransport
from app.integrations.shopee import ShopeeConnector, _link_target_verified, _url_identity
from app.models import AffiliateLink, Platform
from app.services.products import validate_affiliate_url

ORIGIN = "https://shopee.com.br/Cockpit-SYNTHETIC-i.1904573791.23199869203"
IDENTITY = ("1904573791", "23199869203")
TRACKING = "?utm_source=SYNTHETIC%2Bowner&signature=SYNTHETIC%2Fvalue%3D&tag=a&tag=b"
UNIVERSAL = ORIGIN.replace(".com.br/", ".com.br/universal-link/")
application = api_fixtures.application
client = api_fixtures.client


@pytest.mark.parametrize(
    "url",
    [
        ORIGIN,
        ORIGIN + "/",
        ORIGIN + TRACKING,
        "https://shopee.com.br/product/1904573791/23199869203",
        "https://www.shopee.com.br/product/1904573791/23199869203/",
        UNIVERSAL,
        UNIVERSAL + "/",
        UNIVERSAL + TRACKING,
        UNIVERSAL.replace("Cockpit-SYNTHETIC", "Estrutura-em-a%C3%A7o"),
    ],
)
def test_canonical_and_specific_universal_paths_identify_the_original_product(url):
    assert _url_identity(url) == IDENTITY
    assert _link_target_verified(url, IDENTITY) is True


@pytest.mark.parametrize("prefix", ["", "universal-link/"])
@pytest.mark.parametrize("ids", ["1904573792.23199869203", "1904573791.23199869204"])
def test_wrong_product_is_rejected_without_disclosing_tracking(prefix, ids):
    url = f"https://shopee.com.br/{prefix}SYNTHETIC-i.{ids}{TRACKING}"
    with pytest.raises(InvalidPayload) as caught:
        _link_target_verified(url, IDENTITY)
    assert url not in str(caught.value)
    assert "SYNTHETIC" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        None,
        123,
        "",
        UNIVERSAL.replace("shopee.com.br", "shopee.com.br.evil.test"),
        UNIVERSAL.replace("shopee.com.br", "evil-shopee.com.br"),
        UNIVERSAL.replace("shopee.com.br", "s.shopee.com.br"),
        UNIVERSAL.replace("shopee.com.br", "shopee.com.br@evil.test"),
        UNIVERSAL.replace("shopee.com.br", "user@shopee.com.br"),
        UNIVERSAL.replace("https:", "http:"),
        UNIVERSAL.replace("shopee.com.br", "shopee.com.br:444"),
        UNIVERSAL.replace("shopee.com.br", "shopee.com.br:"),
        UNIVERSAL.replace("/universal-link/", "/universal-link//"),
        UNIVERSAL.replace("/universal-link/", "/universal-link/../"),
        UNIVERSAL.replace("/universal-link/", "/universal-link/./"),
        UNIVERSAL.replace("/universal-link/", "/universal-link/universal-link/"),
        UNIVERSAL.replace("/universal-link/", "/unknown/"),
        UNIVERSAL.replace("Cockpit", "%FFCockpit"),
        UNIVERSAL.replace("Cockpit", "%ZZCockpit"),
        UNIVERSAL.replace("Cockpit", "%00Cockpit"),
        UNIVERSAL.replace("Cockpit", "%20Cockpit"),
        UNIVERSAL.replace("Cockpit", "%2fCockpit"),
        UNIVERSAL.replace("Cockpit", "%5cCockpit"),
        UNIVERSAL.replace("1904573791", "0"),
        UNIVERSAL.replace("23199869203", "-1"),
        UNIVERSAL.replace("23199869203", "01"),
        UNIVERSAL.replace("23199869203", "9223372036854775808"),
        UNIVERSAL + "/extra",
        UNIVERSAL + "#context",
        "https://shopee.com.br/universal-link/product/1904573791/23199869203",
        "https://shopee.com.br/unknown-deep-link?shopId=1904573791&itemId=23199869203",
        "https://shopee.com.br/universal-link/?url=" + ORIGIN,
        "https://shopee.com.br/universal-link/Cockpit-SYNTHETIC",
        "https://shopee.com.br/universal-link/-i.1904573791.23199869203",
    ],
)
def test_unsafe_or_unproven_identity_paths_are_rejected(url):
    with pytest.raises(InvalidPayload):
        _url_identity(url)
    with pytest.raises(InvalidPayload):
        _link_target_verified(url, IDENTITY)


@pytest.mark.parametrize("long_url", [ORIGIN + TRACKING, UNIVERSAL + TRACKING])
def test_mocked_response_preserves_tracking_but_never_proves_account_attribution(long_url, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "generateShortLink": {
                        "shortLink": "https://s.shopee.com.br/SYNTHETIC-LINK",
                        "longLink": long_url,
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = JsonTransport(client=client, min_interval=0)
        source = ShopeeConnector(
            transport,
            app_id=SecretStr("SYNTHETIC_APP"),
            secret=SecretStr("SYNTHETIC_SECRET"),
            enabled=True,
            short_links_enabled=True,
        )
        with caplog.at_level(logging.DEBUG):
            result = source.generate_affiliate_link(ORIGIN)

    assert len(calls) == 1  # Mock only, no link resolution or other request.
    assert result.product_identity_verified is True
    assert result.account_verified is False
    assert result.long_url == long_url
    assert validate_affiliate_url(Platform.SHOPEE, result.long_url) == long_url
    assert result.url == "https://s.shopee.com.br/SYNTHETIC-LINK"
    assert TRACKING not in repr(result)
    assert "SYNTHETIC%2Fvalue" not in caplog.text + repr(result)
    assert "SYNTHETIC%2Bowner" not in caplog.text + repr(result)
    assert {c.name: c.status for c in source.capabilities()}[
        "generate_affiliate_link"
    ] is CapabilityStatus.REQUIRES_CREDENTIALS


def test_tracking_output_cannot_be_reused_as_conversion_origin():
    source = ShopeeConnector(
        None,
        app_id=SecretStr("SYNTHETIC_APP"),
        secret=SecretStr("SYNTHETIC_SECRET"),
        enabled=True,
        short_links_enabled=True,
    )
    with pytest.raises(InvalidPayload):
        source.generate_affiliate_link(UNIVERSAL)


@pytest.mark.parametrize("selector", ["SYNTHETIC", ""])
def test_universal_variant_selector_keeps_exact_variant_unverified(selector):
    url = UNIVERSAL + "?variation_id=" + selector
    assert _url_identity(url) == IDENTITY
    assert _link_target_verified(url, IDENTITY) is False


def test_admin_cannot_automatically_associate_an_identity_only_result(client, application):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "generateShortLink": {
                        "shortLink": "https://s.shopee.com.br/SYNTHETIC-LINK",
                        "longLink": UNIVERSAL + TRACKING,
                    }
                }
            },
        )

    product = create(client, platform="SHOPEE", original_url=ORIGIN)
    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
        source = ShopeeConnector(
            JsonTransport(client=mock_client, min_interval=0),
            app_id=SecretStr("SYNTHETIC_APP"),
            secret=SecretStr("SYNTHETIC_SECRET"),
            enabled=True,
            short_links_enabled=True,
        )
        application.state.integrations._connectors[Platform.SHOPEE] = source
        response = client.post(
            f"/api/v1/products/{product['id']}/affiliate-link/generate", headers=HEADERS
        )
    assert response.status_code == 409
    assert "SYNTHETIC%2Fvalue" not in response.text
    assert len(calls) == 0  # Legacy real route is blocked before any connector I/O.
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AffiliateLink)) == 0


def test_manual_storage_preserves_long_link_query_exactly_in_isolated_sqlite(client, application):
    product = create(client, platform="SHOPEE", original_url=ORIGIN)
    long_url = UNIVERSAL + TRACKING
    response = client.put(
        f"/api/v1/products/{product['id']}/affiliate-link",
        headers=HEADERS,
        json={"url": long_url},
    )
    assert response.status_code == 200
    with application.state.session_factory() as session:
        assert session.scalar(select(AffiliateLink.url)) == long_url
