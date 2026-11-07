import copy
import json
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import test_auto_pilot as offline
import test_auto_pilot_real as real
import test_auto_pilot_worker as workers
import test_opportunity_ranking as scores
import test_shopee_multistore as multi
from sqlalchemy import func, select

from app.analyze_pilot_captures import analyze_saved_document
from app.models import Product, ProductPriceHistory
from app.services.comparison import PriceComparisonService

base_context = real.base_context
context = real.context
no_network = real.no_network
application = real.application
client = real.client


def test_real_capture_has_unknown_metrics_and_no_commercial_approval():
    path = Path(__file__).parent / "fixtures/shopee_multistore_real_sanitized.json"
    recorded = json.loads(path.read_text(encoding="utf-8"))
    result = analyze_saved_document(recorded, source_file=str(path), now=scores.NOW)
    assert len(result["analysis_queue"]) == 3
    for row in result["analysis_queue"]:
        ranking = row["intelligent_ranking"]
        assert ranking["coverage_percent"] == 0 and ranking["priority"] == "INSUFFICIENT_DATA"
        assert all(d["score"] is None for d in ranking["dimensions"].values())
        assert row["publication_state"] == "BLOCKED"


def test_ranking_survives_restart_and_budget_without_publishing_unknown_candidates(context):
    ctx = context
    plan = real.authorized(ctx, operating_policy={}, max_queries=1)
    offline.listings(ctx, [1], verified=False)
    first = ctx["pilot"].tick(ctx["pilot_id"])
    assert first["state"] == "ACTIVE_REAL"
    assert (
        first["opportunities"]["analysis_queue"][0]["intelligent_ranking"]["priority"]
        == "INSUFFICIENT_DATA"
    )
    with closing(workers.replacement(ctx)) as replacement:
        ctx["clock"][0] += timedelta(seconds=30)
        second = workers.worker(ctx, plan, replacement).step()
        assert (
            second["opportunities"]["analysis_queue"][0]["intelligent_ranking"]
            == first["opportunities"]["analysis_queue"][0]["intelligent_ranking"]
        )
    assert ctx["calls"] == 1 and not ctx["telegram_calls"]


def test_proven_comparison_automatically_supplies_price_dimension(context):
    ctx = context
    ctx["nodes"] = [multi.node(101, 201, "80.00"), multi.node(102, 202, "100.00")]
    page = multi.page(ctx)
    comparison = multi.compare_offers(page, multi.QUERY, evidence=multi.proofs(page.items))
    from app.integrations.contracts import fingerprint
    from app.services.shopee_opportunities import pilot_opportunities

    comparison = json.loads(json.dumps(comparison, default=str))
    result = pilot_opportunities(
        [dict(capture_id="test", comparison=comparison, comparison_sha256=fingerprint(comparison))],
        now=ctx["clock"][0],
    )
    assert (
        result["analysis_queue"][0]["intelligent_ranking"]["dimensions"]["price"]["status"]
        == "VERIFIED"
    )
    assert (
        result["analysis_queue"][0]["intelligent_ranking"]["dimensions"]["seller"]["status"]
        == "UNKNOWN"
    )


def test_admin_ranking_and_documentary_preview_are_read_only(context, application, client):
    ctx = context
    real.authorized(ctx, operating_policy={}, max_queries=1)
    offline.listings(ctx, [1], verified=False)
    ctx["pilot"].tick(ctx["pilot_id"])
    application.state.auto_pilot_real = ctx["pilot"]
    from test_shopee_collection import api

    headers = api.HEADERS
    path = "/api/v1/auto-pilots-real/" + ctx["pilot_id"] + "/ranking"
    assert client.get(path).status_code == 401
    for key in ("score", "price", "coverage", "opportunity"):
        response = client.get(path, params={"sort_by": key}, headers=headers)
        assert response.status_code == 200 and len(response.json()["candidates"]) == 1
    data = dict(
        actor="synthetic_reviewer",
        confirmed_documented_sources=True,
        signals_by_identity={
            "101:201": {
                "reviews": scores.metric(
                    "PRODUCT_RATING", 5, observed_at=ctx["clock"][0].isoformat()
                )
            }
        },
    )
    response = client.post(path + "/preview", json=data, headers=headers)
    assert response.status_code == 200
    assert response.json()["persisted"] is False
    assert (
        response.json()["candidates"][0]["intelligent_ranking"]["dimensions"]["reviews"]["score"]
        == 20
    )
    data["confirmed_documented_sources"] = False
    assert client.post(path + "/preview", json=data, headers=headers).status_code == 422
    assert not ctx["telegram_calls"]


def test_radar_reuses_real_same_context_history_without_writing_scores(context):
    ctx = context
    offline.listings(ctx, [1])
    with ctx["factory"]() as session:
        product = session.scalar(select(Product).where(Product.external_id == "101:201"))
        current = session.scalar(
            select(ProductPriceHistory).where(ProductPriceHistory.product_id == product.id)
        )
        meta = copy.deepcopy(current.evidence_metadata)
        meta["price_basis"] = "SYNTHETIC_DOCUMENTED_SAME_PRICE_CONTEXT"
        current.evidence_metadata = meta
        values = {
            c.name: copy.deepcopy(getattr(current, c.name))
            for c in ProductPriceHistory.__table__.columns
            if c.name != "id"
        }
        values.update(
            current_price=scores.Decimal("120.00"),
            observed_at=ctx["clock"][0] - timedelta(days=1),
            raw_fingerprint="c" * 64,
        )
        old = ProductPriceHistory(**values)
        session.add(old)
        session.flush()
        before = session.scalar(select(func.count()).select_from(ProductPriceHistory))
        result = PriceComparisonService().rank_observation(session, current, now=ctx["clock"][0])
        assert result["ranking"]["dimensions"]["price"]["status"] == "VERIFIED"
        assert (
            result["ranking"]["dimensions"]["price"]["evidence"][0]["kind"]
            == "SAME_LISTING_HISTORY"
        )
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == before
        assert result["history_writes"] == 0 and not result["publication_authorized"]
