"""Post-audit regressions: identity, fail-closed generation, evidence and comparability."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import test_api as api
import test_offers as prices
from sqlalchemy import func, select

from app.models import (
    AffiliateLink,
    Availability,
    IntegrationEvent,
    Offer,
    OfferStatus,
    Platform,
    PriceAccess,
    ProductPriceHistory,
    SourceType,
)
from app.services.affiliate_links import validate_link_for_product
from app.services.comparison import PriceComparisonService, PriceDirection
from app.services.offers import _carried_historical_price
from app.services.shopee_evidence import (
    CommercialEvidenceInput,
    EvidenceKind,
    LinkEvidenceInput,
    TrustPolicyProposal,
    evidence_readiness,
    link_fingerprint,
    propose_trust_policy,
    record_commercial_evidence,
    record_manual_link_evidence,
)

application = api.application
client = api.client
session = prices.session
product = prices.product
BASE = "https://shopee.com.br/product/7001/1"
LONG = "https://shopee.com.br/universal-link/SYNTHETIC-i.7001.1"
TRACKING = "?tag=a&tag=b&signature=SYNTHETIC%2F%2B%3D"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Real HTTP prohibited in post-audit validation")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)


@pytest.mark.parametrize(
    "path",
    [
        "/universal-link/SYNTHETIC-i.999.888",
        "/SYNTHETIC-i.999.888",
        "/product/999/888",
        "/unknown?shopId=7001&itemId=1",
        "/universal-link/?url=" + BASE,
        "/universal-link//SYNTHETIC-i.7001.1",
        "/universal-link/%252fSYNTHETIC-i.7001.1",
        "/universal-link/%00SYNTHETIC-i.7001.1",
        "/universal-link/../SYNTHETIC-i.7001.1",
    ],
)
def test_manual_mismatched_or_unproven_paths_rejected(client, path):
    p = api.create(client, platform="SHOPEE", original_url=BASE, external_id="7001:1")
    response = client.put(
        f"/api/v1/products/{p['id']}/affiliate-link",
        headers=api.HEADERS,
        json={"url": "https://shopee.com.br" + path},
    )
    assert response.status_code == 422
    assert "signature" not in response.text


@pytest.mark.parametrize("url", [BASE, LONG, LONG + TRACKING])
def test_shared_validator_preserves_valid_manual_urls_without_account_promotion(client, url):
    p = api.create(client, platform="SHOPEE", original_url=BASE, external_id="7001:1")
    response = client.put(
        f"/api/v1/products/{p['id']}/affiliate-link", headers=api.HEADERS, json={"url": url}
    )
    assert response.status_code == 200
    assert response.json()["url"] == url
    assert response.json()["source"] == "MANUAL_SHOPEE"


@pytest.mark.parametrize(
    "url",
    [
        LONG.replace("https:", "http:"),
        LONG.replace("shopee.com.br", "user@shopee.com.br"),
        LONG.replace("shopee.com.br", "shopee.com.br.evil.test"),
        LONG + "?variation_id=999",
    ],
)
def test_manual_strict_domain_and_known_variant(url):
    p = SimpleNamespace(
        platform=Platform.SHOPEE, external_id="7001:1", original_url=BASE, variant_id="42"
    )
    with pytest.raises(ValueError):
        validate_link_for_product(p, url)


def test_matching_variant_does_not_alter_tracking():
    url = LONG + "?variation_id=42&tag=SYNTHETIC%2Fvalue"
    p = SimpleNamespace(
        platform=Platform.SHOPEE, external_id="7001:1", original_url=BASE, variant_id="42"
    )
    validate_link_for_product(p, url)
    assert url.endswith("tag=SYNTHETIC%2Fvalue")


def test_repeated_concurrent_admin_generation_is_blocked_before_connector(client, application):
    p = api.create(client, platform="SHOPEE", original_url=BASE, external_id="7001:1")
    calls = []

    def forbidden(*args):
        calls.append(1)
        raise AssertionError("No unreserved real entry point")

    application.state.integrations._connectors[Platform.SHOPEE] = SimpleNamespace(
        generate_affiliate_link=forbidden
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda _: client.post(
                    f"/api/v1/products/{p['id']}/affiliate-link/generate", headers=api.HEADERS
                ),
                range(6),
            )
        )
    assert all(r.status_code == 409 for r in results)
    assert all(r.json()["detail"]["code"] == "REAL_GENERATION_BLOCKED" for r in results)
    assert calls == []
    assert client.post(f"/api/v1/products/{p['id']}/affiliate-link/generate").status_code == 401


def evidence_product(session, product):
    product.platform = Platform.SHOPEE
    product.external_id = "7001:1"
    product.original_url = BASE
    session.add(AffiliateLink(product_id=product.id, url=LONG + TRACKING, source="MANUAL_SHOPEE"))
    session.flush()
    session.refresh(product)
    return product


def documentary(now):
    return dict(actor="responsavel_local", reference="document:SYNTHETIC_EVIDENCE", verified_at=now)


def test_separate_trust_dimensions_without_previous_sale_or_account_promotion(session, product):
    product = evidence_product(session, product)
    now = datetime.now(UTC)
    policy = propose_trust_policy(
        session,
        product,
        TrustPolicyProposal(**documentary(now), require_authenticated_generation=False),
        now=now,
    )
    for kind in (
        EvidenceKind.PRODUCT_IDENTITY,
        EvidenceKind.CREDENTIAL_PROVENANCE,
        EvidenceKind.OWNERSHIP_ATTESTATION,
    ):
        record_manual_link_evidence(
            session,
            product,
            LinkEvidenceInput(
                **documentary(now),
                kind=kind,
                account_reference="account:SYNTHETIC",
                credential_reference="credential:SYNTHETIC",
                link_sha256=link_fingerprint(product.affiliate_link.url),
            ),
            now=now,
        )
    result = evidence_readiness(session, product, policy["id"], now=now)
    assert result["link_criteria_met"] is True  # no sale universally required
    assert (
        result["usable"] is result["account_verified"] is result["publication_authorized"] is False
    )
    assert result["commercial_evidence_sufficient"] is False
    assert product.affiliate_link.source == "MANUAL_SHOPEE"
    assert session.scalar(select(func.count()).select_from(IntegrationEvent)) == 4
    changed = product.affiliate_link.url + "&changed=1"
    product.affiliate_link.url = changed
    assert evidence_readiness(session, product, policy["id"], now=now)["link_criteria_met"] is False


@pytest.mark.parametrize(
    "kind", [EvidenceKind.AUTHENTICATED_GENERATION, EvidenceKind.COMMERCIAL_ATTESTATION]
)
def test_manual_claim_cannot_impersonate_api_generation_or_commercial_evidence(
    session, product, kind
):
    product = evidence_product(session, product)
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        record_manual_link_evidence(
            session,
            product,
            LinkEvidenceInput(
                **documentary(now),
                kind=kind,
                account_reference="account:SYNTHETIC",
                credential_reference="credential:SYNTHETIC",
                link_sha256=link_fingerprint(product.affiliate_link.url),
            ),
            now=now,
        )


def test_commission_and_conversion_are_independent_opt_in_proofs(session, product):
    product = evidence_product(session, product)
    now = datetime.now(UTC)
    policy = propose_trust_policy(
        session,
        product,
        TrustPolicyProposal(**documentary(now), require_conversion=True, require_commission=True),
        now=now,
    )
    result = evidence_readiness(session, product, policy["id"], now=now)
    assert EvidenceKind.AUTHENTICATED_GENERATION in result["missing_link_evidence"]
    assert EvidenceKind.CONVERSION_RECORDED in result["missing_link_evidence"]
    assert EvidenceKind.COMMISSION_VALIDATED in result["missing_link_evidence"]


def actual_quote(session, product, now):
    history = ProductPriceHistory(
        product_id=product.id,
        current_price=Decimal("99.90"),
        currency="BRL",
        source="shopee:official_api",
        source_type=SourceType.OFFICIAL_API,
        availability=Availability.UNKNOWN,
        price_access=PriceAccess.UNKNOWN,
        is_simulated=False,
        observed_at=now,
        evidence_metadata={},
    )
    session.add(history)
    session.flush()
    return history


@pytest.mark.parametrize("sufficient", [True, False])
def test_commercial_attestation_never_relabels_automatic_quote(session, product, sufficient):
    now = datetime.now(UTC)
    history = actual_quote(session, product, now)
    product.platform = Platform.SHOPEE
    policy = propose_trust_policy(
        session, product, TrustPolicyProposal(**documentary(now)), now=now
    )
    original = (history.source, history.source_type, history.availability, history.price_access)
    record_commercial_evidence(
        session,
        product,
        CommercialEvidenceInput(
            **documentary(now),
            history_id=history.id,
            applicable_price="99.90",
            availability="AVAILABLE" if sufficient else "UNKNOWN",
            price_access="PUBLIC",
            conditions_confirmed=True,
            conditions="Atestado independente de condições aplicáveis.",
        ),
        now=now,
    )
    result = evidence_readiness(session, product, policy["id"], now=now)
    assert result["commercial_evidence_sufficient"] is sufficient
    assert result["usable"] is False
    session.refresh(history)
    assert (
        history.source,
        history.source_type,
        history.availability,
        history.price_access,
    ) == original
    assert (
        PriceComparisonService().compare(session, history, now=now).direction
        == PriceDirection.UNAVAILABLE
    )
    assert (
        evidence_readiness(session, product, policy["id"], now=now + timedelta(minutes=11))[
            "commercial_evidence_sufficient"
        ]
        is False
    )


@pytest.mark.parametrize(
    "change",
    [
        {"applicable_price": "100.00"},
        {"variant_id": "999"},
        {"history_id": 999},
        {"is_simulated": True},
    ],
)
def test_commercial_evidence_rejects_incompatible_or_simulated_data(session, product, change):
    now = datetime.now(UTC)
    history = actual_quote(session, product, now)
    values = dict(documentary(now), history_id=history.id, applicable_price="99.90")
    if "is_simulated" in change:
        history.is_simulated = True
    else:
        values.update(change)
    with pytest.raises(ValueError):
        record_commercial_evidence(session, product, CommercialEvidenceInput(**values), now=now)


@pytest.mark.parametrize(
    "change",
    [
        {"actor": ""},
        {"reference": "https://example.test/?token=SECRET"},
        {"verified_at": "2026-10-25T12:00:00"},
        {"account_verified": True},
    ],
)
def test_evidence_api_validates_input_and_requires_admin(client, change):
    p = api.create(client, platform="SHOPEE", original_url=BASE, external_id="7001:1")
    url = f"/api/v1/products/{p['id']}/shopee/trust-proposals"
    values = (
        dict(
            actor="admin", reference="document:SYNTHETIC", verified_at=datetime.now(UTC).isoformat()
        )
        | change
    )
    assert client.post(url, json=values).status_code == 401
    assert client.post(url, headers=api.HEADERS, json=values).status_code == 422


def test_evidence_api_round_trip_preserves_link_and_proposal_cannot_approve(client):
    p = api.create(client, platform="SHOPEE", original_url=BASE, external_id="7001:1")
    root = f"/api/v1/products/{p['id']}"
    assert (
        client.put(
            root + "/affiliate-link", headers=api.HEADERS, json={"url": LONG + TRACKING}
        ).status_code
        == 200
    )
    doc = dict(
        actor="admin", reference="document:SYNTHETIC", verified_at=datetime.now(UTC).isoformat()
    )
    prop = client.post(root + "/shopee/trust-proposals", headers=api.HEADERS, json=doc)
    assert prop.status_code == 201
    assert prop.json()["status"] == "PROPOSED"
    response = client.post(
        root + "/shopee/link-evidence",
        headers=api.HEADERS,
        json=doc
        | {
            "kind": "PRODUCT_IDENTITY",
            "link_sha256": hashlib.sha256((LONG + TRACKING).encode()).hexdigest(),
        },
    )
    assert response.status_code == 201
    assert "signature" not in response.text
    status = client.get(
        root + "/shopee/evidence-readiness",
        headers=api.HEADERS,
        params={"policy_id": prop.json()["id"]},
    )
    assert status.status_code == 200
    assert status.json()["usable"] is False
    assert (
        client.get(root + "/affiliate-link", headers=api.HEADERS).json()["source"]
        == "MANUAL_SHOPEE"
    )


@pytest.mark.parametrize(
    "source_type", [SourceType.MANUAL, SourceType.UNKNOWN, SourceType.OFFICIAL_API]
)
def test_public_comparison_never_mixes_source_types(session, product, source_type):
    old = prices.observe(session, product, "100.00", at=prices.NOW - timedelta(minutes=1))
    current = prices.observe(session, product, "90.00")
    old.source_type = source_type
    current.source_type = SourceType.OFFICIAL_API
    result = PriceComparisonService().compare(session, current, now=prices.NOW)
    assert result.direction == (
        PriceDirection.DROP if source_type == SourceType.OFFICIAL_API else PriceDirection.INITIAL
    )


@pytest.mark.parametrize("change", ["source", "price_range", "price_basis", "unknown_public"])
def test_quoted_and_verified_prices_have_distinct_comparability(session, product, change):
    old = prices.observe(session, product, "100.00", at=prices.NOW - timedelta(minutes=1))
    current = prices.observe(session, product, "90.00")
    if change == "source":
        old.source = "another_provider"
    elif change == "unknown_public":
        old.price_access = PriceAccess.UNKNOWN
    else:
        old.evidence_metadata = {change: "quoted_range"}
    assert (
        PriceComparisonService().compare(session, current, now=prices.NOW).direction
        == PriceDirection.INITIAL
    )


def test_carried_reference_cannot_cross_source_type_or_price_basis(session, product):
    baseline = prices.observe(session, product, "100.00", at=prices.NOW - timedelta(minutes=3))
    prior = prices.observe(session, product, "90.00", at=prices.NOW - timedelta(minutes=2))
    current = prices.observe(session, product, "90.00")
    session.add(
        Offer(
            product_id=product.id,
            price_history_id=prior.id,
            current_price=Decimal("90.00"),
            historical_price=Decimal("100.00"),
            status=OfferStatus.PENDING,
            discount_amount=Decimal("10.00"),
            discount_percentage=Decimal("10.00"),
            score=Decimal("10.00"),
            expires_at=prices.NOW + timedelta(hours=1),
        )
    )
    session.flush()
    assert _carried_historical_price(session, current) == Decimal("100.00")
    baseline.source_type = SourceType.MANUAL
    assert _carried_historical_price(session, current) is None
    baseline.source_type = current.source_type
    prior.source_type = SourceType.MANUAL
    assert _carried_historical_price(session, current) is None
    prior.source_type = current.source_type
    baseline.evidence_metadata = {"price_basis": "quote"}
    assert _carried_historical_price(session, current) is None


@pytest.mark.parametrize(
    "kind,account,credential",
    [
        (EvidenceKind.CREDENTIAL_PROVENANCE, None, "credential:SYNTHETIC"),
        (EvidenceKind.CREDENTIAL_PROVENANCE, "account:SYNTHETIC", None),
        (EvidenceKind.OWNERSHIP_ATTESTATION, None, None),
    ],
)
def test_provenance_requires_nonsecret_account_and_credential_references(
    session, product, kind, account, credential
):
    product = evidence_product(session, product)
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        record_manual_link_evidence(
            session,
            product,
            LinkEvidenceInput(
                **documentary(now),
                kind=kind,
                link_sha256=link_fingerprint(product.affiliate_link.url),
                account_reference=account,
                credential_reference=credential,
            ),
            now=now,
        )


def test_ownership_cannot_borrow_another_accounts_provenance(session, product):
    product = evidence_product(session, product)
    now = datetime.now(UTC)
    policy = propose_trust_policy(
        session,
        product,
        TrustPolicyProposal(**documentary(now), require_authenticated_generation=False),
        now=now,
    )
    for kind, account in (
        (EvidenceKind.PRODUCT_IDENTITY, None),
        (EvidenceKind.CREDENTIAL_PROVENANCE, "account:SYNTHETIC_A"),
        (EvidenceKind.OWNERSHIP_ATTESTATION, "account:SYNTHETIC_B"),
    ):
        record_manual_link_evidence(
            session,
            product,
            LinkEvidenceInput(
                **documentary(now),
                kind=kind,
                link_sha256=link_fingerprint(product.affiliate_link.url),
                account_reference=account,
                credential_reference="credential:SYNTHETIC",
            ),
            now=now,
        )
    result = evidence_readiness(session, product, policy["id"], now=now)
    assert result["link_criteria_met"] is False
    assert EvidenceKind.OWNERSHIP_ATTESTATION in result["missing_link_evidence"]


def test_evidence_dates_may_predate_registration_but_cannot_be_future(session, product):
    product = evidence_product(session, product)
    now = datetime.now(UTC)
    data = dict(
        kind=EvidenceKind.PRODUCT_IDENTITY, link_sha256=link_fingerprint(product.affiliate_link.url)
    )
    record_manual_link_evidence(
        session, product, LinkEvidenceInput(**documentary(now - timedelta(days=1)), **data), now=now
    )
    with pytest.raises(ValueError):
        record_manual_link_evidence(
            session,
            product,
            LinkEvidenceInput(**documentary(now + timedelta(seconds=1)), **data),
            now=now,
        )


def test_informational_quotes_cannot_mix_source_types(session, product):
    old = prices.observe(
        session,
        product,
        "100.00",
        at=prices.NOW - timedelta(minutes=1),
        availability=Availability.UNKNOWN,
        price_access=PriceAccess.UNKNOWN,
    )
    current = prices.observe(
        session,
        product,
        "90.00",
        availability=Availability.UNKNOWN,
        price_access=PriceAccess.UNKNOWN,
    )
    old.source_type = SourceType.MANUAL
    current.source_type = SourceType.OFFICIAL_API
    assert (
        PriceComparisonService().reported_quotes(session, current).direction
        == PriceDirection.INITIAL
    )
