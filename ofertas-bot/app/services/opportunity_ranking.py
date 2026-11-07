"""Deterministic documentary ranking. Scores never confer commercial authority."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.services.comparison import aware_utc, valid_money

D = Decimal
CENT = D("0.01")
WEIGHTS = dict(price=35, reviews=20, popularity=15, trend=15, seller=15)


class RankingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    weights: dict[str, int] = Field(default_factory=lambda: dict(WEIGHTS))
    sufficient_coverage: int = Field(default=80, ge=1, le=100)
    exceptional: int = 85
    strong: int = 70
    interesting: int = 55
    sample_for_full_score: int = Field(default=100, ge=30)
    popularity_units_for_full_score: int = Field(default=1000, ge=1)
    metric_max_age_seconds: int = Field(default=86400, ge=1)
    price_max_age_seconds: int = Field(default=600, ge=1)
    trend_min_window_seconds: int = Field(default=86400, ge=3600)
    anomaly_drop_percent: Decimal = Field(default=D("50"), gt=0, le=100)

    @model_validator(mode="after")
    def bounded(self):
        if (
            set(self.weights) != set(WEIGHTS)
            or any(type(v) is not int or v <= 0 for v in self.weights.values())
            or sum(self.weights.values()) != 100
        ):
            raise ValueError("Pesos inteiros positivos devem somar 100 nas cinco dimensões.")
        if not 0 <= self.interesting < self.strong < self.exceptional <= 100:
            raise ValueError("Faixas de prioridade inválidas.")
        return self


class MetricEvidence(BaseModel):
    """Only documented semantics; raw API seller fields are never mapped here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    subject_identity: str = Field(min_length=1)
    metric: Literal[
        "PRODUCT_RATING",
        "SELLER_RATING",
        "LIFETIME_SALES",
        "WINDOW_SALES",
        "OFFICIAL_RANK",
        "PRODUCT_RATING_STAR_UNSCALED",
        "PRODUCT_SALES_UNSPECIFIED_PERIOD",
    ]
    value: Decimal = Field(ge=0, allow_inf_nan=False)
    sample_count: int | None = Field(default=None, strict=True, ge=0)
    population_size: int | None = Field(default=None, strict=True, ge=1)
    period_seconds: int | None = Field(default=None, strict=True, gt=0)
    series_key: str = Field(min_length=3)
    source: Literal["OFFICIAL_API", "OFFICIAL_EXPORT", "MANUAL_DOCUMENTED"]
    source_url: str = Field(pattern=r"^https://", max_length=2048)
    reference: str = Field(min_length=3)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=3)
    semantics_reference: str = Field(min_length=3)
    confirmed_semantics: StrictBool
    observed_at: datetime

    @field_validator("source_url")
    @classmethod
    def official_source(cls, value):
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        if (
            url.username
            or url.password
            or url.port not in (None, 443)
            or not any(
                host == domain or host.endswith("." + domain)
                for domain in ("shopee.com.br", "shopee.com")
            )
        ):
            raise ValueError("Fonte documental fora dos domínios oficiais suportados.")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def exact_decimal(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Métrica exige decimal exato.")
        return value

    @field_validator("reference", "series_key", "actor", "semantics_reference")
    @classmethod
    def meaningful(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Referência documental inválida.")
        return value.strip()

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)

    @model_validator(mode="after")
    def explicit(self):
        if self.confirmed_semantics is not True:
            raise ValueError("Semântica documental não confirmada.")
        if self.metric == "WINDOW_SALES" and self.period_seconds is None:
            raise ValueError("Vendas por período exigem duração documentada.")
        if self.metric == "LIFETIME_SALES" and self.period_seconds is not None:
            raise ValueError("Vendas acumuladas não são vendas de uma janela.")
        if (
            self.metric
            in {
                "LIFETIME_SALES",
                "WINDOW_SALES",
                "OFFICIAL_RANK",
                "PRODUCT_SALES_UNSPECIFIED_PERIOD",
            }
            and self.value != self.value.to_integral_value()
        ):
            raise ValueError("Contagens e posições exigem valores inteiros documentados.")
        return self


def _dimension(maximum, reason, *, score=None, status="UNKNOWN", evidence=()):
    return dict(
        score=score.quantize(CENT, rounding=ROUND_HALF_UP) if score is not None else None,
        max_score=maximum,
        status=status,
        evidence=list(evidence),
        reason=reason,
    )


def _metric(raw, identity, metric, now, policy):
    if raw is None:
        return None
    value = MetricEvidence.model_validate(raw)
    if (
        value.subject_identity != identity
        or value.metric not in metric
        or not 0
        <= (aware_utc(now) - value.observed_at).total_seconds()
        <= policy.metric_max_age_seconds
    ):
        return None
    return value


def _rating(raw, identity, metric, now, policy, maximum):
    if metric == "PRODUCT_RATING":
        unscaled = _metric(raw, identity, {"PRODUCT_RATING_STAR_UNSCALED"}, now, policy)
        if unscaled is not None:
            return _dimension(
                maximum,
                "PRODUCT_RATING_REPORTED_SCALE_AND_SAMPLE_UNDOCUMENTED",
                status="PARTIAL",
                evidence=[unscaled.model_dump(mode="json")],
            )
    value = _metric(raw, identity, {metric}, now, policy)
    if value is None or not 1 <= value.value <= 5 or value.sample_count == 0:
        return _dimension(maximum, "NO_CURRENT_DOCUMENTED_RATING_AND_SAMPLE")
    if value.sample_count is None:
        return _dimension(
            maximum,
            "REVIEW_COUNT_UNKNOWN_NO_CONSERVATIVE_SCORE",
            status="PARTIAL",
            evidence=[value.model_dump(mode="json")],
        )
    factor = (
        min(D(value.sample_count) / policy.sample_for_full_score, D(1))
        if value.sample_count is not None
        else D(0)
    )
    score = D(maximum) * (value.value - 1) / 4 * factor
    return _dimension(
        maximum,
        "DOCUMENTED_RATING_SAMPLE_SHRINKAGE",
        score=score,
        status="VERIFIED"
        if value.sample_count is not None and value.sample_count >= policy.sample_for_full_score
        else "PARTIAL",
        evidence=[value.model_dump(mode="json")],
    )


def _price(row, reference, now, policy):
    maximum = policy.weights["price"]
    if row.get("status") == "REJECTED":
        return _dimension(maximum, "INCOMPATIBLE_OR_CONFLICTING_IDENTITY")
    if row.get("price_status") != "VALID_ANNOUNCED_PRICE" or row.get("currency") != "BRL":
        return _dimension(maximum, "AMBIGUOUS_OR_INVALID_ANNOUNCED_PRICE")
    current = D(str(row.get("announced_price")))
    if (
        not valid_money(current)
        or not 0
        <= (
            aware_utc(now) - aware_utc(datetime.fromisoformat(str(row["observed_at"])))
        ).total_seconds()
        <= policy.price_max_age_seconds
    ):
        return _dimension(maximum, "CURRENT_ANNOUNCED_PRICE_REQUIRED")
    if not reference or not reference.get("evidence_reference"):
        return _dimension(maximum, "NO_RELIABLE_COMPARABLE_PRICE_REFERENCE")
    if reference.get("currency") != "BRL":
        return _dimension(maximum, "INCOMPARABLE_REFERENCE_CURRENCY")
    if reference["kind"] == "PROVEN_CROSS_STORE_MEDIAN":
        if (
            not row.get("equivalence_proven")
            or reference.get("sample_size", 0) < 2
            or row["identity"] not in reference.get("identities", ())
        ):
            return _dimension(maximum, "EQUIVALENCE_NOT_PROVEN_FOR_REFERENCE")
        if aware_utc(datetime.fromisoformat(reference["observed_at"])) != aware_utc(
            datetime.fromisoformat(str(row["observed_at"]))
        ):
            return _dimension(maximum, "CROSS_STORE_REFERENCE_FROM_ANOTHER_OBSERVATION")
    elif reference["kind"] == "SAME_LISTING_HISTORY":
        if (
            reference.get("identity") != row["identity"]
            or reference.get("same_documented_context") is not True
        ):
            return _dimension(maximum, "HISTORICAL_IDENTITY_OR_CONTEXT_NOT_PROVEN")
        if aware_utc(datetime.fromisoformat(reference["observed_at"])) >= aware_utc(
            datetime.fromisoformat(str(row["observed_at"]))
        ):
            return _dimension(maximum, "HISTORICAL_REFERENCE_MUST_PRECEDE_CURRENT_OBSERVATION")
    else:
        return _dimension(maximum, "UNSUPPORTED_REFERENCE_KIND")
    baseline = D(str(reference["price"]))
    if not valid_money(baseline):
        return _dimension(maximum, "INVALID_REFERENCE_PRICE")
    drop = (baseline - current) / baseline * 100
    if drop >= policy.anomaly_drop_percent:
        return _dimension(
            maximum, "ANOMALOUS_LOW_PRICE_REQUIRES_IDENTITY_REVIEW", evidence=[reference]
        )
    # At median: half the price points; 20% below: full; 20% above: zero.
    score = D(maximum) * max(D(0), min(D(1), D("0.5") + drop / 40))
    return _dimension(
        maximum,
        "ANNOUNCED_PRICE_VS_DOCUMENTED_REFERENCE_NOT_HISTORICAL_DISCOUNT",
        score=score,
        status="VERIFIED",
        evidence=[dict(reference, below_reference_percent=str(drop.quantize(CENT)))],
    )


def _trend(raw, identity, now, policy):
    maximum = policy.weights["trend"]
    if not raw or not isinstance(raw, list) or not 3 <= len(raw) <= 100:
        return _dimension(maximum, "AT_LEAST_THREE_COMPARABLE_TEMPORAL_OBSERVATIONS_REQUIRED")
    points = sorted((MetricEvidence.model_validate(v) for v in raw), key=lambda v: v.observed_at)
    last = points[-1]
    keys = {
        (
            p.subject_identity,
            p.metric,
            p.source,
            p.series_key,
            p.period_seconds,
            p.semantics_reference,
        )
        for p in points
    }
    if (
        len(keys) != 1
        or last.subject_identity != identity
        or last.metric not in {"LIFETIME_SALES", "OFFICIAL_RANK"}
        or len({p.observed_at for p in points}) != len(points)
    ):
        return _dimension(maximum, "INCONSISTENT_TREND_SERIES")
    if (
        _metric(last, identity, {last.metric}, now, policy) is None
        or any(p.observed_at > aware_utc(now) for p in points)
        or (last.observed_at - points[0].observed_at).total_seconds()
        < policy.trend_min_window_seconds
    ):
        return _dimension(maximum, "INSUFFICIENT_WINDOW_OR_STALE_TREND")
    if last.metric == "LIFETIME_SALES":
        if any(b.value < a.value for a, b in zip(points, points[1:], strict=False)):
            return _dimension(maximum, "COUNTER_RESET_OR_INCOMPARABLE_SERIES")
        first_rate = (points[1].value - points[0].value) / D(
            str((points[1].observed_at - points[0].observed_at).total_seconds())
        )
        recent_rate = (last.value - points[-2].value) / D(
            str((last.observed_at - points[-2].observed_at).total_seconds())
        )
        if first_rate <= 0:
            return _dimension(maximum, "ZERO_BASELINE_RATE_NO_RELIABLE_GROWTH_PERCENT")
        growth = (recent_rate / first_rate - 1) * 100
    else:
        if points[0].value <= 0 or any(p.value < 1 for p in points):
            return _dimension(maximum, "INVALID_OFFICIAL_RANK")
        growth = (points[0].value - last.value) / points[0].value * 100
    return _dimension(
        maximum,
        "DOCUMENTED_TEMPORAL_GROWTH_NOT_TITLE_OR_CUMULATIVE_SALES",
        score=D(maximum) * max(D(0), min(D(1), growth / 100)),
        status="VERIFIED",
        evidence=[p.model_dump(mode="json") for p in points],
    )


def rank_candidate(row, *, now, price_reference=None, signals=None, policy=None):
    policy = policy or RankingPolicy()
    signals = signals if isinstance(signals, dict) else {}
    dimensions = {}
    handlers = {
        "price": lambda: _price(row, price_reference, now, policy),
        "reviews": lambda: _rating(
            signals.get("reviews"),
            row["identity"],
            "PRODUCT_RATING",
            now,
            policy,
            policy.weights["reviews"],
        ),
        "seller": lambda: _rating(
            signals.get("seller"),
            row["shop_id"],
            "SELLER_RATING",
            now,
            policy,
            policy.weights["seller"],
        ),
        "trend": lambda: _trend(signals.get("trend"), row["identity"], now, policy),
    }
    for name, handler in handlers.items():
        try:
            dimensions[name] = handler()
        except (ValueError, TypeError, KeyError, ArithmeticError):
            dimensions[name] = _dimension(
                policy.weights[name], "INVALID_OR_UNBOUND_DOCUMENTARY_EVIDENCE"
            )
    try:
        popular = _metric(
            signals.get("popularity"),
            row["identity"],
            {"LIFETIME_SALES", "WINDOW_SALES", "OFFICIAL_RANK", "PRODUCT_SALES_UNSPECIFIED_PERIOD"},
            now,
            policy,
        )
        ratio = (
            min(D(1), popular.value / policy.popularity_units_for_full_score) if popular else D(0)
        )
        if popular and popular.metric == "OFFICIAL_RANK":
            if popular.population_size is None or not 1 <= popular.value <= popular.population_size:
                raise ValueError("Ranking oficial sem população comparável.")
            ratio = D(1) - (popular.value - 1) / max(D(1), D(popular.population_size - 1))
        dimensions["popularity"] = (
            _dimension(
                policy.weights["popularity"],
                "DOCUMENTED_OFFICIAL_RANK_POSITION"
                if popular.metric == "OFFICIAL_RANK"
                else "DOCUMENTED_SALES_VOLUME_NOT_RECENT_SEARCHES_OR_TREND",
                score=D(policy.weights["popularity"]) * ratio,
                status="VERIFIED",
                evidence=[popular.model_dump(mode="json")],
            )
            if popular
            else _dimension(
                policy.weights["popularity"], "NO_SALES_WITH_CONFIRMED_SEMANTICS_AND_PERIOD"
            )
        )
        if popular and popular.metric == "PRODUCT_SALES_UNSPECIFIED_PERIOD":
            dimensions["popularity"] = _dimension(
                policy.weights["popularity"],
                "PRODUCT_SALES_COUNT_REPORTED_PERIOD_UNDOCUMENTED",
                status="PARTIAL",
                evidence=[popular.model_dump(mode="json")],
            )
    except (ValueError, TypeError, ArithmeticError):
        dimensions["popularity"] = _dimension(
            policy.weights["popularity"], "INVALID_OR_UNBOUND_DOCUMENTARY_EVIDENCE"
        )
    known = sum((d["score"] for d in dimensions.values() if d["score"] is not None), D(0))
    known_max = sum(d["max_score"] for d in dimensions.values() if d["status"] != "UNKNOWN")
    coverage = sum(
        (
            D(d["max_score"])
            * (1 if d["status"] == "VERIFIED" else D("0.5") if d["status"] == "PARTIAL" else 0)
            for d in dimensions.values()
        ),
        D(0),
    )
    upper = known + sum(
        (
            D(d["max_score"]) - (d["score"] or 0)
            for d in dimensions.values()
            if d["status"] != "VERIFIED"
        ),
        D(0),
    )
    sufficient = (
        coverage >= policy.sufficient_coverage
        and dimensions["price"]["status"] == "VERIFIED"
        and sum(d["status"] == "VERIFIED" for d in dimensions.values()) >= 4
    )
    priority = (
        "INSUFFICIENT_DATA"
        if not sufficient
        else "EXCEPTIONAL"
        if known >= policy.exceptional
        else "STRONG"
        if known >= policy.strong
        else "INTERESTING"
        if known >= policy.interesting
        else "MONITORING"
    )
    return dict(
        version="OPPORTUNITY_100_V1",
        weights=dict(policy.weights),
        dimensions=dimensions,
        known_points=known,
        conservative_score=known,
        known_max_score=known_max,
        possible_max_points=upper,
        coverage_percent=coverage,
        scorable_coverage_percent=sum(
            d["max_score"] for d in dimensions.values() if d["score"] is not None
        ),
        sufficient_evidence=sufficient,
        priority=priority,
        priority_label={
            "EXCEPTIONAL": "Oportunidade excepcional",
            "STRONG": "Oportunidade forte",
            "INTERESTING": "Oportunidade interessante",
            "MONITORING": "Monitoramento",
            "INSUFFICIENT_DATA": "Dados insuficientes",
        }[priority],
        definitive_score=known if coverage == 100 else None,
        missing_dimensions=[k for k, d in dimensions.items() if d["status"] != "VERIFIED"],
        evidence_semantics="DOCUMENTED_SOURCE_AND_SEMANTICS_NOT_AUTOMATIC_PLATFORM_ATTESTATION",
        experimental_thresholds=True,
        publication_authorized=False,
    )


def rank_rows(rows, *, now, sort_by="score", signals_by_identity=None):
    if sort_by not in {"score", "price", "coverage", "opportunity"}:
        raise ValueError("Ordenação inválida.")
    result = []
    for raw in rows:
        row = dict(raw)
        opportunity = row.get("opportunity", {})
        stats = opportunity.get("statistics")
        reference = row.get("historical_price_reference")
        if stats and opportunity.get("group"):
            reference = dict(
                kind="PROVEN_CROSS_STORE_MEDIAN",
                price=stats["median"],
                minimum=stats["minimum"],
                sample_size=stats["sample_size"],
                identities=opportunity.get("compared_identities", []),
                evidence_reference=f"capture:{row.get('capture_id')}:{opportunity['group']}",
                observed_at=str(row.get("observed_at")),
                currency="BRL",
            )
        from app.services.shopee_indicators import automatic_product_signals, product_indicators

        row["product_indicators"] = product_indicators(row)
        try:
            age = (
                aware_utc(now) - aware_utc(datetime.fromisoformat(str(row["observed_at"])))
            ).total_seconds()
        except (ValueError, TypeError):
            age = None
        row["product_indicators"]["current_for_metrics"] = age is not None and 0 <= age <= 86400
        signals = automatic_product_signals(row)
        signals.update(row.get("documented_ranking_signals") or {})
        signals.update((signals_by_identity or {}).get(row["identity"]) or {})
        row["intelligent_ranking"] = rank_candidate(
            row,
            now=now,
            price_reference=reference,
            signals=signals,
        )
        row["enrichment_dimensions"] = row["intelligent_ranking"]["missing_dimensions"]
        row["enrichment_requirements"] = {
            dimension: (
                ["rating_scale", "product_review_count"]
                if dimension == "reviews"
                else ["sales_period"]
                if dimension == "popularity"
                else ["documented_seller_metric", "seller_sample_size"]
                if dimension == "seller"
                else ["three_comparable_observations_over_24_hours"]
                if dimension == "trend"
                else ["current_quote_and_reliable_same_product_reference"]
            )
            for dimension in row["enrichment_dimensions"]
        }
        result.append(row)

    def key(row):
        ranking = row["intelligent_ranking"]
        primary = (
            (
                D(str(row["announced_price"]))
                if row.get("price_status") == "VALID_ANNOUNCED_PRICE" and row.get("announced_price")
                else D("Infinity")
            )
            if sort_by == "price"
            else -ranking["coverage_percent"]
            if sort_by == "coverage"
            else -(D(str(row.get("opportunity", {}).get("below_median_percent") or 0)))
            if sort_by == "opportunity"
            else -ranking["known_points"]
        )
        return (
            row.get("status") == "REJECTED",
            primary,
            -ranking["coverage_percent"],
            row["identity"],
            row.get("capture_id", ""),
        )

    result.sort(key=key)
    for index, row in enumerate(result, 1):
        row["intelligent_rank"] = index
    return result
