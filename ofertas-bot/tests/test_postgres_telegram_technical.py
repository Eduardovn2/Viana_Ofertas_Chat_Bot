"""Technical outbox on real PostgreSQL, isolated *_test schemas, Telegram mocked."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import test_postgres_categories as pg
import test_telegram_technical as technical
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models import Base, PublicationDestination
from app.models import TelegramTechnicalAudit as Audit
from app.models import TelegramTechnicalPublication as Technical

categories_postgres = pg.categories_postgres
no_live_telegram = technical.no_live_telegram
pytestmark = pytest.mark.postgres


def test_technical_postgres_schema_constraints_and_non_destructive_downgrade(categories_postgres):
    engine, _ = categories_postgres
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, Base.metadata) == []
    inspector = inspect(engine)
    columns = {
        column["name"] for column in inspector.get_columns("telegram_technical_publications")
    }
    assert not {"product_id", "offer_id", "price", "coupon_id", "affiliate_url"} & columns
    assert {"destination_id", "content_hash", "attempt_number"} <= columns
    assert ("destination_id", "content_hash") in {
        tuple(key["column_names"])
        for key in inspector.get_unique_constraints("telegram_technical_publications")
    }
    context = technical.setup_context(engine)
    technical.test_database_enforces_unique_content_single_attempt_and_approval(context)
    with context[0]() as session:
        destination = session.get(PublicationDestination, context[2])
        session.delete(destination)
        with pytest.raises(IntegrityError):
            session.commit()
    config = Config(str(pg.PROJECT_ROOT / "alembic.ini"))
    with pytest.raises(RuntimeError, match="without data loss"):
        command.downgrade(config, "0005_telegram_outbox")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0007_simulated_automation"
        )
        assert connection.scalar(select(func.count()).select_from(Technical)) == 1
        assert connection.scalar(select(func.count()).select_from(Audit)) == 1


def test_technical_postgres_success_exact_message_and_audit(categories_postgres):
    technical.test_success_exact_content_durable_claim_and_audit(
        technical.setup_context(categories_postgres[0])
    )


def test_technical_postgres_concurrent_dispatch_single_http_attempt(categories_postgres):
    technical.test_concurrent_dispatch_performs_one_publisher_call(
        technical.setup_context(categories_postgres[0])
    )


def test_technical_postgres_concurrent_previews_share_single_entry(categories_postgres):
    context = technical.setup_context(categories_postgres[0])
    barrier = Barrier(6)
    with technical.workflow(context) as (service, calls, _, _):

        def create():
            barrier.wait(10)
            return technical.preview(context, service)

        with ThreadPoolExecutor(max_workers=6) as workers:
            entries = list(workers.map(lambda _: create(), range(6)))
        assert len(set(entries)) == 1 and calls == []
    with context[0]() as session:
        assert session.scalar(select(func.count()).select_from(Technical)) == 1
        assert session.scalar(select(func.count()).select_from(Audit)) == 1


def test_technical_postgres_timeout_retains_reservation(categories_postgres):
    import httpx

    technical.test_failure_timeout_no_retry_no_new_preview(
        technical.setup_context(categories_postgres[0]),
        lambda r: httpx.Response(503),
        technical.State.UNCERTAIN,
    )


def test_technical_postgres_persistence_failure_after_http(categories_postgres, monkeypatch):
    technical.test_persistence_failure_after_http_reserves_uncertain(
        technical.setup_context(categories_postgres[0]), monkeypatch
    )


def test_technical_postgres_invalidated_review_keeps_audit(categories_postgres):
    context = technical.setup_context(categories_postgres[0])
    technical.test_mutation_invalidates_approval(context, "configuration")
    with context[0]() as session:
        assert session.scalars(select(Audit.event).order_by(Audit.id)).all() == [
            "PREVIEW_CREATED",
            "APPROVED",
            "REVIEW_INVALIDATED",
        ]


def test_technical_postgres_administrative_api_with_mocked_dispatch(categories_postgres):
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    _, url = categories_postgres
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=url.render_as_string(hide_password=False),
        admin_api_key=technical.api.KEY,
    )
    app = create_app(settings)
    try:
        with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
            technical.test_api_mocked_dispatch_requires_confirmed_hash_destination(app, client)
    finally:
        app.state.engine.dispose()
