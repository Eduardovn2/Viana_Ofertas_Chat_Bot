"""No real scheduler thread, sleep, network or external database is used here."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event, Lock

import pytest
from sqlalchemy import func, select

import app.jobs.runtime as runtime_module
from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.mock import SimulatedPriceProvider
from app.jobs.runtime import (
    MonitoringBusyError,
    MonitoringRuntime,
    ProviderRateLimitedError,
)
from app.models import (
    Base,
    MonitoringJob,
    MonitoringJobStatus,
    MonitoringLease,
    Offer,
    Platform,
    Product,
    ProductPriceHistory,
)
from app.services.configuration import save_monitoring_configuration
from app.services.leases import acquire_lease, release_lease, renew_lease


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        self.lock = Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += timedelta(seconds=seconds)


@pytest.fixture
def database(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'scheduler.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test", provider_min_interval_seconds=Decimal("0"))


def product(database, *, priority=False, enabled=True, platform=Platform.MERCADO_LIVRE):
    with database() as session:
        row = Product(
            name="Produto simulado",
            platform=platform,
            original_url="https://produto.mercadolivre.com.br/MLB-3810190843-exemplo_JM",
            priority=priority,
            monitoring_enabled=enabled,
        )
        session.add(row)
        session.commit()
        return row.id


def history_count(database):
    with database() as session:
        return session.scalar(select(func.count()).select_from(ProductPriceHistory))


def test_lease_exclusion_expiry_fencing_and_owner_safe_release(database, clock):
    first = acquire_lease(database, "resource", 10, clock)
    assert first is not None
    assert acquire_lease(database, "resource", 10, clock) is None
    clock.advance(10)
    with database() as session:
        assert not renew_lease(session, "resource", first, 10, clock)
    second = acquire_lease(database, "resource", 10, clock)
    assert second is not None and second != first
    assert not release_lease(database, "resource", first)
    with database() as session:
        assert session.get(MonitoringLease, "resource").owner == second
        assert renew_lease(session, "resource", second, 10, clock)
        session.commit()
    assert release_lease(database, "resource", second)


def test_two_database_sessions_compete_atomically_for_same_lease(database, clock):
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return acquire_lease(database, "concurrent", 30, clock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sum(value is not None for value in results) == 1


def test_runtime_constructor_does_not_connect_to_database(settings):
    def unavailable():
        raise AssertionError("A construção não deve abrir uma sessão")

    runtime = MonitoringRuntime(unavailable, settings)
    assert runtime.status()["running"] is False
    assert runtime.start()["threads_blocked"] is True
    assert runtime.resume()["running"] is False
    runtime.shutdown()


def test_manual_monitoring_returns_detached_history_and_records_success(database, settings, clock):
    product_id = product(database)
    runtime = MonitoringRuntime(database, settings, clock=clock)
    history = runtime.run_product(product_id)
    assert history.product_id == product_id
    assert history.current_price == Decimal("99.90")
    assert history.previous_price is None
    assert history.source == "simulated"
    assert history.is_simulated is True
    assert history.observed_at == clock()
    with database() as session:
        job = session.scalar(select(MonitoringJob))
        assert job.status == MonitoringJobStatus.SUCCEEDED
        assert job.last_run_at == clock()
        assert job.next_run_at == clock() + timedelta(seconds=300)
        assert job.failure_count == 0 and job.last_error is None
        assert session.get(MonitoringLease, f"monitor:product:{product_id}") is None


def test_due_intervals_priority_order_and_disabled_products(database, settings, clock):
    general = product(database)
    priority = product(database, priority=True)
    product(database, enabled=False)
    called = []

    def provider(row):
        called.append(row.id)
        return SimulatedPriceProvider(clock=clock)

    runtime = MonitoringRuntime(database, settings, provider, clock)
    assert runtime.run_due()["succeeded"] == 2
    assert called == [priority, general]
    assert runtime.run_due()["attempted"] == 0
    clock.advance(59)
    assert runtime.run_due()["attempted"] == 0
    clock.advance(1)
    assert runtime.run_due()["succeeded"] == 1
    assert called == [priority, general, priority]
    clock.advance(240)
    assert runtime.run_due()["succeeded"] == 2


def test_separate_runtimes_recheck_durable_due_time(database, settings, clock):
    product(database)
    first = MonitoringRuntime(database, settings, clock=clock)
    second = MonitoringRuntime(database, settings, clock=clock)
    assert first.run_due()["succeeded"] == 1
    assert second.run_due()["attempted"] == 0
    assert history_count(database) == 1


def test_provider_rate_limit_is_shared_and_defers_without_sleep(database, settings, clock):
    settings = settings.model_copy(update={"provider_min_interval_seconds": Decimal("30")})
    first_id = product(database)
    second_id = product(database)
    runtime = MonitoringRuntime(database, settings, clock=clock)
    other = MonitoringRuntime(database, settings, clock=clock)
    runtime.run_product(first_id)
    with pytest.raises(ProviderRateLimitedError) as error:
        other.run_product(second_id)
    assert error.value.retry_at == clock() + timedelta(seconds=30)
    assert history_count(database) == 1
    with database() as session:
        job = session.scalar(select(MonitoringJob).where(MonitoringJob.product_id == second_id))
        assert job.last_error == "ProviderRateLimitedError"
        assert job.failure_count == 0
        assert job.next_run_at == clock() + timedelta(seconds=30)
    clock.advance(29)
    assert other.run_due()["attempted"] == 0
    clock.advance(1)
    assert other.run_due()["succeeded"] == 1
    assert history_count(database) == 2


def test_rate_limit_is_independent_for_each_platform(database, settings, clock):
    settings = settings.model_copy(update={"provider_min_interval_seconds": Decimal("30")})
    first_id = product(database)
    second_id = product(database, platform=Platform.SHOPEE)
    runtime = MonitoringRuntime(database, settings, clock=clock)
    runtime.run_product(first_id)
    runtime.run_product(second_id)
    assert history_count(database) == 2


def test_manual_rate_limit_does_not_shorten_next_automatic_run(database, settings, clock):
    settings = settings.model_copy(update={"provider_min_interval_seconds": Decimal("30")})
    product_id = product(database)
    runtime = MonitoringRuntime(database, settings, clock=clock)
    runtime.run_product(product_id)
    with pytest.raises(ProviderRateLimitedError):
        runtime.run_product(product_id)
    with database() as session:
        job = session.scalar(select(MonitoringJob))
        assert job.next_run_at == clock() + timedelta(seconds=300)


def test_expired_worker_cannot_commit_price_or_offer(database, settings, clock):
    product_id = product(database)
    observed_at = clock()

    class SlowProvider:
        def fetch(self, row):
            snapshot = SimulatedPriceProvider(clock=lambda: observed_at).fetch(row)
            clock.advance(settings.monitoring_lease_seconds + 1)
            return snapshot

    runtime = MonitoringRuntime(database, settings, lambda _: SlowProvider(), clock)
    with pytest.raises(MonitoringBusyError):
        runtime.run_product(product_id)
    assert history_count(database) == 0
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Offer)) == 0


def test_parallel_product_calls_only_fetch_once(database, settings, clock):
    product_id = product(database)
    entered = Event()
    finish = Event()

    class BlockingProvider:
        def fetch(self, row):
            entered.set()
            assert finish.wait(timeout=5)
            return SimulatedPriceProvider(clock=clock).fetch(row)

    first = MonitoringRuntime(database, settings, lambda _: BlockingProvider(), clock)
    second = MonitoringRuntime(database, settings, clock=clock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(first.run_product, product_id)
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(MonitoringBusyError):
                second.run_product(product_id)
        finally:
            finish.set()
        assert pending.result(timeout=5).product_id == product_id
    assert history_count(database) == 1


def test_lease_takeover_fences_original_worker(database, settings, clock):
    product_id = product(database)
    entered = Event()
    finish = Event()
    original_time = clock()

    class BlockingProvider:
        def fetch(self, row):
            entered.set()
            assert finish.wait(timeout=5)
            return SimulatedPriceProvider(clock=lambda: original_time).fetch(row)

    first = MonitoringRuntime(database, settings, lambda _: BlockingProvider(), clock)
    second = MonitoringRuntime(database, settings, clock=clock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(first.run_product, product_id)
        try:
            assert entered.wait(timeout=5)
            clock.advance(settings.monitoring_lease_seconds + 1)
            second.run_product(product_id)
        finally:
            finish.set()
        with pytest.raises(MonitoringBusyError):
            pending.result(timeout=5)
    assert history_count(database) == 1
    with database() as session:
        assert session.scalar(select(MonitoringJob)).status == MonitoringJobStatus.SUCCEEDED


def test_failure_backoff_is_bounded_and_error_messages_are_sanitized(database, settings, clock):
    product_id = product(database)
    settings = settings.model_copy(
        update={"general_interval_seconds": 10, "monitoring_backoff_max_seconds": 60}
    )

    class BrokenProvider:
        def fetch(self, _):
            raise TimeoutError("https://private?token=do-not-persist")

    runtime = MonitoringRuntime(database, settings, lambda _: BrokenProvider(), clock)
    for number, delay in enumerate([10, 20, 40, 60, 60], start=1):
        assert runtime.run_due()["failed"] == 1
        with database() as session:
            job = session.scalar(
                select(MonitoringJob).where(MonitoringJob.product_id == product_id)
            )
            assert job.failure_count == number
            assert job.last_error == "TimeoutError"
            assert job.next_run_at == clock() + timedelta(seconds=delay)
        assert runtime.run_due()["attempted"] == 0
        clock.advance(delay)
    assert history_count(database) == 0


def test_runtime_reloads_pause_mock_and_intervals_from_database(database, settings, clock):
    product_id = product(database)
    runtime = MonitoringRuntime(database, settings, clock=clock)
    with database() as session:
        save_monitoring_configuration(
            session,
            settings,
            {
                "scheduler_enabled": False,
                "mock_current_price": "42.50",
                "mock_previous_price": "50.00",
            },
        )
        session.commit()
    runtime.pause()
    assert runtime.status()["enabled"] is False
    assert runtime.run_due()["attempted"] == 0
    manual = runtime.run_product(product_id)
    assert manual.current_price == Decimal("42.50")
    assert manual.previous_price == Decimal("50.00")
    with database() as session:
        save_monitoring_configuration(
            session, settings, {"scheduler_enabled": True, "general_interval_seconds": 5}
        )
        session.commit()
    clock.advance(5)
    # Another process persisted resume: no local resume call is required here.
    assert runtime.run_due()["succeeded"] == 1
    assert runtime.status()["paused"] is False


def test_tick_database_failure_uses_backoff_and_sanitized_status(settings, clock, monkeypatch):
    runtime = MonitoringRuntime(lambda: None, settings, clock=clock)
    calls = []

    def unavailable():
        calls.append(clock())
        raise RuntimeError("secret connection/password")

    monkeypatch.setattr(runtime, "run_due", unavailable)
    runtime._tick()
    assert runtime.status()["last_error"] == "RuntimeError"
    assert runtime.status()["next_tick_at"] == clock() + timedelta(seconds=1)
    runtime._tick()
    assert len(calls) == 1
    clock.advance(1)
    runtime._tick()
    assert runtime.status()["next_tick_at"] == clock() + timedelta(seconds=2)
    clock.advance(1)
    runtime._tick()
    assert len(calls) == 2
    monkeypatch.setattr(runtime, "run_due", lambda: {})
    clock.advance(1)
    runtime._tick()
    assert runtime.status()["last_error"] is None


def test_test_environment_and_pytest_always_block_scheduler_threads(settings, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Uma thread de scheduler não deve ser iniciada em testes")

    monkeypatch.setattr(runtime_module, "BackgroundScheduler", forbidden)
    for app_env in ("test", "development"):
        configured = settings.model_copy(update={"app_env": app_env})
        runtime = MonitoringRuntime(lambda: None, configured)
        assert runtime.start()["running"] is False
        assert runtime.resume()["running"] is False
        assert runtime.status()["threads_blocked"] is True


def test_scheduler_uses_single_coalesced_tick_and_safe_lifecycle(settings, monkeypatch):
    created = []

    class FakeScheduler:
        def __init__(self, **kwargs):
            self.running = False
            self.jobs = []
            self.shutdown_wait = None
            created.append(self)

        def add_job(self, callback, trigger, **kwargs):
            self.jobs.append((callback, trigger, kwargs))

        def start(self):
            self.running = True

        def resume(self):
            self.running = True

        def shutdown(self, *, wait):
            self.shutdown_wait = wait
            self.running = False

    monkeypatch.setattr(runtime_module, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(runtime_module, "_threads_blocked", lambda _: False)
    runtime = MonitoringRuntime(lambda: None, settings)
    assert runtime.start()["running"] is True
    assert runtime.start()["running"] is True
    assert len(created) == 1
    _, trigger, options = created[0].jobs[0]
    assert trigger == "interval"
    assert options["seconds"] == 1
    assert options["max_instances"] == 1
    assert options["coalesce"] is True
    assert runtime.pause()["running"] is False
    assert runtime.resume()["running"] is True
    assert runtime.shutdown()["running"] is False
    assert created[0].shutdown_wait is True


def test_missing_or_disabled_product_never_creates_history(database, settings, clock):
    runtime = MonitoringRuntime(database, settings, clock=clock)
    with pytest.raises(LookupError):
        runtime.run_product(999)
    disabled_id = product(database, enabled=False)
    with pytest.raises(ValueError):
        runtime.run_product(disabled_id)
    assert history_count(database) == 0
