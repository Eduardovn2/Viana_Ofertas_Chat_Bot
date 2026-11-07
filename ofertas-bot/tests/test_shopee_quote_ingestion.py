"""Synthetic audit evidence only; no real API or operational database."""

import copy

import pytest
import test_shopee_collection as collection
import test_shopee_single_read as single
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.models import AppSetting, ProductPriceHistory, ShopeeCollectionRun, SourceType
from app.services.comparison import PriceComparisonService
from app.services.shopee_quote_ingestion import ingest_historical_quote, prepare_historical_quote

base_context = collection.context
no_network = collection.no_network


def setup(ctx):
    single.setup(ctx)
    plan = single.prepare(ctx)
    single.approve(ctx, plan)
    response = single.execute(ctx, plan)
    quote = response["quote"]
    quote["mode"] = "REAL_SINGLE_READ"
    # Deliberately synthetic persisted provenance in an isolated test database.
    with ctx["factory"]() as session:
        row = session.scalar(select(AppSetting))
        value = copy.deepcopy(row.value)
        value["mode"] = value["scope"]["mode"] = "REAL_SINGLE_READ"
        value["scope_sha256"] = fingerprint(value["scope"])
        value["approval"]["scope_sha256"] = value["scope_sha256"]
        value["result"]["mode"] = "REAL_SINGLE_READ"
        value["result"]["quote_sha256"] = fingerprint(quote)
        row.value = value
        run = session.get(ShopeeCollectionRun, plan["authorization_id"])
        run.mode = "REAL"
        run.result = value["result"]
        run.specification = {**run.specification, "scope_sha256": value["scope_sha256"]}
        session.commit()
    ctx["envelope"] = {
        "authorization_id": plan["authorization_id"],
        "status": "SUCCEEDED",
        "http_attempts": 1,
        "http_status": 200,
        "quote": quote,
    }
    return ctx


@pytest.fixture
def context(base_context):
    return setup(base_context)


def ingest(ctx):
    with ctx["factory"]() as session:
        row = ingest_historical_quote(
            session, ctx["settings"], ctx["envelope"], ctx["product_id"], offline=True
        )
        session.commit()
        return row


def test_initial_official_provenance_and_idempotence(context):
    row = ingest(context)
    assert row.current_price == single.observation.Decimal("99.90")
    assert row.observed_at.isoformat() == context["envelope"]["quote"]["observed_at"]
    assert row.source_type == SourceType.OFFICIAL_API
    assert row.availability.value == row.price_access.value == "UNKNOWN"
    assert row.evidence_metadata["commercial_verified"] is False
    assert row.evidence_metadata["price_basis"].startswith("v1:")
    assert ingest(context).id == row.id
    with context["factory"]() as session:
        row = session.get(ProductPriceHistory, row.id)
        assert PriceComparisonService().reported_quotes(session, row).direction == "INITIAL"
        assert (
            PriceComparisonService().compare(session, row, now=context["clock"][0]).direction
            == "EXCLUDED"
        )
    assert collection.count(context, ProductPriceHistory) == 1
    single.observation.assert_no_commerce(context)


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", "1.00"),
        ("observed_at", "2026-10-22T00:00:00+00:00"),
        ("identity", "999:888"),
        ("mode", "MOCK_ONLY"),
    ],
)
def test_tampered_quote_rejected(context, field, value):
    context["envelope"]["quote"][field] = value
    with context["factory"]() as session, pytest.raises(ValueError):
        prepare_historical_quote(session, context["envelope"], context["product_id"])
    assert collection.count(context, ProductPriceHistory) == 0


def test_manual_baseline_and_documentary_records_preserved(context):
    row = ingest(context)
    with context["factory"]() as session:
        row = session.get(ProductPriceHistory, row.id)
        row.source_type = SourceType.MANUAL
        row.raw_fingerprint = None
        session.commit()
    official = ingest(context)
    with context["factory"]() as session:
        assert PriceComparisonService().reported_quotes(session, official).direction == "INITIAL"
        assert session.get(ProductPriceHistory, row.id).source_type == SourceType.MANUAL


def test_operational_ingestion_unconditionally_blocked(context):
    with context["factory"]() as session, pytest.raises(ValueError, match="operacional bloqueada"):
        ingest_historical_quote(
            session, context["settings"], context["envelope"], context["product_id"]
        )
    assert collection.count(context, ProductPriceHistory) == 0
