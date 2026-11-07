"""Offline replays and synthetic variations; no live Shopee/Telegram access."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import pytest
import test_api as api
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.http import JsonTransport
from app.integrations.shopee import ShopeeConnector
from app.jobs.runtime import MonitoringRuntime
from app.models import (
    Base,
    Category,
    Offer,
    Product,
    ProductPriceHistory,
    PublicationQueueItem,
    ShopeeCollectionCandidate,
    ShopeeCollectionRun,
)
from app.services.categories import CategoryService
from app.services.shopee_collection import CollectionRequest, ShopeeCollectionService

application = api.application
client = api.client
NODE = json.loads(
    (Path(__file__).parent / "fixtures" / "shopee_product_offer_observed.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live APIs are forbidden in collection tests.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def setup(engine, url="sqlite://"):
    clock = [datetime(2026, 10, 21, 13, tzinfo=UTC)]
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=url,
        scheduler_enabled=False,
        shopee_app_id=SecretStr("SYNTHETIC_COLLECTION_APP"),
        shopee_secret=SecretStr("synthetic-collection-secret"),
    )
    factory = build_session_factory(engine)
    with factory() as session:
        CategoryService().seed(session)
        session.commit()
    state = {
        "clock": clock,
        "node": copy.deepcopy(NODE),
        "calls": 0,
        "failure": None,
        "factory": factory,
        "settings": settings,
        "engine": engine,
    }

    def handler(request):
        state["calls"] += 1
        query = json.loads(request.content)["query"]
        assert "mutation" not in query and "productOfferV2" in query
        if state.get("handler"):
            state["handler"]()
        failure = state["failure"]
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if isinstance(failure, int):
            return httpx.Response(failure, json={"errors": [{"message": "synthetic"}]})
        if failure == "graphql":
            return httpx.Response(200, json={"errors": [{"message": "synthetic denied"}]})
        nodes = state.get("nodes", [state["node"]])
        return httpx.Response(
            200,
            json={
                "data": {
                    "productOfferV2": {
                        "nodes": nodes,
                        "pageInfo": {"page": 1, "limit": 1, "hasNextPage": True},
                    }
                }
            },
        )

    state["http"] = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    state["transport"] = JsonTransport(client=state["http"], clock=lambda: clock[0], min_interval=0)
    state["connector"] = ShopeeConnector(
        state["transport"],
        enabled=True,
        app_id=settings.shopee_app_id,
        secret=settings.shopee_secret,
        clock=lambda: clock[0],
    )
    state["service"] = ShopeeCollectionService(factory, settings, lambda: clock[0])
    return state


@pytest.fixture
def context(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'collection.simulation.db').as_posix()}")
    Base.metadata.create_all(engine)
    ctx = setup(engine)
    yield ctx
    ctx["transport"].close()
    engine.dispose()


def specification(**changes):
    return {
        "request_id": str(uuid4()),
        "query": "PlayStation 5",
        "actor": "offline reviewer",
        "filters": {"categories": ["games"], "terms": ["hdmi"]},
        **changes,
    }


def execute(ctx, data=None):
    return ctx["service"].execute(data or specification(), connector=ctx["connector"], offline=True)


def count(ctx, model):
    with ctx["factory"]() as session:
        return session.scalar(select(func.count()).select_from(model))


def set_price(ctx, price):
    ctx["clock"][0] += timedelta(seconds=60)
    for field in ("price", "priceMin", "priceMax"):
        ctx["node"][field] = price


def test_observed_ingestion_initial_reference_provenance_and_no_outbox(context):
    result = execute(context)
    assert result["status"] == "SUCCEEDED" and result["mode"] == "OFFLINE"
    assert result["result"]["products"] == 1 and result["result"]["http_calls"] == 1
    assert result["result"]["has_next_not_followed"] is True
    row = result["result"]["candidates"][0]
    assert row["status"] == "INITIAL"
    assert row["reported_change"]["direction"] == "INITIAL"
    assert row["reported_change"]["percentage"] is None
    assert row["verified_change"]["direction"] == "UNAVAILABLE"
    with context["factory"]() as session:
        product = session.scalar(select(Product))
        history = session.scalar(select(ProductPriceHistory))
        assert product.external_id == "1018042820:22293101052"
        assert product.name == NODE["productName"] and product.original_url == NODE["productLink"]
        assert product.affiliate_link is None
        assert history.current_price == Decimal("59.00") and history.previous_price is None
        assert not history.is_simulated and history.source == "shopee:official_api"
        assert history.observed_at == context["clock"][0]
        assert history.evidence_metadata["category_external_ids"] == ["100535", "100583"]
        assert history.evidence_metadata["price_min"] == "59.00"
        assert history.evidence_metadata["price_max"] == "59.00"
        assert history.evidence_metadata["commission_rate"] == "0.38"
        assert history.evidence_metadata["image_url"] == NODE["imageUrl"]
    assert count(context, Category) == 9
    assert count(context, Offer) == count(context, PublicationQueueItem) == 0
    assert not context["settings"].shopee_enabled


@pytest.mark.parametrize(
    "price,direction,percentage",
    [("55", "DROP", "-6.78"), ("60", "RISE", "1.69"), ("59", "UNCHANGED", "0.00")],
)
def test_informational_changes_never_become_verified_discounts(
    context, price, direction, percentage
):
    execute(context)
    set_price(context, price)
    row = execute(context)["result"]["candidates"][0]
    assert row["reported_change"]["direction"] == direction
    assert row["reported_change"]["percentage"] == percentage
    assert row["status"] == "UNVERIFIED" and not row["publication_authorized"]
    assert row["verified_change"]["percentage"] is None
    assert count(context, ProductPriceHistory) == 2 and count(context, Offer) == 0


def test_duplicate_run_and_cached_observation_do_not_duplicate_history(context):
    data = specification()
    first = execute(context, data)
    assert execute(context, data) == first
    assert context["calls"] == 1
    execute(context)
    assert (
        context["calls"] == 2
        and count(context, Product) == count(context, ProductPriceHistory) == 1
    )
    with pytest.raises(ValueError, match="idempotência"):
        execute(context, {**data, "query": "another query"})


@pytest.mark.parametrize(
    "filters,reason",
    [
        ({"categories": ["moda"], "terms": ["hdmi"]}, "CATEGORY_FILTER"),
        (
            {"categories": ["games"], "terms": ["PlayStation 5"]},
            "PRODUCT_FILTER_OR_AMBIGUOUS_INTENT",
        ),
        ({"categories": ["games"], "terms": ["hdmi"], "max_price": "58.99"}, "MAX_PRICE_FILTER"),
        (
            {"categories": ["games"], "terms": ["hdmi"], "min_discount": "1"},
            "MIN_VERIFIED_DISCOUNT_FILTER",
        ),
    ],
)
def test_filters_are_joint_and_commission_is_not_discount(context, filters, reason):
    row = execute(context, specification(filters=filters))["result"]["candidates"][0]
    assert row["status"] == "FILTERED" and reason in row["reasons"]
    assert count(context, PublicationQueueItem) == 0


@pytest.mark.parametrize(
    "name,accepted",
    [
        ("Console PlayStation 5 Slim", True),
        ("Cabo HDMI para Console PlayStation 5", False),
        ("Capa PlayStation 5 Console", False),
        ("PlayStation 5 edição especial", False),
    ],
)
def test_console_intent_requires_positive_evidence_and_excludes_accessories(
    context, name, accepted
):
    context["node"]["productName"] = name
    result = execute(
        context, specification(filters={"categories": ["games"], "terms": ["PlayStation 5"]})
    )
    assert (result["result"]["candidates"][0]["status"] != "FILTERED") == accepted


@pytest.mark.parametrize(
    "failure,status,code",
    [
        ("timeout", "UNCERTAIN", "TIMEOUT"),
        (403, "FAILED", "AUTHORIZATION_DENIED"),
        (429, "FAILED", "RATE_LIMITED"),
        (500, "FAILED", "UPSTREAM_ERROR"),
        ("graphql", "FAILED", "UPSTREAM_GRAPHQL_ERROR"),
    ],
)
def test_failures_consume_one_attempt_without_retries_or_partial_history(
    context, failure, status, code
):
    context["failure"] = failure
    data = specification()
    result = execute(context, data)
    assert result["status"] == status and result["error_code"] == code
    assert execute(context, data) == result and context["calls"] == 1
    assert count(context, ProductPriceHistory) == count(context, ShopeeCollectionCandidate) == 0


def test_unresolved_range_preserves_evidence_without_selecting_a_variant_price(context):
    context["node"].update(priceMin="50", priceMax="80")
    result = execute(context)
    with context["factory"]() as session:
        history = session.scalar(select(ProductPriceHistory))
        assert history.current_price is None
        assert history.evidence_metadata["price_min"] == "50.00"
        assert history.evidence_metadata["price_max"] == "80.00"
    assert result["result"]["candidates"][0]["reported_change"]["direction"] == "UNAVAILABLE"


def test_bad_price_preserves_last_history_and_revalidation_expires_older_candidate(context):
    first = execute(context)["result"]["candidates"][0]
    set_price(context, "0")
    assert execute(context)["status"] == "FAILED"
    assert count(context, ProductPriceHistory) == 1
    set_price(context, "58")
    execute(context)
    assert context["service"].revalidate(first["id"])["status"] == "EXPIRED"


def test_manual_categories_and_product_filter_remain_authoritative(context):
    first = execute(context)["result"]["candidates"][0]
    with context["factory"]() as session:
        product = session.get(Product, first["product_id"])
        category_id = session.scalar(select(Category.id).where(Category.slug == "tecnologia"))
        CategoryService().set_manual(session, product, [category_id])
        session.commit()
    data = specification(
        filters={"categories": ["tecnologia"], "product_ids": [first["product_id"]]}
    )
    row = execute(context, data)["result"]["candidates"][0]
    assert row["categories"] == ["tecnologia"] and not row["reasons"]


def test_concurrent_same_run_has_one_http_attempt(context):
    data = specification()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(context, data), range(4)))
    assert context["calls"] == 1 and count(context, ShopeeCollectionRun) == 1
    assert all(r["status"] in ("RESERVED", "SUCCEEDED") for r in results)


def test_concurrent_different_runs_are_fenced(context):
    entered, release = Event(), Event()

    def hold():
        entered.set()
        assert release.wait(10)

    context["handler"] = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, context)
        assert entered.wait(10)
        second = execute(context)
        assert second["error_code"] == "MONITORING_BUSY"
        release.set()
        assert first.result()["status"] == "SUCCEEDED"
    assert context["calls"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 0},
        {"limit": 6},
        {"limit": True},
        {"confirmed_real_read": "true"},
        {"query": "   "},
        {"query": "hello\nworld"},
        {"page": 2},
        {"filters": {"categories": ["console"], "terms": ["ps5"]}},
        {"filters": {"categories": ["games"], "automatic_approval": True, "terms": ["ps5"]}},
        {"filters": {"categories": ["games"]}},
    ],
)
def test_invalid_scopes_fail_before_io(changes):
    with pytest.raises(ValidationError):
        CollectionRequest.model_validate(specification(**changes))


def test_real_mode_requires_new_per_run_opt_in_and_replay_requires_isolation(context):
    with pytest.raises(ValueError, match="opt-in"):
        context["service"].execute(specification())
    with pytest.raises(ValueError, match="MockTransport"):
        context["service"].execute(specification(), offline=True)
    context["service"].settings = context["settings"].model_copy(update={"shopee_enabled": True})
    with pytest.raises(ValueError, match="flags"):
        execute(context)
    assert context["calls"] == 0


def test_scheduler_job_is_prepared_paused_and_cannot_consume_commercial_consent(context):
    runtime = MonitoringRuntime(context["factory"], context["settings"])
    result = context["service"].prepare_schedule(runtime)
    job = runtime._prepared_jobs["shopee-collection-prepared"]
    assert result["status"] == "PAUSED" and job["next_run_time"] is None
    assert job["func"]()["status"] == "BLOCKED" and runtime._scheduler is None
    assert context["calls"] == 0


def test_real_registry_rate_limit_and_authorization_block_are_reused(context, monkeypatch):
    import app.integrations.registry as module

    monkeypatch.setattr(
        module, "JsonTransport", lambda **kw: JsonTransport(client=context["http"], **kw)
    )
    # Keep the injected mock client available across independent registry instances.
    monkeypatch.setattr(context["http"], "close", lambda: None)
    data = specification(confirmed_real_read=True, authorization="synthetic approved single read")
    assert context["service"].execute(data)["status"] == "SUCCEEDED"
    limited = context["service"].execute({**data, "request_id": str(uuid4())})
    assert limited["error_code"] == "RATE_LIMITED" and context["calls"] == 1
    context["clock"][0] += timedelta(seconds=901)
    context["failure"] = 403
    assert (
        context["service"].execute({**data, "request_id": str(uuid4())})["error_code"]
        == "AUTHORIZATION_DENIED"
    )
    blocked = context["service"].execute({**data, "request_id": str(uuid4())})
    assert blocked["error_code"] == "AUTHORIZATION_DENIED" and context["calls"] == 2


def test_admin_routes_preserve_auth_and_require_separate_real_consent(client, application):
    prefix = "/api/v1/shopee-collection"
    assert client.get(prefix + "/runs").status_code == 401
    assert client.get(prefix + "/runs", headers=api.HEADERS).json() == []
    assert client.get(prefix + "/candidates", headers=api.HEADERS).json() == []
    assert (
        client.post(prefix + "/runs", json=specification(), headers=api.HEADERS).status_code == 409
    )
    prepared = client.post(prefix + "/schedule/prepare", headers=api.HEADERS)
    assert prepared.status_code == 200 and prepared.json()["status"] == "PAUSED"


def test_response_extras_never_establish_stock_public_access_or_store_discount(context):
    context["node"].update(
        stock=100,
        availability="AVAILABLE",
        priceAccess="PUBLIC",
        originalPrice="999",
        coupon="SYNTHETIC",
        priceDiscountRate=99,
    )
    row = execute(context)["result"]["candidates"][0]
    assert row["status"] == "INITIAL" and row["verified_change"]["direction"] == "UNAVAILABLE"
    with context["factory"]() as session:
        history = session.scalar(select(ProductPriceHistory))
        assert history.previous_price is None
        assert history.availability.value == history.price_access.value == "UNKNOWN"
    assert count(context, Offer) == 0


def test_simulated_history_is_not_a_baseline_for_reported_real_quotes(context):
    execute(context)
    with context["factory"]() as session:
        history = session.scalar(select(ProductPriceHistory))
        history.is_simulated = True
        session.commit()
    set_price(context, "50")
    row = execute(context)["result"]["candidates"][0]
    assert row["reported_change"]["direction"] == "INITIAL"
    assert row["reported_change"]["percentage"] is None


def test_expired_lease_discards_the_received_page_without_replaying(context):
    context["handler"] = lambda: context["clock"].__setitem__(
        0, context["clock"][0] + timedelta(seconds=121)
    )
    data = specification()
    result = execute(context, data)
    assert result["status"] == "UNCERTAIN" and result["error_code"] == "LEASE_LOST"
    assert count(context, Product) == 0
    assert execute(context, data) == result and context["calls"] == 1


def test_over_limit_page_is_not_partially_ingested(context):
    second = {
        **context["node"],
        "itemId": 1234,
        "productLink": "https://shopee.com.br/product/1018042820/1234",
    }
    context["nodes"] = [context["node"], second]
    result = execute(context)
    assert result["status"] == "FAILED" and count(context, Product) == 0
    assert context["calls"] == 1


def test_manual_pause_is_preserved_and_does_not_gain_an_observation(context):
    execute(context)
    with context["factory"]() as session:
        product = session.scalar(select(Product))
        product.monitoring_enabled = False
        session.commit()
    set_price(context, "55")
    result = execute(context)
    assert result["result"]["products"] == 0 and count(context, ProductPriceHistory) == 1


def test_revalidation_keeps_original_assessment_and_appends_audit(context):
    from app.models import IntegrationEvent

    first = execute(context)
    row = first["result"]["candidates"][0]
    with context["factory"]() as session:
        games = session.scalar(select(Category).where(Category.slug == "games"))
        games.active = False
        session.commit()
    assert context["service"].revalidate(row["id"])["status"] == "FILTERED"
    assert context["service"].read(first["id"])["result"] == first["result"]
    with context["factory"]() as session:
        audit = session.scalar(
            select(IntegrationEvent).where(IntegrationEvent.operation == "collection_revalidate")
        )
        assert audit.status == "FILTERED" and str(row["id"]) in audit.source_reference


def test_prepared_job_uses_the_existing_apscheduler_and_remains_paused(context):
    from apscheduler.schedulers.background import BackgroundScheduler

    runtime = MonitoringRuntime(context["factory"], context["settings"])
    runtime._scheduler = BackgroundScheduler(timezone="UTC")
    original = runtime._scheduler
    context["service"].prepare_schedule(runtime)
    job = original.get_job("shopee-collection-prepared")
    assert runtime._scheduler is original and job.next_run_time is None
    assert job.func()["status"] == "BLOCKED" and not original.running
    assert context["calls"] == 0


def test_stale_candidate_revalidation_is_local_and_expires(context):
    row = execute(context)["result"]["candidates"][0]
    context["clock"][0] += timedelta(seconds=601)
    assert context["service"].revalidate(row["id"])["status"] == "EXPIRED"
    assert context["calls"] == 1


def test_hypothetical_verified_drop_creates_only_pending_offer_and_no_queue(context, monkeypatch):
    from dataclasses import replace

    from app.models import Availability, OfferStatus, PriceAccess

    original = context["connector"]._snapshot

    def verified(*args, **kwargs):
        # Synthetic future proof: today's API never establishes these conditions.
        return replace(
            original(*args, **kwargs),
            availability=Availability.AVAILABLE,
            price_access=PriceAccess.PUBLIC,
        )

    monkeypatch.setattr(context["connector"], "_snapshot", verified)
    execute(context)
    assert count(context, Offer) == 0
    set_price(context, "55")
    row = execute(context)["result"]["candidates"][0]
    assert row["status"] == "MANUAL_REVIEW" and row["requires_manual_approval"]
    with context["factory"]() as session:
        offer = session.scalar(select(Offer))
        assert offer.status == OfferStatus.PENDING
        assert offer.historical_price == Decimal("59.00")
    assert count(context, PublicationQueueItem) == 0
