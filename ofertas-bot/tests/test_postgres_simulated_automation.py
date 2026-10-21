"""Real PostgreSQL in owned UUID schemas, all provider/publisher IO simulated."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import test_postgres_categories as pg
import test_simulated_automation as simulation
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models import Base, ProductPriceHistory, TelegramSimulation
from app.services.simulation_safety import require_simulation_scope

categories_postgres = pg.categories_postgres
no_network = simulation.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = simulation.setup(engine, url.render_as_string(hide_password=False))
    yield ctx
    ctx["runtime"].shutdown()


def test_postgres_simulation_metadata_and_non_destructive_downgrade(categories_postgres, context):
    engine, _ = categories_postgres
    with engine.connect() as connection:
        migration = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(migration, Base.metadata) == []
    simulation.stage(context)
    with context["factory"]() as session:
        entry = session.scalar(select(TelegramSimulation))
        entry.status = "SENT"
        with pytest.raises(IntegrityError):
            session.commit()
    config = Config(str(pg.PROJECT_ROOT / "alembic.ini"))
    with pytest.raises(RuntimeError, match="without data loss"):
        command.downgrade(config, "0006_telegram_technical")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0007_simulated_automation"
        )
        assert connection.scalar(select(func.count()).select_from(ProductPriceHistory)) == 2


def test_postgres_full_simulated_scheduler_pipeline(context):
    simulation.test_full_scheduler_history_scoring_queue_mock_and_audit(context)


def test_postgres_simulation_capture_concurrency(context):
    simulation.stage(context)
    barrier = Barrier(6)

    def capture():
        barrier.wait(10)
        return context["pipeline"].capture()

    with ThreadPoolExecutor(max_workers=6) as workers:
        outcomes = list(workers.map(lambda _: capture(), range(6)))
    assert sum(result["enqueued"] for result in outcomes) == 0
    assert len(simulation.rows(context, TelegramSimulation)) == 1


def test_postgres_simulation_dispatch_concurrency(context, monkeypatch):
    simulation.test_concurrent_dispatch_claims_one_mock_request(context, monkeypatch)


def test_postgres_three_hour_simulated_republication(context):
    simulation.test_deduplicated_queue_and_three_hour_republication_with_fresh_history(context)


def test_postgres_timeout_keeps_simulated_reservation(context):
    simulation.test_mock_failure_timeout_no_retry_and_durable_audit(context, "timeout", "UNCERTAIN")


def test_postgres_simulation_result_persistence_failure(context, monkeypatch):
    simulation.test_persistence_failure_after_mock_is_uncertain(context, monkeypatch)


def test_postgres_simulation_requires_owned_schema_not_public(context):
    with context["factory"]() as session:
        session.execute(text("SET LOCAL search_path = public"))
        with pytest.raises(ValueError, match="isolado"):
            require_simulation_scope(session, context["settings"])


def test_postgres_reservation_returns_only_the_actual_owner(context):
    from app.services.leases import acquire_lease, release_lease

    factory, clock = context["factory"], lambda: context["clock"][0]
    owner = acquire_lease(factory, "simulation:lease-proof", 120, clock)
    assert owner is not None
    assert acquire_lease(factory, "simulation:lease-proof", 120, clock) is None
    assert release_lease(factory, "simulation:lease-proof", "not-the-owner") is False
    assert release_lease(factory, "simulation:lease-proof", owner) is True


def test_postgres_policy_edit_serializes_with_pre_dispatch_revalidation(context, monkeypatch):
    from threading import Event

    from app.models import PublicationDestination, SimulationPolicy
    from app.services.simulation_safety import configure_policy

    entry_id = simulation.stage(context)
    entered, release, updated = Event(), Event(), Event()
    pipeline = context["pipeline"]
    original = pipeline._context

    def held(*args):
        result = original(*args)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(pipeline, "_context", held)

    def update_policy():
        with context["factory"]() as session:
            configure_policy(
                session,
                context["settings"],
                session.get(PublicationDestination, context["destination"]),
                {"enabled": False},
            )
            session.commit()
            updated.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        dispatch = workers.submit(pipeline.dispatch, entry_id)
        assert entered.wait(10)
        edit = workers.submit(update_policy)
        try:
            assert not updated.wait(0.2)
        finally:
            release.set()
        assert dispatch.result(10) == "SIMULATED"
        edit.result(10)
    with context["factory"]() as session:
        assert session.get(SimulationPolicy, context["destination"]).enabled is False
    assert pipeline.delivery.calls == 1
