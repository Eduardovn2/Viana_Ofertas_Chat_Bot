"""Opt-in real PostgreSQL; only UUID schemas inside ofertas_bot_test, all HTTP denied."""

from pathlib import Path

import pytest
import test_offers as prices
import test_postgres_categories as pg
import test_shopee_post_audit as contracts
from alembic.config import Config
from sqlalchemy.orm import Session

from alembic import command
from app.models import SourceType

pytestmark = pytest.mark.postgres
categories_postgres = pg.categories_postgres
no_network = contracts.no_network
product = prices.product


@pytest.fixture
def session(categories_postgres):
    engine, _ = categories_postgres
    with Session(engine) as session:
        yield session


def test_postgres_evidence_trust_and_audit(session, product):
    contracts.test_separate_trust_dimensions_without_previous_sale_or_account_promotion(
        session, product
    )
    session.commit()


@pytest.mark.parametrize("sufficient", [True, False])
def test_postgres_commercial_evidence_preserves_quote(session, product, sufficient):
    contracts.test_commercial_attestation_never_relabels_automatic_quote(
        session, product, sufficient
    )
    session.commit()


@pytest.mark.parametrize(
    "source_type", [SourceType.MANUAL, SourceType.UNKNOWN, SourceType.OFFICIAL_API]
)
def test_postgres_source_type_isolation(session, product, source_type):
    contracts.test_public_comparison_never_mixes_source_types(session, product, source_type)


def test_postgres_carried_history_isolation(session, product):
    contracts.test_carried_reference_cannot_cross_source_type_or_price_basis(session, product)


def test_postgres_migration_current_check(categories_postgres):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.current(config)
    command.check(config)
