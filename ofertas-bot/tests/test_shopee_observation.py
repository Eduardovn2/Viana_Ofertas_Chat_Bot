"""Observational quotes in isolated databases; every upstream response is synthetic."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

import httpx
import pytest
import test_api as api
import test_shopee as connector_contract
import test_shopee_collection as collection
from pydantic import ValidationError
from sqlalchemy import select

from app.integrations.contracts import PriceContext
from app.models import (
    AffiliateLink,
    AppSetting,
    Availability,
    IntegrationEvent,
    Offer,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationQueueItem,
    ShopeeCollectionRun,
    SourceType,
    TelegramPublication,
)
from app.services.comparison import PriceComparisonService
from app.services.discovery_operations import DiscoveryOperations
from app.services.monitoring import _safe_evidence, record_price
from app.services.offers import OfferEngine
from app.services.shopee_collection import ObservationRequest

base_context = collection.context
no_network = collection.no_network
application = api.application
client = api.client


def prepare(ctx):
    ctx["node"] = copy.deepcopy(connector_contract.SYNTHETIC_NODE)
    ctx["queries"] = []
    ctx["http"].event_hooks["request"].append(
        lambda request: ctx["queries"].append(json.loads(request.content)["query"])
    )
    with ctx["factory"]() as session:
        product = Product(
            platform=Platform.SHOPEE,
            external_id="101:202",
            variant_id="",
            original_url=connector_contract.URL,
            name="SYNTHETIC known item",
            monitoring_enabled=False,
        )
        session.add(product)
        session.commit()
        ctx["product_id"] = product.id
    return ctx


@pytest.fixture
def context(base_context):
    return prepare(base_context)


def specification(ctx, **changes):
    return {
        "request_id": str(uuid4()),
        "actor": "offline reviewer",
        "product_id": ctx["product_id"],
        "shop_id": "101",
        "item_id": "202",
        **changes,
    }


def observe(ctx, data=None):
    return ctx["service"].observe(
        data or specification(ctx), connector=ctx["connector"], offline=True
    )


def histories(ctx):
    with ctx["factory"]() as session:
        return list(session.scalars(select(ProductPriceHistory).order_by(ProductPriceHistory.id)))


def assert_no_commerce(ctx):
    for model in (Offer, AffiliateLink, PublicationQueueItem, TelegramPublication):
        assert collection.count(ctx, model) == 0
    assert collection.count(ctx, Product) == 1
    with ctx["factory"]() as session:
        product = session.get(Product, ctx["product_id"])
        assert not product.monitoring_enabled
        assert product.name == "SYNTHETIC known item"
    assert not ctx["settings"].shopee_enabled
    assert not ctx["settings"].telegram_publisher_enabled
    assert not ctx["settings"].telegram_real_sends_authorized


def test_identity_query_initial_decimal_context_and_audit(context):
    data = specification(context)
    result = observe(context, data)
    assert result["status"] == "SUCCEEDED", result
    assert context["calls"] == 1
    query = context["queries"][0]
    assert "productOfferV2(shopId:101,itemId:202,page:1,limit:1)" in query
    assert "keyword" not in query
    for field in (
        "price",
        "priceMin",
        "priceMax",
        "priceDiscountRate",
        "periodStartTime",
        "periodEndTime",
    ):
        assert field in query
    candidate = result["result"]["candidates"][0]
    assert candidate["reported_change"]["direction"] == "INITIAL"
    assert candidate["verified_change"]["direction"] == "EXCLUDED"
    assert candidate["status"] == "INITIAL" and not candidate["commercial_verified"]
    history = histories(context)[0]
    assert history.current_price == Decimal("99.90") and history.previous_price is None
    assert (
        history.availability == Availability.UNKNOWN and history.price_access == PriceAccess.UNKNOWN
    )
    assert history.source_type == SourceType.OFFICIAL_API
    assert history.source_reference == "productOfferV2:101:202"
    assert history.evidence_metadata["observation_only"] is True
    basis = PriceContext.model_validate(history.evidence_metadata["price_context"])
    assert history.evidence_metadata["price_basis"] == basis.price_basis
    assert basis.variant_identity == basis.payment == basis.coupon == basis.region == "UNKNOWN"
    with context["factory"]() as session:
        events = list(session.scalars(select(IntegrationEvent).order_by(IntegrationEvent.id)))
        assert [e.operation for e in events] == ["observation_reserved", "observation_result"]
        assert [e.status for e in events] == ["RESERVED", "SUCCEEDED"]
        assert all(str(data["request_id"]) in e.source_reference for e in events)
    assert_no_commerce(context)


@pytest.mark.parametrize("response", ["empty", "other", "inconsistent_url", "graphql", "too_many"])
def test_identity_errors_never_substitute_known_product(context, response):
    if response == "empty":
        context["nodes"] = []
    elif response == "other":
        context["node"].update(itemId=999, productLink="https://shopee.com.br/product/101/999")
    elif response == "inconsistent_url":
        context["node"]["productLink"] = "https://shopee.com.br/product/101/999"
    elif response == "too_many":
        context["nodes"] = [context["node"], context["node"]]
    else:
        context["failure"] = "graphql"
    result = observe(context)
    assert result["status"] == "FAILED" and context["calls"] == 1
    assert histories(context) == []
    assert_no_commerce(context)


@pytest.mark.parametrize("field", ["shop_id", "item_id"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "01", "101 mutation {}", 2**63])
def test_invalid_identifiers_fail_before_reservation(context, field, value):
    with pytest.raises(ValidationError):
        observe(context, specification(context, **{field: value}))
    assert context["calls"] == collection.count(context, ShopeeCollectionRun) == 0


@pytest.mark.parametrize("change", [{"product_id": 9999}, {"shop_id": "999"}, {"item_id": "999"}])
def test_known_product_scope_checked_before_http(context, change):
    with pytest.raises(ValueError, match="Produto conhecido"):
        observe(context, specification(context, **change))
    assert context["calls"] == 0


def test_nonempty_variant_is_not_fabricated_or_queried(context):
    with context["factory"]() as session:
        session.get(Product, context["product_id"]).variant_id = "unverified-model"
        session.commit()
    with pytest.raises(ValueError, match="variante"):
        observe(context)
    assert context["calls"] == 0


@pytest.mark.parametrize("percent", [0, 100, None])
def test_announced_discount_is_not_historical_or_coupon(context, percent):
    context["node"]["priceDiscountRate"] = percent
    result = observe(context)
    history = histories(context)[0]
    assert history.evidence_metadata["advertised_discount_percent"] == percent
    assert history.evidence_metadata["commission_amount"] == "12.32766"
    assert history.previous_price is None
    assert result["result"]["candidates"][0]["reported_change"]["percentage"] is None
    assert_no_commerce(context)


@pytest.mark.parametrize("value", [-1, 101, True, "10", 1.5, {}, []])
def test_invalid_announced_discount_rejects_observation(context, value):
    context["node"]["priceDiscountRate"] = value
    assert observe(context)["status"] == "FAILED"
    assert histories(context) == []


@pytest.mark.parametrize("value", [None, 0, -1, 1792969200, "1792969200", 2**63 - 1, -(2**63)])
def test_periods_remain_uninterpreted_int64(context, value):
    context["node"].update(periodStartTime=value, periodEndTime=value)
    assert observe(context)["status"] == "SUCCEEDED"
    metadata = histories(context)[0].evidence_metadata
    assert metadata["offer_period_start_raw"] == (int(value) if value is not None else None)
    assert metadata["offer_period_end_raw"] == metadata["offer_period_start_raw"]
    assert metadata["period_time_unit"] == metadata["period_semantics"] == "UNCONFIRMED"
    assert not metadata["commercial_verified"]


@pytest.mark.parametrize("value", [True, 1.5, "1.5", "1e10", 2**63, -(2**63) - 1, {}, []])
def test_malformed_period_rejected(context, value):
    context["node"]["periodEndTime"] = value
    assert observe(context)["status"] == "FAILED"
    assert histories(context) == []


def test_range_and_untrusted_extra_conditions_remain_unknown(context):
    context["node"].update(
        priceMin="80",
        priceMax="110",
        stock=99,
        availability="AVAILABLE",
        priceAccess="PUBLIC",
        modelId=999,
        payment="PIX",
        coupon="SYNTHETIC",
        originalPrice="999",
    )
    observe(context)
    row = histories(context)[0]
    assert row.current_price is None and row.previous_price is None
    assert row.evidence_metadata["price_min"] == "80.00"
    assert row.evidence_metadata["price_max"] == "110.00"
    assert row.evidence_metadata["price_context"]["variant_identity"] == "UNKNOWN"
    assert row.evidence_metadata["price_context"]["payment"] == "UNKNOWN"
    assert row.availability == Availability.UNKNOWN and row.price_access == PriceAccess.UNKNOWN
    assert_no_commerce(context)


@pytest.mark.parametrize(
    "price,direction,percent",
    [("99.90", "UNCHANGED", "0.00"), ("100.00", "RISE", "0.10"), ("89.91", "DROP", "-10.00")],
)
def test_series_stable_basis_informational_only(context, price, direction, percent):
    observe(context)
    first = histories(context)[0]
    collection.set_price(context, price)
    context["node"].update(priceDiscountRate=100, commission="999", periodStartTime=999)
    candidate = observe(context)["result"]["candidates"][0]
    assert candidate["reported_change"]["direction"] == direction
    assert candidate["reported_change"]["percentage"] == percent
    assert (
        histories(context)[1].evidence_metadata["price_basis"]
        == first.evidence_metadata["price_basis"]
    )
    assert candidate["verified_change"]["percentage"] is None
    assert_no_commerce(context)


@pytest.mark.parametrize(
    "field",
    [
        "origin",
        "operation",
        "product_identity",
        "variant_identity",
        "price_kind",
        "payment",
        "coupon",
        "eligibility",
        "region",
    ],
)
def test_different_context_never_becomes_drop(context, field):
    observe(context)
    with context["factory"]() as session:
        previous = session.scalar(select(ProductPriceHistory))
        metadata = dict(previous.evidence_metadata)
        basis = PriceContext.model_validate(
            {**metadata["price_context"], field: "SYNTHETIC_DIFFERENT"}
        )
        metadata.update(price_context=basis.model_dump(), price_basis=basis.price_basis)
        previous.evidence_metadata = metadata
        session.commit()
    collection.set_price(context, "80")
    candidate = observe(context)["result"]["candidates"][0]
    assert candidate["reported_change"]["direction"] == "INITIAL"
    assert candidate["reported_change"]["percentage"] is None


def test_manual_history_never_supplies_official_baseline(context):
    observe(context)
    with context["factory"]() as session:
        session.scalar(select(ProductPriceHistory)).source_type = SourceType.MANUAL
        session.commit()
    collection.set_price(context, "80")
    assert observe(context)["result"]["candidates"][0]["reported_change"]["direction"] == "INITIAL"


def test_validated_basis_round_trip_and_tampering(context):
    observe(context)
    metadata = histories(context)[0].evidence_metadata
    assert _safe_evidence(metadata) == metadata
    for changes in (
        {"price_basis": "arbitrary"},
        {"price_basis": "v1:" + "0" * 64},
        {"advertised_discount_percent": True},
        {"period_time_unit": "SECONDS"},
    ):
        with pytest.raises(ValueError):
            _safe_evidence({**metadata, **changes})
    with pytest.raises(ValidationError):
        PriceContext.model_validate({**metadata["price_context"], "price": "99.90"})


def test_observation_cannot_be_promoted_even_if_enums_are_altered(context):
    observe(context)
    with context["factory"]() as session:
        row = session.scalar(select(ProductPriceHistory))
        row.availability, row.price_access = Availability.AVAILABLE, PriceAccess.PUBLIC
        session.get(Product, context["product_id"]).monitoring_enabled = True
        assert (
            PriceComparisonService().compare(session, row, now=context["clock"][0]).direction
            == "EXCLUDED"
        )
        assert OfferEngine(context["settings"]).process(session, row.product, row) is None
        original = context["connector"]._snapshot(context["node"], context["clock"][0])
        with pytest.raises(ValueError, match="observacional"):
            record_price(
                session,
                row.product,
                replace(
                    original,
                    availability=Availability.AVAILABLE,
                    price_access=PriceAccess.PUBLIC,
                    evidence_metadata={**original.evidence_metadata, "observation_only": True},
                ),
            )
    assert collection.count(context, Offer) == collection.count(context, PublicationQueueItem) == 0


def test_replay_is_idempotent_and_scope_is_immutable(context):
    data = specification(context)
    result = observe(context, data)
    assert observe(context, data) == result
    assert context["calls"] == 1 and len(histories(context)) == 1
    assert collection.count(context, IntegrationEvent) == 2
    # Same instant and content in a distinct run still reuses the observation.
    observe(context)
    assert context["calls"] == 2 and len(histories(context)) == 1
    with pytest.raises(ValueError, match="idempotência"):
        observe(context, {**data, "actor": "different actor"})


def test_timeout_is_consumed_uncertain_and_audited(context):
    context["failure"] = "timeout"
    data = specification(context)
    result = observe(context, data)
    assert result["status"] == "UNCERTAIN" and result["error_code"] == "TIMEOUT"
    assert observe(context, data) == result and context["calls"] == 1
    assert histories(context) == []
    with context["factory"]() as session:
        event = session.scalars(
            select(IntegrationEvent).order_by(IntegrationEvent.id.desc())
        ).first()
        assert event.status == "UNCERTAIN" and event.error_code == "TIMEOUT"


def test_persistence_failure_rolls_back_and_consumes_attempt(context, monkeypatch):
    original = context["service"]._persist_observation

    def fail(*args):
        original(*args)
        raise RuntimeError("synthetic failure after flush")

    monkeypatch.setattr(context["service"], "_persist_observation", fail)
    data = specification(context)
    assert observe(context, data)["status"] == "UNCERTAIN"
    assert observe(context, data)["status"] == "UNCERTAIN" and context["calls"] == 1
    assert histories(context) == []


def test_interruption_is_uncertain_without_replay(context):
    def interrupt():
        raise KeyboardInterrupt()

    context["handler"] = interrupt
    data = specification(context)
    assert observe(context, data)["status"] == "UNCERTAIN"
    assert observe(context, data)["status"] == "UNCERTAIN" and context["calls"] == 1


def test_concurrent_same_scope_consumes_one_attempt(context):
    data = specification(context)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: observe(context, data), range(4)))
    assert context["calls"] == 1 and len(histories(context)) == 1
    assert all(r["status"] in ("RESERVED", "SUCCEEDED") for r in results)
    assert collection.count(context, IntegrationEvent) == 2


def test_concurrent_different_scopes_are_fenced(context):
    entered, release = Event(), Event()

    def hold():
        entered.set()
        assert release.wait(10)

    context["handler"] = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(observe, context)
        assert entered.wait(10)
        try:
            assert observe(context)["error_code"] == "MONITORING_BUSY"
        finally:
            release.set()
        assert first.result()["status"] == "SUCCEEDED"
    assert context["calls"] == 1


def test_dynamic_operations_budget_does_not_grant_real_authority(context):
    with context["factory"]() as session:
        DiscoveryOperations().save(session, {"max_queries_per_period": 0}, 0)
        session.commit()
    result = observe(context)
    assert result["error_code"] == "RATE_LIMITED" and context["calls"] == 0
    with context["factory"]() as session:
        assert session.scalar(select(AppSetting)) is not None


def test_real_execution_and_nonmock_transport_are_unconditionally_blocked(context):
    data = specification(context)
    with pytest.raises(ValueError, match="real bloqueada"):
        context["service"].observe(data)
    with pytest.raises(ValueError, match="real bloqueada"):
        context["service"].execute(
            ObservationRequest.model_validate(data), connector=context["connector"]
        )
    with pytest.raises(ValueError, match="MockTransport"):
        context["service"].observe(data, offline=True)
    previous = context["transport"].client
    with httpx.Client(transport=httpx.HTTPTransport(), trust_env=False) as client:
        try:
            context["transport"].client = client
            with pytest.raises(ValueError, match="MockTransport"):
                context["service"].observe(data, connector=context["connector"], offline=True)
        finally:
            context["transport"].client = previous
    assert context["calls"] == collection.count(context, ShopeeCollectionRun) == 0


def test_query_cannot_escape_the_reserved_identity(context, monkeypatch):
    def unexpected(ref):
        return context["connector"]._request(
            context["connector"].product_query(ref).replace("shopId:101", "shopId:999")
        )

    monkeypatch.setattr(context["connector"], "fetch_product", unexpected)
    data = specification(context)
    assert observe(context, data)["status"] == "FAILED"
    assert observe(context, data)["status"] == "FAILED"
    assert context["calls"] == 0 and histories(context) == []


def test_expired_lease_makes_response_uncertain_without_persistence(context):
    context["handler"] = lambda: context["clock"].__setitem__(
        0, context["clock"][0] + timedelta(seconds=601)
    )
    data = specification(context)
    result = observe(context, data)
    assert result["status"] == "UNCERTAIN" and result["error_code"] == "LEASE_LOST"
    assert histories(context) == []
    assert observe(context, data) == result and context["calls"] == 1


def test_observation_revalidation_stays_local_and_expires(context):
    candidate = observe(context)["result"]["candidates"][0]
    assert context["service"].revalidate(candidate["id"])["status"] == "INITIAL"
    context["clock"][0] += timedelta(seconds=601)
    assert context["service"].revalidate(candidate["id"])["status"] == "EXPIRED"
    assert context["calls"] == 1


def test_observational_replay_does_not_adopt_a_legacy_nonobservational_history(context):
    snapshot = context["connector"]._snapshot(context["node"], context["clock"][0])
    with context["factory"]() as session:
        row = record_price(session, session.get(Product, context["product_id"]), snapshot)
        session.commit()
        previous_id = row.id
    candidate = observe(context)["result"]["candidates"][0]
    assert candidate["history_id"] != previous_id
    assert histories(context)[1].evidence_metadata["observation_only"] is True
    assert "observation_only" not in histories(context)[0].evidence_metadata


def test_admin_observational_route_validates_scope_but_never_executes(client):
    route = "/api/v1/shopee-collection/observations"
    data = {
        "request_id": str(uuid4()),
        "actor": "offline reviewer",
        "product_id": 2,
        "shop_id": "1904573791",
        "item_id": "23199869203",
    }
    assert client.post(route, json=data).status_code == 401
    assert client.post(route, json=data, headers=api.HEADERS).status_code == 409
    assert (
        client.post(route, json={**data, "shop_id": True}, headers=api.HEADERS).status_code == 422
    )
    assert (
        client.post(
            route, json={**data, "confirmed_real_read": True}, headers=api.HEADERS
        ).status_code
        == 422
    )
