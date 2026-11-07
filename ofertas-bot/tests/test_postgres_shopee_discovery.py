"""Real PostgreSQL, owned UUID schemas in ofertas_bot_test; all HTTP is mocked."""

import pytest
import test_postgres_categories as pg
import test_shopee_discovery as contracts
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


def test_postgres_schema_and_guarded_downgrade(context):
    with context["engine"].connect() as connection:
        assert (
            compare_metadata(
                MigrationContext.configure(
                    connection, opts={"compare_type": True, "compare_server_default": True}
                ),
                Base.metadata,
            )
            == []
        )
    contracts.configure(context)
    with pytest.raises(RuntimeError, match="without data loss"):
        command.downgrade(Config(str(pg.PROJECT_ROOT / "alembic.ini")), "0008_shopee_collection")


def test_postgres_nine_category_rotation(context):
    contracts.test_fair_rotation_and_terms(context)


def test_postgres_bounded_pages_and_duplicates(context):
    contracts.test_pagination_dedup_and_initial(context)


def test_postgres_single_claim_concurrency(context):
    contracts.test_concurrency_same_key_or_other_run(context)


def test_postgres_product_lease(context):
    contracts.test_product_lease_blocks_monitoring(context)


def test_postgres_partial_failure_recovery(context):
    contracts.test_page_two_failure_preserves_page_one_and_cursor(context)


def test_postgres_history_decimal_and_unknown_evidence(context):
    contracts.test_history_comparable_quotes_not_verified_discounts(
        context, "90.00", "DROP", "-10.00"
    )


def test_postgres_manual_decision_preserved(context):
    contracts.test_manual_decision_audited_and_does_not_bypass_accessory_filter(context)


def test_postgres_mock_only_approval_and_pending_queue(context, monkeypatch):
    contracts.test_dry_run_template_queue_dedup_no_approval(context, monkeypatch)


def test_postgres_republication_requires_three_hours(context, monkeypatch):
    contracts.test_cooldown_requires_three_hours_and_new_observation(context, monkeypatch)


def test_postgres_recover_without_repeating_request(context):
    contracts.test_recover_abandoned_never_retries(context)
