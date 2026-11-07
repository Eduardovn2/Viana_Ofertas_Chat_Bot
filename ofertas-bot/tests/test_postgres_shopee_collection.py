"""Controlled collection in owned test schemas; HTTP is always mock transport."""

import pytest
import test_postgres_categories as pg
import test_shopee_collection as contracts
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from alembic import command
from app.models import Base

categories_postgres = pg.categories_postgres
no_network = contracts.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = contracts.setup(engine, url.render_as_string(hide_password=False))
    yield ctx
    ctx["transport"].close()


def test_postgres_collection_schema_and_lossless_downgrade(context):
    with context["engine"].connect() as connection:
        migration = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(migration, Base.metadata) == []
    contracts.execute(context)
    with pytest.raises(RuntimeError, match="without data loss"):
        command.downgrade(Config(str(pg.PROJECT_ROOT / "alembic.ini")), "0007_simulated_automation")


def test_postgres_collection_ingestion(context):
    contracts.test_observed_ingestion_initial_reference_provenance_and_no_outbox(context)


def test_postgres_collection_quote_change(context):
    contracts.test_informational_changes_never_become_verified_discounts(
        context, "55", "DROP", "-6.78"
    )


def test_postgres_collection_duplicates(context):
    contracts.test_duplicate_run_and_cached_observation_do_not_duplicate_history(context)


def test_postgres_collection_concurrent_same_run(context):
    contracts.test_concurrent_same_run_has_one_http_attempt(context)


def test_postgres_collection_concurrent_runs(context):
    contracts.test_concurrent_different_runs_are_fenced(context)


def test_postgres_collection_failure_and_revalidation(context):
    contracts.test_bad_price_preserves_last_history_and_revalidation_expires_older_candidate(
        context
    )


def test_postgres_collection_manual_category_correction(context):
    contracts.test_manual_categories_and_product_filter_remain_authoritative(context)
