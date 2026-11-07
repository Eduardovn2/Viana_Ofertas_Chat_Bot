"""Documented cached semantics and offline official-data ingestion."""

import csv
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
import test_auto_pilot_worker as workers
from sqlalchemy import func, select

from app.analyze_pilot_captures import analyze_saved_document
from app.models import AppSetting, Offer, Product, ProductPriceHistory
from app.services.opportunity_ranking import rank_rows
from app.services.ranking_signal_import import PREFIX, import_ranking_signals, load_ranking_signals
from app.services.shopee_indicators import indicator_contract

base_context = real.base_context
context = real.context
no_network = real.no_network
application = real.application
client = real.client
FIXTURE = Path(__file__).parent / "fixtures/shopee_multistore_real_sanitized.json"


def document(now, *, price_rating="4.8", sales="1000", **changes):
    row = dict(
        shop_id="101",
        item_id="201",
        observed_at=now.isoformat(),
        product_rating=price_rating,
        product_review_count="100",
        sales=sales,
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    content = output.getvalue()
    return (
        dict(
            csv_content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            columns={key: key for key in row},
            source_url="https://affiliate.shopee.com.br/",
            source_reference="FICTICIO exportação oficial para teste isolado",
            column_semantics_reference="FICTICIO dicionário avaliado em fixture",
            actor="synthetic_reviewer",
            exported_at=now.isoformat(),
            sales_semantics="WINDOW",
            sales_period_seconds=86400,
            rating_scale_max=5,
            confirmed_official_document=True,
            confirmed_column_semantics=True,
            confirmed_import=True,
        )
        | changes
    )


def test_real_indicators_are_available_without_invented_samples_periods_or_scores():
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    observed = datetime.fromisoformat(recorded["candidates"][0]["observed_at"])
    historic = analyze_saved_document(recorded, source_file=str(FIXTURE), now=observed)
    assert len(historic["analysis_queue"]) == 3
    assert {
        r["product_indicators"]["fields"]["ratingStar"] for r in historic["analysis_queue"]
    } == {"0", "4.9"}
    assert {r["product_indicators"]["fields"]["sales"] for r in historic["analysis_queue"]} == {
        0,
        2,
        7,
    }
    for row in historic["analysis_queue"]:
        score = row["intelligent_ranking"]
        assert score["coverage_percent"] == Decimal("17.5")
        assert score["scorable_coverage_percent"] == score["known_points"] == 0
        assert (
            score["dimensions"]["reviews"]["status"]
            == score["dimensions"]["popularity"]["status"]
            == "PARTIAL"
        )
        assert score["dimensions"]["reviews"]["score"] is None
        assert score["dimensions"]["popularity"]["score"] is None
        assert score["dimensions"]["seller"]["status"] == "UNKNOWN"
        assert row["publication_state"] == "BLOCKED"
        assert row["product_indicators"]["observed_at"] == row["observed_at"]
    current = analyze_saved_document(
        recorded, source_file=str(FIXTURE), now=observed + timedelta(days=3)
    )
    assert all(r["intelligent_ranking"]["coverage_percent"] == 0 for r in current["analysis_queue"])


def test_cached_description_proves_product_subject_but_not_stock_or_rating_scale():
    contract, _ = indicator_contract()
    fields = {f["name"]: f for f in contract["types"]["ProductOfferV2"]["fields"]}
    assert fields["ratingStar"]["description"] == "product rating star"
    assert fields["sales"]["description"] == "Sales count for this product"
    assert not {"stock", "availability", "reviewCount", "sku"} & fields.keys()
    assert any(o["name"] == "shopOfferV2" for o in contract["operations"])


def test_new_optional_fields_do_not_change_legacy_observation_binding():
    from app.integrations.contracts import fingerprint
    from app.integrations.shopee import ShopeeConnector
    from app.services.shopee_replay import SanitizedListing

    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = recorded["candidates"][0]
    row = SanitizedListing.model_validate(raw)
    previous_binding = row.model_dump(mode="json", exclude={"image_url", "provider_observation"})
    seller = raw["seller"]
    previous_binding["seller"] = ShopeeConnector._seller_information(
        dict(
            shopName=seller["shop_name"],
            ratingStar=seller["rating_star"],
            sales=seller["sales"],
            shopType=seller["shop_types"],
        )
    )
    result = analyze_saved_document(
        recorded, source_file=str(FIXTURE), now=datetime.fromisoformat(raw["observed_at"])
    )
    matched = next(r for r in result["analysis_queue"] if r["identity"] == raw["identity"])
    assert matched["observation_fingerprint"] == fingerprint(previous_binding)


def test_import_previews_without_product_registration_and_never_creates_offers(context):
    ctx = context
    with ctx["factory"]() as session:
        before = tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Product, Offer, ProductPriceHistory, AppSetting)
        )
        result = import_ranking_signals(session, document(ctx["clock"][0]), now=ctx["clock"][0])
        assert result["observations"] == 1 and len(result["signals"]) == 2
        assert not result["persisted"] and not result["creates_products"]
        assert before == tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Product, Offer, ProductPriceHistory, AppSetting)
        )


def test_import_is_idempotent_and_source_values_feed_points(context):
    ctx = context
    with ctx["factory"]() as session:
        data = document(ctx["clock"][0])
        first = import_ranking_signals(session, data, now=ctx["clock"][0], persist=True)
        second = import_ranking_signals(session, data, now=ctx["clock"][0], persist=True)
        session.commit()
        assert first["document_id"] == second["document_id"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(AppSetting)
                .where(AppSetting.key.startswith(PREFIX))
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(Product)) == 0
        signals = load_ranking_signals(session, ["101:201"])
    row = dict(
        identity="101:201",
        shop_id="101",
        observed_at=ctx["clock"][0].isoformat(),
        announced_price="100.00",
        currency="BRL",
        price_status="VALID_ANNOUNCED_PRICE",
        seller={},
    )
    score = rank_rows([row], now=ctx["clock"][0], signals_by_identity=signals)[0][
        "intelligent_ranking"
    ]
    assert score["known_points"] == 34 and score["coverage_percent"] == 35
    assert score["dimensions"]["trend"]["status"] == "UNKNOWN"
    assert not score["publication_authorized"]


@pytest.mark.parametrize(
    "changes",
    [
        dict(content_sha256="0" * 64),
        dict(confirmed_official_document=False),
        dict(confirmed_column_semantics=False),
        dict(confirmed_import=False),
        dict(source_url="https://invalid.example/"),
        dict(origin_operation="shopOfferV2"),
        dict(sales_period_seconds=None),
    ],
)
def test_invalid_or_unconfirmed_import_cannot_persist(context, changes):
    ctx = context
    with ctx["factory"]() as session:
        with pytest.raises(ValueError):
            import_ranking_signals(
                session, document(ctx["clock"][0], **changes), now=ctx["clock"][0], persist=True
            )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AppSetting)
                .where(AppSetting.key.startswith(PREFIX))
            )
            == 0
        )


def test_contradictory_same_time_documents_do_not_choose_by_arrival(context):
    ctx = context
    with ctx["factory"]() as session:
        for rating in ("4.0", "5.0"):
            import_ranking_signals(
                session,
                document(ctx["clock"][0], price_rating=rating),
                now=ctx["clock"][0],
                persist=True,
            )
        session.commit()
        loaded = load_ranking_signals(session, ["101:201"])
        assert "conflict" in loaded["101:201"]["reviews"]


def test_documented_lifetime_series_can_feed_trend_without_assuming_api_sales_period(context):
    ctx = context
    now = ctx["clock"][0]
    with ctx["factory"]() as session:
        for days, value in ((2, "100"), (1, "200"), (0, "400")):
            observed = now - timedelta(days=days)
            data = document(
                observed, sales=value, sales_semantics="LIFETIME", sales_period_seconds=None
            )
            import_ranking_signals(session, data, now=now, persist=True)
        session.commit()
        signals = load_ranking_signals(session, ["101:201"], now=now)
        assert len(signals["101:201"]["trend"]) == 3
    row = dict(
        identity="101:201",
        shop_id="101",
        observed_at=now.isoformat(),
        announced_price="100.00",
        currency="BRL",
        price_status="VALID_ANNOUNCED_PRICE",
        seller={},
    )
    score = rank_rows([row], now=now, signals_by_identity=signals)[0]["intelligent_ranking"]
    assert score["dimensions"]["trend"]["score"] == 15
    assert score["dimensions"]["seller"]["score"] is None


def test_stale_import_does_not_hide_current_partial_api_indicators(context):
    ctx = context
    with ctx["factory"]() as session:
        data = document(ctx["clock"][0] - timedelta(days=2))
        import_ranking_signals(session, data, now=ctx["clock"][0], persist=True)
        session.commit()
        assert load_ranking_signals(session, ["101:201"], now=ctx["clock"][0]) == {}


def test_document_preserves_bom_and_hash_but_not_as_a_column_name(context):
    ctx = context
    data = document(ctx["clock"][0])
    data["csv_content"] = "\ufeff" + data["csv_content"]
    data["content_sha256"] = hashlib.sha256(data["csv_content"].encode()).hexdigest()
    assert import_ranking_signals(None, data, now=ctx["clock"][0])["observations"] == 1


def test_worker_consumes_new_document_after_budget_without_new_search_or_offer(context):
    ctx = context
    plan = real.authorized(ctx, operating_policy={}, max_queries=1)
    offline.listings(ctx, [1], verified=False)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert Decimal(
        str(result["opportunities"]["analysis_queue"][0]["intelligent_ranking"]["coverage_percent"])
    ) == Decimal("17.5")
    ctx["clock"][0] += timedelta(seconds=30)
    with ctx["factory"]() as session:
        import_ranking_signals(
            session, document(ctx["clock"][0]), now=ctx["clock"][0], persist=True
        )
        session.commit()
    result = workers.worker(ctx, plan).step()
    row = result["opportunities"]["analysis_queue"][0]
    assert result["state"] == "ACTIVE_REAL" and ctx["calls"] == 1
    assert Decimal(str(row["intelligent_ranking"]["known_points"])) == 34
    assert (
        row["commercial_verification_state"] == "PENDING" and row["publication_state"] == "BLOCKED"
    )
    assert not ctx["telegram_calls"]


def test_mapped_feed_capture_requires_origin_authorization_but_does_not_call_feed(context):
    ctx = context
    data = document(
        ctx["clock"][0],
        origin_operation="getItemFeedData",
        source_authorization_reference="FICTICIO concessão anterior documentada",
    )
    result = import_ranking_signals(None, data, now=ctx["clock"][0])
    assert result["external_calls"] == 0 and result["observations"] == 1


def test_shop_document_without_item_enriches_same_store_without_inventing_product_ids(context):
    ctx = context
    row = dict(
        shop_id="101",
        observed_at=ctx["clock"][0].isoformat(),
        seller_rating="4.9",
        seller_review_count="100",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    content = stream.getvalue()
    data = document(
        ctx["clock"][0],
        origin_operation="shopOfferV2",
        source_authorization_reference="FICTICIO fonte autorizada registrada",
        csv_content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        columns={k: k for k in row},
    )
    with ctx["factory"]() as session:
        result = import_ranking_signals(session, data, now=ctx["clock"][0], persist=True)
        session.commit()
        assert result["signals"][0]["identity"] == "shop:101"
        sources = load_ranking_signals(
            session, ["101:201", "101:202", "999:777"], now=ctx["clock"][0]
        )
        assert set(sources) == {"101:201", "101:202"}
        assert sources["101:201"]["seller"]["subject_identity"] == "101"


def test_api_import_routes_are_authenticated_and_preview_does_not_write(context, client):
    from test_shopee_collection import api

    path = "/api/v1/shopee-discovery/ranking-signals"
    data = document(datetime.now(UTC) - timedelta(seconds=10))
    assert client.post(path + "/preview", json=data).status_code == 401
    assert client.post(path + "/import", json=data).status_code == 401
    response = client.post(path + "/preview", json=data, headers=api.HEADERS)
    assert response.status_code == 200 and not response.json()["persisted"]


def test_optional_returned_offer_link_is_only_a_candidate_and_payload_is_unchanged(context):
    ctx = context
    from app.integrations.shopee import ShopeeConnector

    node = dict(ctx["nodes"][0], offerLink="https://s.shopee.com.br/syntheticFixtureOnly")
    snapshot = ctx["connector"]._snapshot(node, ctx["clock"][0])
    candidate = snapshot.evidence_metadata["affiliate_link_candidate"]
    assert candidate["url"] == node["offerLink"]
    assert not candidate["ownership_verified"] and not candidate["operationally_verified"]
    assert "offerLink" not in ShopeeConnector.discovery_query(
        offline.QUERY, page=1, limit=3, profile="multistore"
    )
