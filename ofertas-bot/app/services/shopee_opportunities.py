"""Consolidated discovery decisions and a non-executable commercial handoff preview."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.contracts import fingerprint
from app.services.opportunity_ranking import rank_rows

CORE_FIELDS = (
    "brand",
    "model",
    "quantity",
    "condition",
    "packaging",
    "kit",
    "variant",
    "price_conditions",
)
COMMERCIAL_BLOCKERS = (
    "APPLICABLE_PUBLIC_PRICE_CONTEXT_NOT_VERIFIED",
    "AVAILABILITY_NOT_VERIFIED",
    "FRESHNESS_REVALIDATION_NOT_PERFORMED",
    "FINAL_PRICE_SHIPPING_COUPON_TERMS_NOT_VERIFIED",
    "EXISTING_AFFILIATE_LINK_AND_ACCOUNT_CHECK_NOT_PERFORMED",
    "CATEGORY_DESTINATION_AND_OFFER_APPROVAL_CHECKS_NOT_PERFORMED",
    "CROSS_STORE_ADAPTER_TO_OFFER_APPROVAL_NOT_ENABLED",
)


class CommercialHandoffPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["MULTISTORE_CANDIDATE_PREVIEW_V1"] = "MULTISTORE_CANDIDATE_PREVIEW_V1"
    reference_kind: Literal["CROSS_STORE_ANNOUNCED_PRICE"] = "CROSS_STORE_ANNOUNCED_PRICE"
    historical_discount: None = None
    execution_allowed: Literal[False] = False
    can_create_offer: Literal[False] = False
    publication_authorized: Literal[False] = False
    coupon_required: Literal[False] = False
    blockers: list[str] = Field(default_factory=lambda: list(COMMERCIAL_BLOCKERS))
    candidates: list[dict] = Field(default_factory=list)


def candidate_decision(row, target, comparable_groups):
    facts, hints = row["attributes"], row["inferred_attributes"]
    required = set(CORE_FIELDS)
    standardized = "creatina" in str(facts.get("model") or "").casefold() or any(
        h["rule"] == "PRODUCT_WORD_MENTION" for h in hints.get("product_role", [])
    )
    if standardized or hints.get("net_content") or (target or {}).get("net_content"):
        required.add("net_content")
    if standardized or hints.get("edition") or (target or {}).get("edition"):
        required.add("edition")
    pending = set(row.get("documentary_review", {}).get("pending_fields", []))
    missing = [
        dict(
            attribute=field,
            reason="PENDING_MANUAL_CONFIRMATION" if field in pending else "UNPROVEN_ATTRIBUTE",
        )
        for field in sorted(required)
        if facts.get(field) is None
    ]
    conflicts = list(row.get("evidence_conflicts", []))
    stale = (
        bool(row.get("review_issue"))
        or row.get("documentary_review", {}).get("source_current") is False
        or row["reason"]
        in {
            "EVIDENCE_FOR_DIFFERENT_QUOTE",
            "EVIDENCE_PREDATES_QUOTE",
        }
    )
    if stale:
        missing.append(dict(attribute="evidence_binding", reason="STALE_OR_DIFFERENT_OBSERVATION"))
    states = {}
    for field, value in facts.items():
        states[field] = (
            "DOCUMENTED"
            if value is not None and row.get("evidence_source") == "MANUAL_DOCUMENTED"
            else "INFERRED_ONLY"
            if hints.get(field)
            else "UNKNOWN"
        )
    for issue in conflicts:
        for token, field in (
            ("CONTENT", "net_content"),
            ("EDITION", "edition"),
            ("COUNT", "quantity"),
        ):
            if token in issue:
                states[field] = "CONFLICTING_OR_INSUFFICIENT_DOCUMENTATION"
    human = False
    if row["equivalence_classification"] == "PROVEN_INCOMPATIBLE":
        action = "REJECTED_DOCUMENTED_MISMATCH"
    elif row["status"] == "REJECTED":
        action = "FILTERED_BY_RELEVANCE"
    elif stale or conflicts:
        action, human = "REVIEW_STALE_OR_CONFLICTING_EVIDENCE", True
    elif row["status"] == "EQUIVALENT":
        action = (
            "AUTO_COMPARED"
            if row.get("group") in comparable_groups
            else "AWAITING_COMPARABLE_SELLER"
        )
        if action == "AWAITING_COMPARABLE_SELLER":
            missing.append(
                dict(attribute="comparable_seller", reason="SECOND_EQUIVALENT_STORE_NOT_OBSERVED")
            )
    elif missing:
        action, human = "REVIEW_INSUFFICIENT_EVIDENCE", True
    elif row["price_status"] != "VALID_ANNOUNCED_PRICE":
        action = "AUTHORIZED_PRICE_REVALIDATION_REQUIRED"
        missing.append(dict(attribute="announced_price", reason="UNRESOLVED_CAPTURED_QUOTE"))
    else:
        action, human = "REVIEW_INSUFFICIENT_EVIDENCE", True
        missing.append(dict(attribute="identity_or_relevance", reason=row["reason"]))
    return dict(
        identity=row["identity"],
        shop_id=row["shop_id"],
        action=action,
        required_attributes=sorted(required),
        human_review_required=human,
        attribute_states=states,
        missing_information=missing,
        conflicting_evidence=conflicts,
        evidence_reused_automatically=row.get("evidence_source") == "MANUAL_DOCUMENTED"
        and not stale
        and not conflicts,
        fingerprint=row.get("observation_fingerprint") or row.get("quote_fingerprint"),
    )


def consolidate_opportunities(analysis, *, selection_limit=2):
    if type(selection_limit) is not int or not 0 <= selection_limit <= 20:
        raise ValueError("Seleção para revisão deve ser inteira entre 0 e 20.")
    groups = analysis["proven_comparisons"]
    comparable = {g["group"] for g in groups}
    decisions = [
        candidate_decision(row, analysis.get("requested_product"), comparable)
        for row in analysis["candidates"]
    ]
    by_id = {d["identity"]: d for d in decisions}
    queue = [q for q in analysis["review_queue"] if by_id[q["identity"]]["human_review_required"]]
    selected, selected_ids, shops = [], set(), set()
    # First cover distinct shops; only then use remaining slots for alternatives.
    for distinct in (True, False):
        for candidate in queue:
            if len(selected) >= selection_limit:
                break
            if candidate["identity"] in selected_ids or distinct and candidate["shop_id"] in shops:
                continue
            selected.append(candidate)
            selected_ids.add(candidate["identity"])
            shops.add(candidate["shop_id"])
    preview_candidates = []
    for group in groups:
        for row in group["ranking"]:
            if row["identity"] in group["lowest_announced_identities"]:
                preview_candidates.append(
                    dict(
                        identity=row["identity"],
                        shop_id=row["shop_id"],
                        comparison_group=group["group"],
                        announced_price=row["announced_price"],
                        currency=row["currency"],
                        observation_fingerprint=row.get("observation_fingerprint"),
                        quote_fingerprint=row.get("quote_fingerprint"),
                        observed_at=row["observed_at"],
                        final_price=None,
                        shipping=None,
                        coupon_applicability="UNKNOWN",
                        availability="UNKNOWN",
                        target_stage="ASSISTED_OFFER_REVIEW_PREVIEW_ONLY",
                    )
                )
    return dict(
        query=analysis["query"],
        requested_product=analysis.get("requested_product"),
        product_search_key=fingerprint(
            {"query": analysis["query"], "product": analysis.get("requested_product")}
        ),
        view_version="MULTISTORE_DISCOVERY_V1",
        review_selection_limit=selection_limit,
        source_authorization_id=analysis.get("source_authorization_id"),
        source_scope_sha256=analysis.get("source_scope_sha256"),
        returned_listings=analysis["returned_listings"],
        distinct_shops=analysis["distinct_shops"],
        proven_comparisons=groups,
        unproven_candidates=analysis["unproven_candidates"],
        stores=analysis["stores"],
        candidate_decisions=decisions,
        review_queue=queue,
        selected_for_review=selected,
        deferred_review_identities=[
            q["identity"] for q in queue if q["identity"] not in selected_ids
        ],
        counts=dict(
            automatic_compared=sum(d["action"] == "AUTO_COMPARED" for d in decisions),
            human_review_required=sum(d["human_review_required"] for d in decisions),
            selected_for_review=len(selected),
            deferred_review=len(queue) - len(selected),
            awaiting_comparable_seller=sum(
                d["action"] == "AWAITING_COMPARABLE_SELLER" for d in decisions
            ),
            price_revalidation_required=sum(
                d["action"] == "AUTHORIZED_PRICE_REVALIDATION_REQUIRED" for d in decisions
            ),
            filtered_or_incompatible=sum(
                d["action"] in {"FILTERED_BY_RELEVANCE", "REJECTED_DOCUMENTED_MISMATCH"}
                for d in decisions
            ),
        ),
        commercial_handoff=CommercialHandoffPreview(candidates=preview_candidates).model_dump(),
        selection_semantics="TEXTUAL_REVIEW_PRIORITY_WITH_SHOP_DIVERSITY_NOT_BUY_RECOMMENDATION",
        publication_authorized=False,
        history_writes=0,
        history_semantics="CONSOLIDATION_ONLY_NO_TEMPORAL_BASELINE_OR_WRITES",
    )


def attach_opportunities(analysis, *, selection_limit=2):
    view = consolidate_opportunities(analysis, selection_limit=selection_limit)
    analysis["review_queue"] = view["review_queue"]
    analysis["opportunity_view"] = view
    return analysis


def pilot_opportunities(captures, *, now, commercial_queue=(), signals_by_identity=None):
    """Local analysis queue; never creates an offer or attests a commercial fact.

    Each capture retains its own comparison population. Observations from other
    terms or times cannot become a cross-store mean or a historical discount.
    """
    from datetime import datetime
    from decimal import ROUND_HALF_UP, Decimal

    from app.services.comparison import aware_utc

    queue, comparisons = [], []
    verified_quotes = {
        fingerprint(item["quote"]): item
        for item in commercial_queue
        if item["state"] in {"QUEUED", "CLAIMED", "SENT"}
    }
    for capture in captures:
        analysis = capture["comparison"]
        if fingerprint(analysis) != capture["comparison_sha256"]:
            raise ValueError("Captura de descoberta alterada.")
        decisions = {d["identity"]: d for d in analysis["opportunity_view"]["candidate_decisions"]}
        benchmarks = {}
        for group in analysis["proven_comparisons"]:
            comparisons.append(
                dict(capture_id=capture["capture_id"], query=analysis["query"], **group)
            )
            stats = group.get("price_statistics")
            if not stats:
                continue
            mean, median = Decimal(str(stats["mean"])), Decimal(str(stats["median"]))
            for ranked in group["ranking"]:
                price = Decimal(str(ranked["announced_price"]))
                benchmarks[ranked["identity"]] = dict(
                    kind="BELOW_PROVEN_GROUP_MEAN_AND_MEDIAN"
                    if price < mean and price < median
                    else "PROVEN_GROUP_PRICE_TIE"
                    if mean == median == Decimal(str(stats["minimum"]))
                    else "PROVEN_GROUP_COMPARABLE",
                    group=group["group"],
                    compared_identities=[r["identity"] for r in group["ranking"]],
                    statistics=stats,
                    below_mean_percent=ranked["below_group_mean_percent"],
                    below_median_percent=((median - price) / median * 100).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    ),
                    semantics="CROSS_STORE_OPPORTUNITY_INDICATOR_NOT_HISTORICAL_DISCOUNT",
                )
        for row in analysis["candidates"]:
            decision = decisions[row["identity"]]
            priority = row["textual_review_priority"]
            proof_complete = row["status"] == "EQUIVALENT" and not decision["conflicting_evidence"]
            age = (
                aware_utc(now) - aware_utc(datetime.fromisoformat(str(row["observed_at"])))
            ).total_seconds()
            blockers = [] if proof_complete else ["DOCUMENTED_LISTING_OR_VARIANT_IDENTITY_REQUIRED"]
            if row["status"] == "REJECTED":
                blockers.append("INCOMPATIBLE_OR_FILTERED_LISTING")
            if row["price_status"] != "VALID_ANNOUNCED_PRICE":
                blockers.append("AMBIGUOUS_OR_INVALID_ANNOUNCED_PRICE")
            if not 0 <= age <= 600:
                blockers.append("CURRENT_PRICE_REVALIDATION_REQUIRED")
            blockers.extend(
                [
                    "APPLICABLE_PUBLIC_PRICE_EVIDENCE_REQUIRED",
                    "AVAILABILITY_EVIDENCE_REQUIRED",
                    "VERIFIED_OWN_AFFILIATE_LINK_REQUIRED",
                ]
            )
            existing = verified_quotes.get(fingerprint(row)) if 0 <= age <= 600 else None
            if existing:
                blockers = []
            queue.append(
                dict(
                    capture_id=capture["capture_id"],
                    query=analysis["query"],
                    **row,
                    opportunity=benchmarks.get(
                        row["identity"],
                        dict(
                            kind="UNPROVEN_PRICE_CANDIDATE",
                            statistics=None,
                            below_mean_percent=None,
                            below_median_percent=None,
                            semantics="TEXT_AND_ANNOUNCED_PRICE_ONLY_NO_COMPARABLE_BENCHMARK",
                        ),
                    ),
                    textual_confidence=priority,
                    discovery_state="CAPTURED",
                    classification_state="CLASSIFIED",
                    commercial_verification_state="VERIFIED_AT_QUEUE_ENTRY"
                    if existing
                    else "PENDING",
                    publication_state=existing["state"] if existing else "BLOCKED",
                    equivalence_proven=proof_complete,
                    only_commercial_evidence_missing=not existing
                    and proof_complete
                    and row["price_status"] == "VALID_ANNOUNCED_PRICE",
                    missing_identity_information=decision["missing_information"],
                    publication_blockers=blockers,
                    publication_authorized=False,
                )
            )
    queue.sort(
        key=lambda r: (
            r["status"] == "REJECTED",
            r["opportunity"]["kind"] != "BELOW_PROVEN_GROUP_MEAN_AND_MEDIAN",
            -Decimal(str(r["opportunity"]["below_mean_percent"] or 0)),
            -Decimal(str(r["opportunity"]["below_median_percent"] or 0)),
            -r["textual_confidence"]["score"],
            r["identity"],
            r["capture_id"],
        )
    )
    for index, row in enumerate(queue, 1):
        row["analysis_priority_rank"] = index
    queue = rank_rows(queue, now=now, signals_by_identity=signals_by_identity)
    return dict(
        analysis_queue=queue,
        proven_comparisons=comparisons,
        classification_state="COMPLETED",
        classified_at=now.isoformat(),
        captured_listings=len(queue),
        ranking_semantics="PROVEN_MEAN_AND_MEDIAN_THEN_TEXTUAL_TRIAGE_NOT_COMMERCIAL_APPROVAL",
        intelligent_ranking_semantics="100_POINTS_WITH_EXPLICIT_UNKNOWN_AND_COVERAGE_NOT_PUBLICATION_AUTHORITY",
        history_writes=0,
        publication_authorized=False,
    )
