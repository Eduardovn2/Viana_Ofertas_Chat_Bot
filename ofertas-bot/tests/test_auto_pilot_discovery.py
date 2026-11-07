import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
import test_shopee_multistore as multi
from sqlalchemy import func, select

from app.analyze_pilot_captures import analyze_saved_document
from app.integrations.contracts import fingerprint
from app.models import AppSetting, ProductPriceHistory
from app.services.auto_pilot import PREFIX
from app.services.shopee_multistore import compare_offers
from app.services.shopee_opportunities import pilot_opportunities

base_context = real.base_context
context = real.context
no_network = real.no_network
FIXTURE = Path(__file__).parent / "fixtures/shopee_multistore_real_sanitized.json"


def test_discovery_without_manual_offers_persists_and_classifies_before_completion(context):
    ctx = context
    real.authorized(ctx, max_queries=1)
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ctx["nodes"] = [
        multi.node(int(r["shop_id"]), int(r["item_id"]), r["announced_price"], r["name"])
        for r in recorded["candidates"]
    ]
    with ctx["factory"]() as session:
        before = session.scalar(select(func.count()).select_from(ProductPriceHistory))
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["reason"] == "QUERY_BUDGET"
    capture = result["queries"][0]["capture"]
    assert capture["execution_environment"] == "ISOLATED_MOCK"
    assert capture["comparison_sha256"] == fingerprint(capture["comparison"])
    rows = result["opportunities"]["analysis_queue"]
    assert len(rows) == 3 and not result["queue"]
    assert all(r["name"] and r["announced_price"] and r["canonical_url"] for r in rows)
    assert all(r["observed_at"] and r["source"] == "shopee:official_api" for r in rows)
    assert all(r["publication_state"] == "BLOCKED" for r in rows)
    assert result["opportunities"]["classification_state"] == "COMPLETED"
    assert not result["opportunities"]["proven_comparisons"]
    with ctx["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == before
        stored = session.scalar(
            select(AppSetting.value).where(AppSetting.key == PREFIX + ctx["pilot_id"])
        )
        assert stored["queries"][0]["capture"] == capture
        saved = fingerprint(stored)
    ctx["pilot"].tick(ctx["pilot_id"])
    with ctx["factory"]() as session:
        assert (
            fingerprint(
                session.scalar(
                    select(AppSetting.value).where(AppSetting.key == PREFIX + ctx["pilot_id"])
                )
            )
            == saved
        )
    assert ctx["calls"] == 1 and not ctx["telegram_calls"]


def test_query_exhaustion_drains_verified_queue_without_another_query(context, monkeypatch):
    ctx = context
    plan = real.authorized(ctx, max_queries=1)
    offline.listings(ctx, [1])
    original = ctx["pilot"].dispatch_batch
    monkeypatch.setattr(
        ctx["pilot"], "dispatch_batch", lambda pilot_id: ctx["pilot"].report(pilot_id)
    )
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["state"] == "ACTIVE_REAL" and len(result["queue"]) == 1
    assert result["discovery_state"] == "QUERY_BUDGET_EXHAUSTED"
    assert result["opportunities"]["classification_state"] == "COMPLETED"
    monkeypatch.setattr(ctx["pilot"], "dispatch_batch", original)
    ctx["clock"][0] += timedelta(seconds=30)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["counts"]["published"] == 1 and result["state"] == "COMPLETED"
    assert result["opportunities"]["analysis_queue"][0]["publication_state"] == "SENT"
    assert ctx["calls"] == 1 and len(ctx["telegram_calls"]) == 1
    assert result["scope_sha256"] == plan["scope_sha256"]


def test_late_response_is_preserved_without_publication(context, monkeypatch):
    ctx = context
    real.authorized(ctx, max_queries=1)
    offline.listings(ctx, [1], verified=False)
    original = ctx["pilot"]._discover_analysis

    def late(*args):
        analysis = original(*args)
        ctx["clock"][0] += timedelta(hours=2)
        return analysis

    monkeypatch.setattr(ctx["pilot"], "_discover_analysis", late)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["reason"] == "DEADLINE"
    assert len(result["queries"][0]["capture"]["comparison"]["candidates"]) == 1
    assert result["opportunities"]["captured_listings"] == 1
    assert not ctx["telegram_calls"]


def test_documented_statistics_deduplicate_stores_and_keep_decimal(context):
    ctx = context
    ctx["nodes"] = [
        multi.node(101, 201, "100.00"),
        multi.node(102, 202, "200.00"),
        multi.node(101, 203, "150.00"),
    ]
    # Use the existing isolated discovery connector, without any pilot HTTP permit.
    page = multi.page(ctx)
    result = compare_offers(page, multi.QUERY, evidence=multi.proofs(page.items))
    group = result["proven_comparisons"][0]
    assert group["price_statistics"]["sample_size"] == 2
    assert group["price_statistics"]["mean"] == Decimal("150.00")
    assert group["price_statistics"]["median"] == Decimal("150.00")
    assert group["ranking"][0]["below_group_mean_percent"] == Decimal("33.33")
    assert "NOT_HISTORICAL" in group["price_statistics"]["semantics"]


def test_same_titles_without_proof_have_no_mean_or_winner(context):
    ctx = context
    page = multi.page(ctx)
    result = compare_offers(page, multi.QUERY)
    assert not result["proven_comparisons"] and not result["groups"]


@pytest.mark.parametrize("case", ["tie", "different_quantity", "ambiguous_price"])
def test_group_statistics_require_matching_facts_and_unambiguous_prices(context, case):
    ctx = context
    ctx["nodes"] = [multi.node(101, 201, "100.00"), multi.node(102, 202, "100.00")]
    if case == "ambiguous_price":
        ctx["nodes"][1]["priceMax"] = "200.00"
    page = multi.page(ctx)
    proofs = multi.proofs(page.items)
    if case == "different_quantity":
        proofs["102:202"]["facts"]["quantity"] = 2
    result = compare_offers(page, multi.QUERY, evidence=proofs)
    if case == "tie":
        group = result["proven_comparisons"][0]
        assert (
            group["price_statistics"]["mean"]
            == group["price_statistics"]["median"]
            == Decimal("100.00")
        )
        assert all(r["below_group_mean_percent"] == 0 for r in group["ranking"])
    else:
        assert not result["proven_comparisons"]
        assert all(g["price_statistics"] is None for g in result["groups"])


def test_preserved_real_capture_is_stale_and_never_fills_missing_new_values():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    before = fingerprint(doc)
    result = analyze_saved_document(
        doc, source_file=str(FIXTURE), now=datetime(2026, 11, 3, tzinfo=UTC)
    )
    assert len(result["analysis_queue"]) == 3 and not result["proven_comparisons"]
    assert all(
        "CURRENT_PRICE_REVALIDATION_REQUIRED" in r["publication_blockers"]
        for r in result["analysis_queue"]
    )
    assert {r["announced_price"] for r in result["analysis_queue"]} == {"639.00", "1399.00"}
    assert fingerprint(doc) == before
    missing = analyze_saved_document(
        {"report": {"counts": {"found": 9}}}, source_file="old-pilot", now=datetime.now(UTC)
    )
    assert missing["unavailable_listing_details"] == 9 and not missing["analysis_queue"]


def test_capture_tampering_blocks_local_classification():
    with pytest.raises(ValueError, match="alterada"):
        pilot_opportunities(
            [{"comparison": {}, "comparison_sha256": "invalid"}], now=datetime.now(UTC)
        )
