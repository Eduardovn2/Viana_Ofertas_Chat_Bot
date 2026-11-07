import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.opportunity_ranking import RankingPolicy, rank_candidate, rank_rows

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def candidate(price="80.00"):
    return dict(
        identity="101:201",
        shop_id="101",
        name="FICTICIO Orion RX-500 azul kit unitário",
        announced_price=price,
        currency="BRL",
        price_status="VALID_ANNOUNCED_PRICE",
        observed_at=NOW.isoformat(),
        equivalence_proven=True,
        status="EQUIVALENT",
    )


def reference():
    return dict(
        kind="PROVEN_CROSS_STORE_MEDIAN",
        price="100.00",
        minimum="80.00",
        sample_size=3,
        identities=["101:201", "102:202", "103:203"],
        observed_at=NOW.isoformat(),
        evidence_reference="FICTICIO capture:documented-group",
        currency="BRL",
    )


def metric(kind, value, **changes):
    return (
        dict(
            subject_identity="101" if kind == "SELLER_RATING" else "101:201",
            metric=kind,
            value=str(value),
            sample_count=100,
            series_key="SYNTHETIC_SERIES_V1",
            source="OFFICIAL_EXPORT",
            source_url="https://affiliate.shopee.com.br/",
            reference="FICTICIO documental fixture",
            source_sha256="a" * 64,
            actor="synthetic_reviewer",
            semantics_reference="FICTICIO semântica documentada",
            confirmed_semantics=True,
            observed_at=NOW.isoformat(),
        )
        | changes
    )


def trend():
    return [
        metric("LIFETIME_SALES", value, observed_at=(NOW - timedelta(days=2 - index)).isoformat())
        for index, value in enumerate([100, 200, 400])
    ]


def full():
    return dict(
        reviews=metric("PRODUCT_RATING", 5),
        seller=metric("SELLER_RATING", 5),
        popularity=metric("LIFETIME_SALES", 1000),
        trend=trend(),
    )


def test_cheap_product_bad_reviews_does_not_receive_good_review_points():
    signals = full()
    signals["reviews"] = metric("PRODUCT_RATING", 1)
    result = rank_candidate(candidate(), now=NOW, price_reference=reference(), signals=signals)
    assert result["dimensions"]["price"]["score"] == 35
    assert result["dimensions"]["reviews"]["score"] == 0
    assert result["known_points"] == 80 and result["priority"] == "STRONG"
    assert not result["publication_authorized"]


def test_good_reviews_at_median_and_small_samples():
    signals = dict(reviews=metric("PRODUCT_RATING", 5, sample_count=5))
    result = rank_candidate(
        candidate("100.00"), now=NOW, price_reference=reference(), signals=signals
    )
    assert result["dimensions"]["price"]["score"] == Decimal("17.50")
    assert result["dimensions"]["reviews"]["score"] == 1
    assert result["dimensions"]["reviews"]["status"] == "PARTIAL"
    assert result["priority"] == "INSUFFICIENT_DATA"


def test_popular_product_is_not_automatically_trending():
    result = rank_candidate(
        candidate(), now=NOW, signals={"popularity": metric("LIFETIME_SALES", 1000)}
    )
    assert result["dimensions"]["popularity"]["score"] == 15
    assert result["dimensions"]["trend"]["score"] is None
    assert result["dimensions"]["trend"]["status"] == "UNKNOWN"


def test_official_popularity_rank_requires_list_size():
    raw = metric("OFFICIAL_RANK", 1)
    assert (
        rank_candidate(candidate(), now=NOW, signals={"popularity": raw})["dimensions"][
            "popularity"
        ]["score"]
        is None
    )
    raw["population_size"] = 100
    assert (
        rank_candidate(candidate(), now=NOW, signals={"popularity": raw})["dimensions"][
            "popularity"
        ]["score"]
        == 15
    )


def test_raw_rating_sales_and_shop_type_are_not_semantic_evidence():
    row = candidate()
    row["seller"] = dict(rating_star="5", sales=999999, shop_types=[1], shop_name="Oficial")
    result = rank_candidate(row, now=NOW)
    assert all(d["score"] is None for d in result["dimensions"].values())
    assert result["known_points"] == 0 and result["possible_max_points"] == 100
    assert result["coverage_percent"] == 0 and result["definitive_score"] is None
    assert result["priority"] == "INSUFFICIENT_DATA"


@pytest.mark.parametrize(
    "change",
    [
        dict(equivalence_proven=False),
        dict(identity="999:888"),
        dict(currency="USD"),
        dict(status="REJECTED"),
        dict(price_status="AMBIGUOUS_OR_INVALID_PRICE"),
    ],
)
def test_different_or_unproven_products_cannot_use_group_reference(change):
    row = candidate() | change
    assert (
        rank_candidate(row, now=NOW, price_reference=reference())["dimensions"]["price"]["score"]
        is None
    )


def test_extremely_low_price_requires_review_instead_of_maximum_score():
    result = rank_candidate(
        candidate("10.00"), now=NOW, price_reference=reference(), signals=full()
    )
    assert result["dimensions"]["price"]["status"] == "UNKNOWN"
    assert "ANOMALOUS" in result["dimensions"]["price"]["reason"]
    assert result["priority"] == "INSUFFICIENT_DATA"


def test_full_coverage_can_be_exceptional_but_never_authorizes_publication():
    result = rank_candidate(candidate(), now=NOW, price_reference=reference(), signals=full())
    assert result["known_points"] == 100 and result["coverage_percent"] == 100
    assert result["priority"] == "EXCEPTIONAL" and result["definitive_score"] == 100
    assert not result["publication_authorized"]


def test_updated_sample_updates_points_and_coverage():
    old = rank_candidate(
        candidate(), now=NOW, signals={"reviews": metric("PRODUCT_RATING", 5, sample_count=5)}
    )
    new = rank_candidate(candidate(), now=NOW, signals={"reviews": metric("PRODUCT_RATING", 5)})
    assert new["known_points"] > old["known_points"]
    assert new["coverage_percent"] > old["coverage_percent"]


def test_missing_review_count_never_creates_an_assumed_sample_score():
    result = rank_candidate(
        candidate(), now=NOW, signals={"reviews": metric("PRODUCT_RATING", 5, sample_count=None)}
    )
    assert result["dimensions"]["reviews"]["status"] == "PARTIAL"
    assert result["dimensions"]["reviews"]["score"] is None
    assert result["known_points"] == 0


@pytest.mark.parametrize(
    "change",
    [
        dict(subject_identity="999"),
        dict(confirmed_semantics=False),
        dict(observed_at=(NOW + timedelta(seconds=1)).isoformat()),
        dict(observed_at=(NOW - timedelta(days=3)).isoformat()),
    ],
)
def test_wrong_subject_unconfirmed_stale_and_future_metrics_are_unknown(change):
    result = rank_candidate(
        candidate(), now=NOW, signals={"reviews": metric("PRODUCT_RATING", 5, **change)}
    )
    assert result["dimensions"]["reviews"]["score"] is None


def test_tendency_requires_real_window_consistent_counter_and_three_observations():
    assert (
        rank_candidate(candidate(), now=NOW, signals={"trend": trend()})["dimensions"]["trend"][
            "score"
        ]
        == 15
    )
    points = trend()
    points[-1]["value"] = "10"
    assert (
        rank_candidate(candidate(), now=NOW, signals={"trend": points})["dimensions"]["trend"][
            "score"
        ]
        is None
    )
    assert (
        rank_candidate(candidate(), now=NOW, signals={"trend": trend()[:1]})["dimensions"]["trend"][
            "score"
        ]
        is None
    )


def test_sales_period_and_reputation_subject_cannot_be_invented():
    result = rank_candidate(
        candidate(),
        now=NOW,
        signals=dict(popularity=metric("WINDOW_SALES", 1000), seller=metric("PRODUCT_RATING", 5)),
    )
    assert result["dimensions"]["popularity"]["score"] is None
    assert result["dimensions"]["seller"]["score"] is None


@pytest.mark.parametrize(
    "change", [dict(value="1.5"), dict(source_url="https://shopee.com.br.invalid.example/")]
)
def test_fractional_sales_and_untrusted_sources_are_unknown(change):
    raw = metric("LIFETIME_SALES", 1000) | change
    assert (
        rank_candidate(candidate(), now=NOW, signals={"popularity": raw})["dimensions"][
            "popularity"
        ]["score"]
        is None
    )


def test_group_reference_must_come_from_same_capture_time():
    ref = reference() | dict(observed_at=(NOW - timedelta(seconds=30)).isoformat())
    assert (
        rank_candidate(candidate(), now=NOW, price_reference=ref)["dimensions"]["price"]["score"]
        is None
    )


def test_partial_scores_have_separate_bounds_and_coverage():
    result = rank_candidate(candidate(), now=NOW, price_reference=reference())
    assert result["known_points"] == result["known_max_score"] == result["coverage_percent"] == 35
    assert result["possible_max_points"] == 100
    assert result["definitive_score"] is None


def test_ties_and_admin_sort_options_are_deterministic_without_mutation():
    rows = [candidate() | dict(identity="102:202"), candidate()]
    saved = copy.deepcopy(rows)
    for sort_by in ("score", "price", "coverage", "opportunity"):
        ranked = rank_rows(rows, now=NOW, sort_by=sort_by)
        assert [r["identity"] for r in ranked] == ["101:201", "102:202"]
    assert rows == saved
    with pytest.raises(ValueError):
        rank_rows(rows, now=NOW, sort_by="random")


def test_policy_rejects_weights_not_totaling_one_hundred():
    with pytest.raises(ValueError):
        RankingPolicy(weights=dict(price=90, reviews=20, popularity=15, trend=15, seller=15))
