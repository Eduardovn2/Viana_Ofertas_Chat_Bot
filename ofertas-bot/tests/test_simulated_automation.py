"""Entire automatic offer pipeline, no external transport and no live credentials."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event

import httpx
import pytest
import test_api as api
import test_shopee as shopee_fixtures
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.telegram_simulated import MockTelegramDelivery
from app.jobs.simulation import SimulationMonitoringRuntime
from app.models import (
    AffiliateLink,
    Base,
    Category,
    CategoryMode,
    DestinationCategory,
    DestinationChannel,
    ManualOfferDetails,
    MonitoringJob,
    Offer,
    Platform,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    SimulationAudit,
    SimulationPolicy,
    TelegramPublication,
    TelegramSimulation,
    TelegramTechnicalPublication,
)
from app.services.categories import CategoryService
from app.services.simulated_automation import SimulatedAutomation
from app.services.simulation_safety import (
    SimulationEnrollmentInput,
    SimulationPolicyInput,
    configure_policy,
    enroll_product,
    require_simulation_scope,
)
from app.services.telegram_publication import TelegramPublicationService

application = api.application
client = api.client
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Nenhuma API real é permitida nesta fase de testes.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def setup(engine, url=None, names=None):
    factory = build_session_factory(engine)
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=str(url or "sqlite://"),
        scheduler_enabled=True,
        simulation_automation_enabled=True,
        price_provider="simulated",
        telegram_publisher_enabled=False,
        telegram_real_sends_authorized=False,
        mercado_livre_enabled=False,
        shopee_enabled=False,
        priority_interval_seconds=60,
        general_interval_seconds=60,
        provider_min_interval_seconds=Decimal(0),
    )
    clock = [NOW]
    with factory() as session:
        CategoryService().seed(session)
        destination = PublicationDestination(
            name="[SIMULATED] Telegram",
            channel=DestinationChannel.SIMULATED,
            external_id="simulation:automation",
            enabled=True,
            manual_approval_required=True,
            category_mode=CategoryMode.ALL,
            min_interval_seconds=1,
            max_messages_per_hour=5,
        )
        real = PublicationDestination(
            name="Viana Ofertas",
            channel=DestinationChannel.TELEGRAM,
            external_id="-5309308689",
            enabled=False,
            manual_approval_required=True,
        )
        session.add_all([destination, real])
        session.flush()
        product_ids = []
        for index, name in enumerate(names or ["PlayStation 5"]):
            product = Product(
                name="[SIMULATED] " + name,
                platform=Platform.MERCADO_LIVRE,
                original_url=f"https://example.test/simulation/{index}",
                external_id=f"SIMULATED{index}",
                classification_metadata={"simulation_only": True},
                monitoring_enabled=True,
                priority=True,
            )
            product.affiliate_link = AffiliateLink(
                url="https://meli.la/SIMULATED_ONLY", source="manual_official"
            )
            session.add(product)
            session.flush()
            enroll_product(session, settings, product.id, {"prices": ["100.00", "90.00"]})
            product_ids.append(product.id)
        configure_policy(
            session, settings, destination, {"enabled": True, "automatic_approval": True}
        )
        session.commit()
        destination_id, real_id = destination.id, real.id
    pipeline = SimulatedAutomation(factory, settings, lambda: clock[0])
    runtime = SimulationMonitoringRuntime(factory, settings, lambda: clock[0], pipeline)
    return {
        "factory": factory,
        "settings": settings,
        "clock": clock,
        "pipeline": pipeline,
        "runtime": runtime,
        "destination": destination_id,
        "real_destination": real_id,
        "products": product_ids,
    }


@pytest.fixture
def context(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'automation.simulation.db').as_posix()}")
    Base.metadata.create_all(engine)
    result = setup(engine)
    yield result
    result["runtime"].shutdown()
    engine.dispose()


def stage(context):
    runtime = context["runtime"]
    for product_id in context["products"]:
        runtime.run_product(product_id)
    context["clock"][0] += timedelta(seconds=60)
    for product_id in context["products"]:
        runtime.run_product(product_id)
    context["pipeline"].capture()
    with context["factory"]() as session:
        return session.scalar(select(TelegramSimulation.id).order_by(TelegramSimulation.id.desc()))


def rows(context, model):
    with context["factory"]() as session:
        return list(
            session.scalars(select(model).order_by(list(model.__table__.primary_key.columns)[0]))
        )


def test_full_scheduler_history_scoring_queue_mock_and_audit(context):
    runtime, pipeline = context["runtime"], context["pipeline"]
    assert runtime.run_due()["succeeded"] == 1
    assert pipeline.metrics()["opportunities"] == 0
    context["clock"][0] += timedelta(seconds=60)
    assert runtime.run_due()["succeeded"] == 1
    outbox = rows(context, TelegramSimulation)[0]
    assert outbox.status == "SIMULATED" and outbox.mock_message_id.startswith("simulated:")
    assert outbox.is_simulated and outbox.attempt_number == 1 and outbox.active_slot is None
    assert outbox.preview_payload["mode"] == "SIMULATED"
    assert outbox.preview_payload["message"]["text"].startswith("🧪 SIMULAÇÃO — NENHUM ENVIO REAL")
    offer = rows(context, Offer)[0]
    assert offer.current_price == Decimal("90.00") and offer.historical_price == Decimal("100.00")
    assert (
        offer.previous_price is None
        and offer.coupon_id is None
        and offer.discount_percentage == Decimal("10.00")
    )
    assert offer.score > 0
    assert all(history.is_simulated for history in rows(context, ProductPriceHistory))
    assert [event.event for event in rows(context, SimulationAudit)] == [
        "ENQUEUED",
        "AUTO_APPROVED_SIMULATED",
        "MOCK_ATTEMPT_RESERVED",
        "SIMULATED",
    ]
    for model in (
        PublicationLog,
        TelegramPublication,
        TelegramTechnicalPublication,
        ManualOfferDetails,
    ):
        assert rows(context, model) == []
    assert pipeline.metrics()["mock_calls"] == 1 and pipeline.metrics()["real_telegram_calls"] == 0
    assert (
        not context["settings"].telegram_publisher_enabled
        and not context["settings"].telegram_real_sends_authorized
    )
    assert rows(context, PublicationDestination)[-1].enabled is False


@pytest.mark.parametrize(
    "terms,product_ids,eligible",
    [
        (["PLAYSTATION 5"], [], True),
        (["station"], [], False),
        (["playstation 4"], [], False),
        (["iphone 15", "playstation 5"], [], True),
        ([], ["own"], True),
        (["iphone 15"], ["own"], True),
    ],
)
def test_specific_filters_are_normalized_and_product_or_term(context, terms, product_ids, eligible):
    with context["factory"]() as session:
        configure_policy(
            session,
            context["settings"],
            session.get(PublicationDestination, context["destination"]),
            {
                "enabled": True,
                "automatic_approval": True,
                "terms": terms,
                "product_ids": context["products"] if product_ids else [],
            },
        )
        session.commit()
    stage(context)
    assert bool(rows(context, TelegramSimulation)) == eligible
    if not eligible:
        assert rows(context, SimulationAudit)[0].reason == "PRODUCT_TERM_FILTER"


def test_observed_hdmi_search_result_is_not_a_playstation_console_opportunity(context):
    node = json.loads(shopee_fixtures.OBSERVED_NODE_PATH.read_text(encoding="utf-8"))
    source = shopee_fixtures.connector(shopee_fixtures.FakeTransport(shopee_fixtures.payload(node)))
    snapshot = source.discover_offers("PlayStation 5", limit=1).items[0]
    assert snapshot.name == node["productName"] and "Cabo HDMI" in snapshot.name
    with context["factory"]() as session:
        product = session.get(Product, context["products"][0])
        # Only the title is replayed into the existing fictitious-price pipeline.
        product.name = "[SIMULATED] " + snapshot.name
        links = CategoryService().classify(session, product)
        assert {link.category.slug for link in links} == {"games"}
        assert all("console" not in link.explanation.casefold() for link in links)
        configure_policy(
            session,
            context["settings"],
            session.get(PublicationDestination, context["destination"]),
            {"enabled": True, "automatic_approval": True, "terms": ["PlayStation 5"]},
        )
        session.commit()
    stage(context)
    assert rows(context, TelegramSimulation) == []
    assert any(row.reason == "PRODUCT_TERM_FILTER" for row in rows(context, SimulationAudit))
    assert context["pipeline"].delivery is None


def test_category_filter_is_additional_and_not_overridden_by_product_id(context):
    with context["factory"]() as session:
        destination = session.get(PublicationDestination, context["destination"])
        destination.category_mode = CategoryMode.SELECTED
        category = session.scalar(select(Category).where(Category.slug == "beleza"))
        session.add(DestinationCategory(destination_id=destination.id, category_id=category.id))
        configure_policy(
            session,
            context["settings"],
            destination,
            {"enabled": True, "automatic_approval": True, "product_ids": context["products"]},
        )
        session.commit()
    stage(context)
    assert rows(context, TelegramSimulation) == []
    assert rows(context, SimulationAudit)[0].reason == "CATEGORY_FILTER"


@pytest.mark.parametrize(
    "policy,expected",
    [
        ({"max_price": "89.99"}, "MAX_PRICE_FILTER"),
        ({"min_discount": "10.01"}, "MIN_DISCOUNT_FILTER"),
        ({"max_price": "90.00", "min_discount": "10.00"}, None),
    ],
)
def test_price_discount_filter_boundaries(context, policy, expected):
    with context["factory"]() as session:
        configure_policy(
            session,
            context["settings"],
            session.get(PublicationDestination, context["destination"]),
            {"enabled": True, "automatic_approval": True, **policy},
        )
        session.commit()
    stage(context)
    assert bool(rows(context, TelegramSimulation)) == (expected is None)
    if expected:
        assert rows(context, SimulationAudit)[0].reason == expected


@pytest.mark.parametrize(
    "prices", [["90.00"], ["90.00", "90.00"], ["90.00", "100.00"], ["100.00", None]]
)
def test_no_opportunity_without_comparable_price_drop(context, prices):
    with context["factory"]() as session:
        enroll_product(session, context["settings"], context["products"][0], {"prices": prices})
        session.commit()
    stage(context)
    assert rows(context, TelegramSimulation) == [] and rows(context, Offer) == []


def test_manual_simulation_approval_preserves_real_controls(context):
    with context["factory"]() as session:
        session.get(SimulationPolicy, context["destination"]).automatic_approval = False
        session.commit()
    entry_id = stage(context)
    pipeline = context["pipeline"]
    entry = rows(context, TelegramSimulation)[0]
    assert entry.status == "PENDING" and pipeline.dispatch(entry_id) == "PENDING"
    with pytest.raises(ValueError):
        pipeline.approve(entry_id, "a" * 64, "Auditor sintético")
    pipeline.approve(entry_id, entry.preview_hash, "Auditor sintético")
    assert pipeline.dispatch(entry_id) == "SIMULATED"
    assert rows(context, TelegramSimulation)[0].approved_by == "Auditor sintético"


def test_deduplicated_queue_and_three_hour_republication_with_fresh_history(context):
    entry_id = stage(context)
    pipeline = context["pipeline"]
    assert pipeline.capture()["deduplicated"] == 1
    assert pipeline.dispatch(entry_id) == "SIMULATED"
    assert pipeline.dispatch(entry_id) == "SIMULATED"
    assert pipeline.delivery.calls == 1
    context["clock"][0] += timedelta(seconds=10800 - 1)
    context["runtime"].run_product(context["products"][0])
    pipeline.capture()
    assert len(rows(context, TelegramSimulation)) == 1
    context["clock"][0] += timedelta(seconds=1)
    pipeline.capture()
    assert len(rows(context, TelegramSimulation)) == 1
    context["runtime"].run_product(context["products"][0])
    pipeline.capture()
    assert len(rows(context, TelegramSimulation)) == 2
    second = rows(context, TelegramSimulation)[1]
    assert pipeline.dispatch(second.id) == "SIMULATED" and pipeline.delivery.calls == 2


@pytest.mark.parametrize(
    "change", ["price", "category", "policy", "paused", "real_history", "unmarked"]
)
def test_dispatch_revalidates_current_evidence_and_filters(context, change):
    entry_id = stage(context)
    with context["factory"]() as session:
        if change == "price":
            session.get(
                Offer, rows(context, TelegramSimulation)[0].offer_id
            ).current_price = Decimal("89.00")
        elif change == "category":
            for category in session.scalars(select(Category)):
                category.active = False
        elif change == "policy":
            session.get(SimulationPolicy, context["destination"]).terms = ["iphone 15"]
        elif change == "paused":
            session.get(PublicationDestination, context["destination"]).enabled = False
        elif change == "real_history":
            session.scalars(
                select(ProductPriceHistory).order_by(ProductPriceHistory.id.desc())
            ).first().is_simulated = False
        else:
            session.get(Product, context["products"][0]).classification_metadata = {}
        session.commit()
    assert context["pipeline"].dispatch(entry_id) == "EXPIRED"
    assert context["pipeline"].delivery is None


def test_stale_price_and_changed_preview_never_dispatch(context):
    entry_id = stage(context)
    with context["factory"]() as session:
        session.get(Product, context["products"][0]).name += " edição"
        session.commit()
    assert context["pipeline"].dispatch(entry_id) == "PENDING"
    context["pipeline"].capture()
    context["clock"][0] += timedelta(seconds=601)
    assert context["pipeline"].dispatch(entry_id) == "EXPIRED"


@pytest.mark.parametrize(
    "fault,expected", [("timeout", "UNCERTAIN"), ("invalid", "UNCERTAIN"), ("forbidden", "FAILED")]
)
def test_mock_failure_timeout_no_retry_and_durable_audit(context, fault, expected):
    entry_id = stage(context)
    pipeline = context["pipeline"]
    pipeline.delivery = MockTelegramDelivery(fault)
    assert pipeline.dispatch(entry_id) == expected
    assert pipeline.dispatch(entry_id) == expected and pipeline.delivery.calls == 1
    assert expected in [event.event for event in rows(context, SimulationAudit)]
    assert rows(context, TelegramSimulation)[0].attempt_number == 1
    if expected == "UNCERTAIN":
        assert rows(context, TelegramSimulation)[0].active_slot == 1


def test_concurrent_dispatch_claims_one_mock_request(context, monkeypatch):
    entry_id = stage(context)
    pipeline = context["pipeline"]
    entered, release = Event(), Event()
    pipeline.delivery = MockTelegramDelivery()
    original = pipeline.delivery.send

    def delayed(message):
        with context["factory"]() as session:
            assert session.get(TelegramSimulation, entry_id).status == "DISPATCHING"
            assert (
                session.scalar(
                    select(SimulationAudit.id).where(
                        SimulationAudit.event == "MOCK_ATTEMPT_RESERVED"
                    )
                )
                is not None
            )
        entered.set()
        assert release.wait(10)
        return original(message)

    monkeypatch.setattr(pipeline.delivery, "send", delayed)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(pipeline.dispatch, entry_id)
        assert entered.wait(10)
        try:
            assert workers.submit(pipeline.dispatch, entry_id).result(10) == "DEFERRED"
        finally:
            release.set()
        assert first.result(10) == "SIMULATED"
    assert pipeline.delivery.calls == 1


def test_persistence_failure_after_mock_is_uncertain(context, monkeypatch):
    entry_id = stage(context)

    def fail(*args):
        raise OperationalError("synthetic database failure", {}, None)

    monkeypatch.setattr(context["pipeline"], "_finish", fail)
    assert context["pipeline"].dispatch(entry_id) == "UNCERTAIN"
    assert rows(context, TelegramSimulation)[0].active_slot == 1
    assert context["pipeline"].delivery.calls == 1


def test_recover_abandoned_claim_without_publisher_or_retry(context):
    entry_id = stage(context)
    with context["factory"]() as session:
        entry = session.get(TelegramSimulation, entry_id)
        entry.status, entry.attempt_number, entry.claimed_at = "DISPATCHING", 1, context["clock"][0]
        session.commit()
    assert context["pipeline"].recover() == 0
    context["clock"][0] += timedelta(seconds=121)
    assert context["pipeline"].recover() == 1
    assert (
        context["pipeline"].dispatch(entry_id) == "UNCERTAIN"
        and context["pipeline"].delivery is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("telegram_publisher_enabled", True),
        ("telegram_real_sends_authorized", True),
        ("mercado_livre_enabled", True),
        ("shopee_enabled", True),
        ("price_provider", "marketplaces"),
    ],
)
def test_simulation_scope_rejects_real_controls(context, field, value):
    setattr(context["settings"], field, value)
    with context["factory"]() as session, pytest.raises(ValueError):
        require_simulation_scope(session, context["settings"])


@pytest.mark.parametrize(
    "data",
    [
        {"terms": [""]},
        {"terms": ["PS5", "ps5"]},
        {"terms": ["a" * 101]},
        {"product_ids": [True]},
        {"product_ids": [1, 1]},
        {"max_price": "0"},
        {"min_discount": "100.01"},
        {"automatic_approval": "true"},
    ],
)
def test_policy_validation(data):
    with pytest.raises(ValidationError):
        SimulationPolicyInput.model_validate(data)


@pytest.mark.parametrize("prices", [[], ["-1"], ["0"], ["1.001"], ["NaN"]])
def test_profile_rejects_invalid_prices(prices):
    with pytest.raises(ValidationError):
        SimulationEnrollmentInput(prices=prices)


def test_cannot_apply_policy_to_real_destination_or_publish_fake_as_real(context):
    with context["factory"]() as session:
        with pytest.raises(ValueError):
            configure_policy(
                session,
                context["settings"],
                session.get(PublicationDestination, context["real_destination"]),
                {"automatic_approval": True},
            )
    entry_id = stage(context)
    with context["factory"]() as session:
        entry = session.get(TelegramSimulation, entry_id)
        with pytest.raises(Exception) as error:
            TelegramPublicationService(context["settings"]).preview(
                session, entry.offer_id, context["real_destination"]
            )
        assert getattr(error.value, "code", None) == "OUTBOX_INVALID"
        entry.is_simulated = False
        with pytest.raises(IntegrityError):
            session.commit()
    assert rows(context, TelegramPublication) == []


def test_scheduler_respects_due_times_and_provider_backoff(context, monkeypatch):
    runtime = context["runtime"]
    assert runtime.run_due()["succeeded"] == 1
    assert runtime.run_due()["attempted"] == 0

    def failed(*args, **kwargs):
        raise ValueError("simulated provider failure")

    monkeypatch.setattr(runtime, "_provider", failed)
    context["clock"][0] += timedelta(seconds=60)
    assert runtime.run_due()["failed"] == 1
    assert rows(context, MonitoringJob)[0].failure_count == 1
    context["clock"][0] += timedelta(seconds=60)
    assert runtime.run_due()["failed"] == 1
    assert rows(context, MonitoringJob)[0].next_run_at == context["clock"][0] + timedelta(
        seconds=120
    )


def test_scheduler_is_never_started_in_tests(context):
    assert context["runtime"].start()["threads_blocked"] is True
    assert context["runtime"]._scheduler is None


def test_mock_transport_cannot_be_replaced_with_live_http(context):
    delivery = MockTelegramDelivery()
    delivery._client._transport = httpx.HTTPTransport(retries=0)
    context["pipeline"].delivery = delivery
    entry_id = stage(context)
    assert context["pipeline"].dispatch(entry_id) == "UNCERTAIN"
    assert delivery.calls == 0


def test_expired_claim_owner_cannot_confirm_success(context, monkeypatch):
    entry_id = stage(context)
    pipeline = context["pipeline"]
    pipeline.delivery = MockTelegramDelivery()
    original = pipeline.delivery.send

    def expired(message):
        result = original(message)
        context["clock"][0] += timedelta(seconds=context["settings"].monitoring_lease_seconds + 1)
        return result

    monkeypatch.setattr(pipeline.delivery, "send", expired)
    assert pipeline.dispatch(entry_id) == "UNCERTAIN"
    assert rows(context, TelegramSimulation)[0].error_code == "MOCK_LEASE_EXPIRED"
    assert pipeline.delivery.calls == 1


def test_fake_history_cannot_enter_real_offer_outbox_even_with_manual_details(context):
    from app.integrations.errors import IntegrationError
    from app.services.manual_offers import read_manual_offer

    entry_id = stage(context)
    with context["factory"]() as session:
        entry = session.get(TelegramSimulation, entry_id)
        session.add(
            ManualOfferDetails(
                offer_id=entry.offer_id,
                verified_by="Auditor sintético",
                verified_at=context["clock"][0],
            )
        )
        session.commit()
        with pytest.raises(IntegrationError) as error:
            read_manual_offer(session, entry.offer_id, context["settings"], context["clock"][0])
        assert error.value.code == "MANUAL_OFFER_INVALID"


def test_provider_throttle_is_reused(context):
    from app.jobs.runtime import ProviderRateLimitedError

    context["settings"].provider_min_interval_seconds = Decimal("60")
    context["runtime"].run_product(context["products"][0])
    with pytest.raises(ProviderRateLimitedError):
        context["runtime"].run_product(context["products"][0])
    assert len(rows(context, ProductPriceHistory)) == 1


def test_pipeline_limits_each_batch_to_three_mock_deliveries(context):
    with context["factory"]() as session:
        for index in range(4):
            destination = PublicationDestination(
                name=f"[SIMULATED] Destino {index}",
                external_id=f"simulation:batch:{index}",
                channel=DestinationChannel.SIMULATED,
                enabled=True,
                manual_approval_required=True,
                category_mode=CategoryMode.ALL,
            )
            session.add(destination)
            session.flush()
            configure_policy(
                session,
                context["settings"],
                destination,
                {"enabled": True, "automatic_approval": True},
            )
        session.commit()
    stage(context)
    result = context["pipeline"].run_pending()
    assert result["simulated"] == 3 and context["pipeline"].delivery.calls == 3
    assert sum(entry.status == "READY" for entry in rows(context, TelegramSimulation)) == 2
    context["clock"][0] += timedelta(seconds=1)
    assert context["pipeline"].run_pending()["simulated"] == 2
    assert context["pipeline"].delivery.calls == 5


def test_hourly_limit_remains_at_most_five_per_simulated_destination(context):
    with context["factory"]() as session:
        for index in range(5):
            product = Product(
                name=f"[SIMULATED] Console {index}",
                external_id=f"EXTRA{index}",
                platform=Platform.MERCADO_LIVRE,
                original_url=f"https://example.test/extra/{index}",
                classification_metadata={"simulation_only": True},
                monitoring_enabled=True,
            )
            product.affiliate_link = AffiliateLink(
                url="https://meli.la/SIMULATED_ONLY", source="manual_official"
            )
            session.add(product)
            session.flush()
            enroll_product(session, context["settings"], product.id, {"prices": ["100", "90"]})
            context["products"].append(product.id)
        session.commit()
    stage(context)
    outcomes = []
    for entry in rows(context, TelegramSimulation):
        outcomes.append(context["pipeline"].dispatch(entry.id))
        context["clock"][0] += timedelta(seconds=1)
    assert outcomes == ["SIMULATED"] * 5 + ["DEFERRED"]
    assert context["pipeline"].delivery.calls == 5


def test_api_is_admin_only_and_cannot_route_real_destination(application, client):
    root = "/api/v1/simulated-automation"
    assert client.get(root + "/status").status_code == 401
    response = client.get(root + "/status", headers=api.HEADERS)
    assert response.status_code == 200 and response.json()["mode"] == "SIMULATED"
    with application.state.session_factory() as session:
        destination = PublicationDestination(
            name="Real pausado",
            external_id="-5309308689",
            channel=DestinationChannel.TELEGRAM,
            enabled=False,
        )
        session.add(destination)
        session.commit()
        dest_id = destination.id
    assert (
        client.put(
            root + f"/destinations/{dest_id}/policy",
            headers=api.HEADERS,
            json={"enabled": True, "automatic_approval": True},
        ).status_code
        == 422
    )
    assert client.get(root + "/publications", headers=api.HEADERS).json() == []
    assert client.post(root + "/run", headers=api.HEADERS).json()["simulated"] == 0
