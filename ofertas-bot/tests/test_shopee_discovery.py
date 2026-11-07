"""Completely offline discovery contracts. All sample products/prices are fictitious."""

import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import uuid4

import httpx
import pytest
import test_api as api
import test_shopee_collection as collection
from pydantic import ValidationError
from sqlalchemy import select

from app.db import build_engine
from app.integrations.registry import ConnectorRegistry
from app.jobs.runtime import MonitoringRuntime
from app.models import (
    AffiliateLink,
    Availability,
    Base,
    Category,
    CategoryMode,
    DestinationChannel,
    PriceAccess,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationQueueItem,
    QueueStatus,
    ShopeeCollectionCandidate,
    ShopeeDiscoveryFinding,
    ShopeeDiscoveryPolicy,
    ShopeeDiscoveryProduct,
    ShopeeDiscoveryRun,
)
from app.services.categories import CATALOG
from app.services.discovery_relevance import DiscoveryPolicyInput, SearchTerm, relevance
from app.services.leases import acquire_lease, release_lease
from app.services.shopee_discovery import JOB_IDS, ShopeeDiscoveryService

no_network = collection.no_network
application = api.application
client = api.client

# Names explicitly identify synthetic data; local rules supply the category.
SAMPLES = [
    ("organizador", "generic"),
    ("furadeira", "tool"),
    ("geladeira", "generic"),
    ("camiseta", "generic"),
    ("brinquedo", "generic"),
    ("smartphone iPhone 15", "phone"),
    ("Console PlayStation 5", "console"),
    ("shampoo", "generic"),
    ("objeto XYZ", "generic"),
]


def node(name="Console PlayStation 5", identity=1, price="100.00"):
    data = copy.deepcopy(collection.NODE)
    data.update(
        shopId=7001,
        itemId=identity,
        productName="[FICTÍCIO] " + name,
        productLink=f"https://shopee.com.br/product/7001/{identity}",
        price=price,
        priceMin=price,
        priceMax=price,
        productCatIds=[100535, 0],
    )
    return data


def setup(engine, url="sqlite://"):
    ctx = collection.setup(engine, url)
    ctx.update(pages={1: [node()]}, has_next=False, requests=[], failure_page=1)

    def handler(request):
        ctx["calls"] += 1
        query = json.loads(request.content)["query"]
        assert "mutation" not in query and "catId:" not in query
        page = int(re.search(r"page:\s*(\d+)", query).group(1))
        limit = int(re.search(r"limit:\s*(\d+)", query).group(1))
        ctx["requests"].append({"page": page, "limit": limit, "query": query})
        if ctx.get("handler"):
            ctx["handler"]()
        failure = ctx["failure"] if page == ctx["failure_page"] else None
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic")
        if isinstance(failure, int):
            return httpx.Response(failure, json={"error": "synthetic"})
        payload = {
            "data": {
                "productOfferV2": {
                    "nodes": ctx["pages"].get(page, []),
                    "pageInfo": {"page": page, "limit": limit, "hasNextPage": ctx["has_next"]},
                }
            }
        }
        if failure == "partial":
            payload["errors"] = [{"message": "synthetic untrusted message"}]
        return httpx.Response(200, json=payload)

    ctx["transport"].client.close()
    ctx["transport"].client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    registry = ConnectorRegistry(
        ctx["settings"], session_factory=ctx["factory"], clock=lambda: ctx["clock"][0]
    )
    registry._connectors[ctx["connector"].platform] = ctx["connector"]
    ctx["discovery"] = ShopeeDiscoveryService(
        ctx["factory"], ctx["settings"], registry=registry, clock=lambda: ctx["clock"][0]
    )
    return ctx


@pytest.fixture
def context(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'discovery.simulation.db').as_posix()}")
    Base.metadata.create_all(engine)
    ctx = setup(engine)
    yield ctx
    ctx["transport"].close()
    engine.dispose()


def configure(ctx, slug="games", **changes):
    index = [s[0] for s in CATALOG].index(slug)
    term, intent = SAMPLES[index]
    config = {"enabled": True, "searches": [{"query": term, "intent": intent}], **changes}
    with ctx["factory"]() as session:
        category_id = session.scalar(select(Category.id).where(Category.slug == slug))
    ctx["discovery"].configure(category_id, config, "synthetic administrator")
    return category_id


def execute(ctx, operation="DISCOVER", request_id=None):
    return ctx["discovery"].execute(request_id or uuid4(), operation)


def count(ctx, model):
    return collection.count(ctx, model)


def advance(ctx, seconds=901):
    ctx["clock"][0] += timedelta(seconds=seconds)


@pytest.mark.parametrize("slug,sample", list(zip([i[0] for i in CATALOG], SAMPLES, strict=True)))
def test_each_existing_category(context, slug, sample):
    configure(context, slug)
    context["pages"] = {1: [node(sample[0])]}
    result = execute(context)
    assert result["status"] == "SUCCEEDED", result
    assert result["result"]["pages"][0]["dispositions"]["SELECTED"] == 1
    assert count(context, Category) == 9
    assert count(context, ShopeeCollectionCandidate) == 1
    assert count(context, PublicationQueueItem) == 0
    with context["factory"]() as session:
        assert (
            session.scalar(select(ShopeeCollectionCandidate)).assessment["reported_change"][
                "direction"
            ]
            == "INITIAL"
        )


@pytest.mark.parametrize(
    "name,query,intent,expected",
    [
        ("Cabo HDMI PS5 Console PlayStation 5", "Console PlayStation 5", "console", "FILTERED"),
        ("Controle PlayStation 5", "PlayStation 5", "console", "FILTERED"),
        ("Suporte Console PlayStation 5", "PlayStation 5", "console", "FILTERED"),
        ("Capa smartphone iPhone 15", "iPhone 15", "phone", "FILTERED"),
        ("Película iPhone 15", "iPhone 15", "phone", "FILTERED"),
        ("Broca para furadeira", "furadeira", "tool", "FILTERED"),
        ("Furadeira profissional", "furadeira", "tool", "SELECTED"),
        ("Console PlayStation 5", "PlayStation 5", "console", "SELECTED"),
        ("PlayStation 5", "PlayStation 5", "console", "REVIEW"),
        ("Console PlayStation 4", "Console PlayStation 5", "console", "REVIEW"),
        ("smartphone iPhone 15", "iPhone 15", "phone", "SELECTED"),
        ("produto diferente", "iPhone 15", "phone", "FILTERED"),
        ("ORGANIZADOR decoração", "organizador", "generic", "SELECTED"),
    ],
)
def test_relevance(name, query, intent, expected):
    assert relevance(name, SearchTerm(query=query, intent=intent), [])[0] == expected


@pytest.mark.parametrize(
    "data",
    [
        {"destinations": [True]},
        {"destinations": ["1"]},
        {"destinations": [0]},
        {"destinations": [1, 1]},
        {"automatic_approval": True},
        {"enabled": "true"},
        {"max_pages": 3},
        {"page_size": 6},
        {"requests_per_hour": 11},
        {"cooldown_seconds": 1},
        {"exclusions": ["", " "]},
        {"exclusions": ["CAFÉ", "cafe"]},
        {"searches": [{"query": "***"}]},
        {"searches": [{"query": "Café"}, {"query": "cafe"}]},
    ],
)
def test_configuration_validation(data):
    with pytest.raises(ValidationError):
        DiscoveryPolicyInput.model_validate({"searches": [{"query": "console"}], **data})


def test_fair_rotation_and_terms(context):
    for slug, _ in CATALOG:
        configure(context, slug, category_priority=10 if slug == "games" else 1)
    ids = []
    for _ in CATALOG:
        result = execute(context)
        ids.append(result["category_id"])
    assert len(set(ids)) == 9
    assert context["calls"] == 9
    assert execute(context)["result"]["reason"] == "NO_DUE_POLICY_OR_BUDGET"


def test_multiple_terms_conservative_rotation(context):
    configure(
        context,
        searches=[{"query": "console", "priority": 1}, {"query": "PlayStation 5", "priority": 2}],
    )
    execute(context)
    advance(context)
    execute(context)
    assert (
        'keyword:"PlayStation 5"' in context["requests"][0]["query"].replace(" ", "")
        or "PlayStation 5" in context["requests"][0]["query"]
    )
    assert 'keyword:"console"' in context["requests"][1]["query"].replace(" ", "")


def test_pagination_dedup_and_initial(context):
    configure(context, max_pages=2, page_size=2)
    context["pages"] = {
        1: [node(identity=1), node("Cabo HDMI PlayStation 5", 2)],
        2: [node(identity=1), node(identity=3)],
    }
    context["has_next"] = True
    result = execute(context)
    assert result["status"] == "SUCCEEDED", result
    assert result["requests"] == 2 and context["calls"] == 2
    assert [r["page"] for r in context["requests"]] == [1, 2]
    assert count(context, Product) == 3 and count(context, ProductPriceHistory) == 2
    assert count(context, ShopeeDiscoveryFinding) == 4
    assert result["result"]["pages"][1]["dispositions"]["DUPLICATE"] == 1


def test_idempotency_no_duplicate_history(context):
    configure(context)
    key = uuid4()
    first = execute(context, request_id=key)
    assert execute(context, request_id=key) == first
    assert context["calls"] == 1
    with pytest.raises(ValueError, match="idempot"):
        execute(context, "MONITOR", key)


@pytest.mark.parametrize(
    "price,direction,percentage",
    [("90.00", "DROP", "-10.00"), ("110.00", "RISE", "10.00"), ("100.00", "UNCHANGED", "0.00")],
)
def test_history_comparable_quotes_not_verified_discounts(context, price, direction, percentage):
    configure(context)
    execute(context)
    advance(context)
    context["pages"][1][0] = node(price=price)
    result = execute(context, "MONITOR")
    assert result["status"] == "SUCCEEDED", result
    with context["factory"]() as session:
        entry = session.scalars(
            select(ShopeeCollectionCandidate).order_by(ShopeeCollectionCandidate.id.desc())
        ).first()
        assert entry.assessment["reported_change"]["direction"] == direction
        assert entry.assessment["reported_change"]["percentage"] == percentage
        assert entry.assessment["verified_change"]["direction"] == "UNAVAILABLE"
    assert count(context, ProductPriceHistory) == 2


def test_variant_range_and_review(context):
    configure(context)
    context["pages"][1][0]["priceMax"] = "120.00"
    result = execute(context)
    assert result["result"]["pages"][0]["dispositions"]["REVIEW"] == 1
    with context["factory"]() as session:
        assert session.scalar(select(ProductPriceHistory)).current_price is None
    advance(context)
    assert execute(context, "MONITOR")["requests"] == 0


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (429, "FAILED", "RATE_LIMITED"),
        (403, "FAILED", "AUTHORIZATION_DENIED"),
        ("timeout", "UNCERTAIN", "TIMEOUT"),
        ("partial", "FAILED", "UPSTREAM_GRAPHQL_ERROR"),
    ],
)
def test_failures_no_retry_backoff(context, failure, status, code):
    configure(context)
    context["failure"] = failure
    result = execute(context)
    assert result["status"] == status and result["error_code"] == code, result
    assert context["calls"] == 1 and count(context, ProductPriceHistory) == 0
    context["failure"] = None
    assert execute(context)["requests"] == 0
    assert context["calls"] == 1


def test_page_two_failure_preserves_page_one_and_cursor(context):
    category = configure(context, max_pages=2)
    context.update(has_next=True, failure=429, failure_page=2)
    result = execute(context)
    assert result["status"] == "FAILED" and count(context, ProductPriceHistory) == 1
    with context["factory"]() as session:
        assert session.get(ShopeeDiscoveryPolicy, category).state["next_page"] == 2
    advance(context)
    context.update(failure=None, pages={2: [node(identity=2)]}, has_next=False)
    assert execute(context)["status"] == "SUCCEEDED"
    assert [r["page"] for r in context["requests"]] == [1, 2, 2]


def test_quotas_and_configuration_cannot_reset_budget(context):
    category = configure(context, requests_per_hour=1)
    execute(context)
    advance(context)
    configure(context, requests_per_hour=1)
    assert execute(context)["requests"] == 0
    assert context["calls"] == 1
    assert context["discovery"].status()["commercial_jobs"] == "PAUSED"
    with context["factory"]() as session:
        assert session.get(ShopeeDiscoveryPolicy, category).last_run_at is not None


def test_exclusions_product_price_and_min_discount(context):
    configure(context, exclusions=["recondicionado"], max_price="90")
    assert execute(context)["result"]["pages"][0]["dispositions"]["FILTERED"] == 1
    advance(context)
    configure(context, min_discount="1")
    execute(context)
    with context["factory"]() as session:
        assert session.scalar(select(ShopeeCollectionCandidate)).status == "FILTERED"


def test_manual_decision_audited_and_does_not_bypass_accessory_filter(context):
    configure(context)
    context["pages"][1] = [node("PlayStation 5")]
    execute(context)
    with context["factory"]() as session:
        product_id = session.scalar(select(Product.id))
    decision = context["discovery"].manual_decision(product_id, "ACCEPT", "synthetic owner")
    assert decision["audit_id"]
    advance(context)
    context["pages"][1] = [node("Cabo HDMI PlayStation 5")]
    result = execute(context, "MONITOR")
    assert result["result"]["pages"][0]["dispositions"]["FILTERED"] == 1
    assert count(context, ProductPriceHistory) == 1


def test_concurrency_same_key_or_other_run(context):
    configure(context)
    started, proceed = Event(), Event()
    context["handler"] = lambda: (started.set(), proceed.wait(15))
    key = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(execute, context, "DISCOVER", key)
        assert started.wait(10)
        try:
            assert execute(context, request_id=key)["status"] == "RESERVED"
            assert execute(context)["status"] == "BUSY"
        finally:
            proceed.set()
        assert task.result(timeout=20)["status"] == "SUCCEEDED"
    assert context["calls"] == 1


def test_product_lease_blocks_monitoring(context):
    configure(context)
    execute(context)
    advance(context)
    with context["factory"]() as session:
        product_id = session.scalar(select(Product.id))
    name = f"monitor:product:{product_id}"
    owner = acquire_lease(context["factory"], name, 120, lambda: context["clock"][0])
    try:
        assert execute(context, "MONITOR")["error_code"] == "MONITORING_BUSY"
        assert context["calls"] == 1
    finally:
        release_lease(context["factory"], name, owner)


def test_recover_abandoned_never_retries(context):
    key = str(uuid4())
    with context["factory"]() as session:
        session.add(
            ShopeeDiscoveryRun(
                id=key,
                operation="DISCOVER",
                status="RESERVED",
                requests=1,
                specification={},
                started_at=context["clock"][0] - timedelta(seconds=121),
            )
        )
        session.commit()
    assert execute(context, "RECOVER")["result"]["recovered"] == [key]
    assert context["discovery"].read(key)["status"] == "UNCERTAIN"
    assert context["calls"] == 0


def test_sealed_scope_and_scheduler_no_threads(context):
    configure(context)
    runtime = MonitoringRuntime(context["factory"], context["settings"])
    prepared = context["discovery"].prepare_schedule(runtime)
    assert prepared["status"] == "PAUSED" and runtime._scheduler is None
    for job in runtime._prepared_jobs.values():
        assert job["func"]()["status"] == "BLOCKED"
    assert set(JOB_IDS) == set(runtime._prepared_jobs)
    runtime.start()
    assert runtime._scheduler is None and context["calls"] == 0
    service = ShopeeDiscoveryService(context["factory"], context["settings"])
    with pytest.raises(ValueError, match="registro isolado"):
        service.execute(uuid4())


@pytest.mark.parametrize(
    "flag",
    [
        "shopee_enabled",
        "mercado_livre_enabled",
        "telegram_publisher_enabled",
        "telegram_real_sends_authorized",
        "simulation_automation_enabled",
    ],
)
def test_global_flags_fail_closed(context, flag):
    setattr(context["settings"], flag, True)
    with pytest.raises(ValueError):
        execute(context)
    assert context["calls"] == 0


def verified_fictional_setup(ctx, monkeypatch):
    # Future policy exercise only: inject explicit synthetic evidence, never claim
    # that current productOfferV2 actually supplies stock or public access.
    original = ctx["connector"]._snapshot

    def snapshot(*args, **kwargs):
        return replace(
            original(*args, **kwargs),
            availability=Availability.AVAILABLE,
            price_access=PriceAccess.PUBLIC,
        )

    monkeypatch.setattr(ctx["connector"], "_snapshot", snapshot)
    with ctx["factory"]() as session:
        destination = PublicationDestination(
            name="[FICTÍCIO] laboratório",
            channel=DestinationChannel.SIMULATED,
            external_id="discovery-offline",
            enabled=True,
            manual_approval_required=True,
            category_mode=CategoryMode.ALL,
        )
        session.add(destination)
        session.commit()
        destination_id = destination.id
    configure(ctx, destinations=[destination_id], approval={"min_score": "80"})
    execute(ctx)
    with ctx["factory"]() as session:
        product = session.scalar(select(Product))
        session.add(
            AffiliateLink(
                product_id=product.id,
                url="https://s.shopee.com.br/FICTICIO",
                source="MANUAL_SHOPEE_VERIFIED",
            )
        )
        session.commit()
    advance(ctx)
    ctx["pages"][1] = [node(price="90.00")]
    execute(ctx, "MONITOR")
    return destination_id


def test_dry_run_template_queue_dedup_no_approval(context, monkeypatch):
    verified_fictional_setup(context, monkeypatch)
    result = execute(context, "EVALUATE")
    item = result["result"]["candidates"][0]
    assert item["decision"] == "MOCK_WOULD_APPROVE", item
    assert "Publicidade" in item["preview"] and "90,00" in item["preview"]
    assert len(item["preview_hash"]) == 64
    with context["factory"]() as session:
        queue = session.scalar(select(PublicationQueueItem))
        assert queue.status == QueueStatus.PENDING and queue.is_simulated
        assert queue.attempts == 0 and queue.validated_at is None
    next_result = execute(context, "EVALUATE")["result"]["candidates"][0]
    assert next_result["decision"] == "BLOCKED"
    assert count(context, PublicationQueueItem) == 1
    assert context["calls"] == 2


def test_cooldown_requires_three_hours_and_new_observation(context, monkeypatch):
    verified_fictional_setup(context, monkeypatch)
    execute(context, "EVALUATE")
    with context["factory"]() as session:
        queue = session.scalar(select(PublicationQueueItem))
        queue.status, queue.active_slot, queue.published_at = (
            QueueStatus.PUBLISHED,
            None,
            context["clock"][0],
        )
        session.commit()
    advance(context, 3601)
    context["pages"][1] = [node(price="80.00")]
    execute(context, "MONITOR")
    assert execute(context, "EVALUATE")["result"]["candidates"][0]["decision"] == "BLOCKED"
    advance(context, 7201)
    context["pages"][1] = [node(price="70.00")]
    execute(context, "MONITOR")
    result = execute(context, "EVALUATE")["result"]["candidates"][0]
    assert result["decision"] == "MOCK_WOULD_APPROVE", result
    assert count(context, PublicationQueueItem) == 2


def test_unknown_facts_block_preparation(context):
    with context["factory"]() as session:
        destination = PublicationDestination(
            name="grupo pausado",
            channel=DestinationChannel.TELEGRAM,
            external_id="-5309308689",
            enabled=False,
            category_mode=CategoryMode.ALL,
        )
        session.add(destination)
        session.commit()
        dest_id = destination.id
    configure(context, destinations=[dest_id])
    execute(context)
    result = execute(context, "EVALUATE")["result"]["candidates"][0]
    assert result["decision"] == "BLOCKED"
    assert "DESTINATION_PAUSED" in result["reasons"]
    assert "UNVERIFIED_STOCK_OR_PUBLIC_PRICE" in result["reasons"]
    assert count(context, PublicationQueueItem) == 0


def test_api_auth_validation_and_no_live_execution(application, client):
    base = "/api/v1/shopee-discovery"
    assert client.get(base + "/status").status_code == 401
    status = client.get(base + "/status", headers=api.HEADERS)
    assert status.status_code == 200 and not status.json()["real_calls_authorized"]
    result = client.post(
        base + "/offline-exercises",
        headers=api.HEADERS,
        json={"actor": "owner", "operation": "DISCOVER", "request_id": str(uuid4())},
    )
    assert result.status_code == 409
    assert client.post(base + "/schedule/prepare", headers=api.HEADERS).json()["status"] == "PAUSED"
    for endpoint in ("runs", "findings", "selections"):
        assert client.get(base + "/" + endpoint, headers=api.HEADERS).json() == []
    assert client.post(base + "/enable", headers=api.HEADERS).status_code == 404


def test_existing_approval_is_not_changed_by_evaluation(context, monkeypatch):
    verified_fictional_setup(context, monkeypatch)
    execute(context, "EVALUATE")
    with context["factory"]() as session:
        item = session.scalar(select(PublicationQueueItem))
        item.status = QueueStatus.READY
        session.commit()
        before = (item.status, item.validated_at, item.offer_id, item.attempts)
    execute(context, "EVALUATE")
    with context["factory"]() as session:
        item = session.scalar(select(PublicationQueueItem))
        assert (item.status, item.validated_at, item.offer_id, item.attempts) == before


def test_expired_lease_discards_entire_page_without_retry(context):
    configure(context)
    context["handler"] = lambda: advance(context, 121)
    key = uuid4()
    result = execute(context, request_id=key)
    assert result["status"] == "UNCERTAIN" and result["error_code"] == "LEASE_LOST"
    assert count(context, ProductPriceHistory) == 0
    assert execute(context, request_id=key) == result and context["calls"] == 1


def test_mock_transport_cannot_be_replaced_with_network(context):
    old = context["transport"].client
    context["transport"].client = httpx.Client(trust_env=False)
    try:
        with pytest.raises(ValueError, match="MockTransport"):
            execute(context)
    finally:
        context["transport"].client.close()
        context["transport"].client = old
    assert context["calls"] == 0


def test_stale_candidate_never_creates_draft(context, monkeypatch):
    verified_fictional_setup(context, monkeypatch)
    advance(context, 601)
    result = execute(context, "EVALUATE")["result"]["candidates"][0]
    assert result["decision"] == "BLOCKED"
    assert "FRESH_COMPARABLE_DROP_REQUIRED" in result["reasons"]
    assert count(context, PublicationQueueItem) == 0


def test_live_destination_is_blocked_even_when_enabled_in_isolated_database(context, monkeypatch):
    destination_id = verified_fictional_setup(context, monkeypatch)
    with context["factory"]() as session:
        destination = session.get(PublicationDestination, destination_id)
        destination.channel = DestinationChannel.TELEGRAM
        destination.external_id = "-5309308689"
        session.commit()
    result = execute(context, "EVALUATE")["result"]["candidates"][0]
    assert "SIMULATED_DESTINATION_REQUIRED" in result["reasons"]
    assert count(context, PublicationQueueItem) == 0


def test_price_rejection_pauses_previously_selected_product(context):
    configure(context, max_price="150", category_priority=10)
    execute(context)
    # A different category's rejection must not pause the valid Games selection.
    configure(context, "casa-decoracao")
    execute(context)
    with context["factory"]() as session:
        assert session.scalar(select(ShopeeDiscoveryProduct)).state == "SELECTED"
    advance(context)
    context["pages"][1] = [node(price="200.00")]
    execute(context, "MONITOR")
    with context["factory"]() as session:
        assert session.scalar(select(ShopeeDiscoveryProduct)).state == "PAUSED"
    assert count(context, ProductPriceHistory) == 1


def test_category_manual_correction_is_authoritative_for_evaluation(context, monkeypatch):
    from app.services.categories import CategoryService

    verified_fictional_setup(context, monkeypatch)
    with context["factory"]() as session:
        product = session.scalar(select(Product))
        category = session.scalar(select(Category).where(Category.slug == "moda"))
        CategoryService().set_manual(session, product, [category.id])
        session.commit()
    result = execute(context, "EVALUATE")["result"]["candidates"][0]
    assert "CATEGORY_INACTIVE_OR_CHANGED" in result["reasons"]
    assert count(context, PublicationQueueItem) == 0


def test_prediction_limits_persist_across_evaluation_runs(context, monkeypatch):
    verified_fictional_setup(context, monkeypatch)
    first = execute(context, "EVALUATE")["result"]["candidates"][0]
    assert first["decision"] == "MOCK_WOULD_APPROVE"
    second = execute(context, "EVALUATE")["result"]["candidates"][0]
    assert "DESTINATION_PREDICTION_LIMIT" in second["reasons"]
    assert second["publication_authorized"] is False
