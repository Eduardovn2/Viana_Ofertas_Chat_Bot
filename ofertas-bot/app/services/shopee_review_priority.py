"""Deterministic textual review priority, never probability or commercial proof."""

from app.services.categories import normalize_text


def prioritize_for_review(rows, query, target=None):
    words = set(normalize_text(query).split())
    queue = []
    for row in rows:
        overlap = len(words & set(normalize_text(row["name"]).split()))
        components = {"query_word_overlap": 50 * overlap // max(1, len(words))}
        hints = row["inferred_attributes"]
        components["brand_mention"] = (
            15
            if any(target is None or h["value"] == target.brand for h in hints.get("brand", []))
            else 0
        )
        components["model_mention"] = (
            15
            if any(target is None or h["value"] == target.model for h in hints.get("model", []))
            else 0
        )
        for field in ("quantity", "net_content", "edition", "variant"):
            mentions = hints.get(field, [])
            if field == "net_content":
                mentions = [h for h in mentions if h["value"]["role"] != "DOSE_MENTION"]
            components[field + "_mention"] = 5 if mentions else 0
        warnings = []
        if any(h["value"] == "POSSIBLE_ACCESSORY_LISTING" for h in hints.get("product_role", [])):
            components["possible_accessory_penalty"] = -30
            warnings.append("POSSIBLE_ACCESSORY")
        contents = {
            str(h["value"])
            for h in hints.get("net_content", [])
            if h["value"]["role"] != "DOSE_MENTION"
        }
        if len(contents) > 1:
            components["ambiguous_content_penalty"] = -20
            warnings.append("MULTIPLE_CONTENT_MENTIONS")
        if row.get("evidence_conflicts"):
            components["document_conflict_penalty"] = -30
            warnings.append("DOCUMENT_REQUIRES_REVIEW")
        if target is not None:
            if target.net_content is not None and len(contents) == 1:
                measurements = [
                    h["value"]
                    for h in hints.get("net_content", [])
                    if h["value"]["role"] != "DOSE_MENTION"
                ]
                expected_content = (format(target.net_content.amount, "f"), target.net_content.unit)
                matches = {(h["amount"], h["unit"]) for h in measurements} == {expected_content}
                components["content_target_match"] = 10 if matches else -20
                if not matches:
                    warnings.append("POSSIBLE_TARGET_CONTENT_MISMATCH")
            for field in ("quantity", "edition", "model"):
                expected = getattr(target, field)
                values = {h["value"] for h in hints.get(field, [])}
                if expected is not None and len(values) == 1 and expected not in values:
                    components[field + "_mismatch_penalty"] = -20
                    warnings.append("POSSIBLE_TARGET_" + field.upper() + "_MISMATCH")
        score = max(0, min(100, sum(components.values())))
        priority = dict(
            score=score,
            band="HIGH" if score >= 80 else "MEDIUM" if score >= 50 else "LOW",
            components=components,
            warnings=warnings,
            version="TEXT_REVIEW_V1",
            semantics="HEURISTIC_REVIEW_PRIORITY_NOT_PROBABILITY_OR_EQUIVALENCE",
        )
        row["textual_review_priority"] = priority
        if row["equivalence_classification"] == "INCONCLUSIVE" and row["status"] != "REJECTED":
            queue.append(
                dict(
                    identity=row["identity"],
                    shop_id=row["shop_id"],
                    name=row["name"],
                    priority=priority,
                    unknown_attributes=row["unknown_attributes"],
                    observation_fingerprint=row.get("observation_fingerprint"),
                    quote_fingerprint=row.get("quote_fingerprint"),
                )
            )
    return sorted(queue, key=lambda entry: (-entry["priority"]["score"], entry["identity"]))
