"""Evidence-aware classifications and presentation. Title conflicts never certify product facts."""

from decimal import Decimal
from itertools import combinations
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.services.shopee_attributes import normalized_measure, title_inferences


class NetContent(BaseModel):
    """Documented content per sale unit; sale-unit count remains a separate attribute."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    amount: Decimal = Field(gt=0, le=1000000000000, max_digits=24, decimal_places=12)
    unit: Literal["mg", "g", "kg", "ml", "l"]

    @field_validator("amount", mode="before")
    @classmethod
    def exact_number(cls, value):
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise ValueError("Conteúdo exige número decimal exato, sem float.")
        return value

    @model_validator(mode="after")
    def canonical(self):
        amount, unit = normalized_measure(self.amount, self.unit)
        if amount > Decimal("1000000000000") or amount.as_tuple().exponent < -12:
            raise ValueError("Conteúdo normalizado fora dos limites exatos.")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "unit", unit)
        return self

    @field_serializer("amount")
    def amount_text(self, value):
        return format(value, "f")


def documented_title_conflicts(name, facts):
    hints = title_inferences(name)
    amounts = {
        (h["value"]["amount"], h["value"]["unit"])
        for h in hints.get("net_content", [])
        if h["value"]["role"] != "DOSE_MENTION"
    }
    issues = []
    standardized = "creatina" in facts.model.casefold() or any(
        h["rule"] == "PRODUCT_WORD_MENTION" for h in hints.get("product_role", [])
    )
    if standardized and facts.net_content is None:
        issues.append("NET_CONTENT_UNPROVEN")
    if standardized and facts.edition is None:
        issues.append("EDITION_UNPROVEN")
    if amounts:
        if facts.net_content is None:
            if "NET_CONTENT_UNPROVEN" not in issues:
                issues.append("NET_CONTENT_UNPROVEN")
        elif amounts != {(format(facts.net_content.amount, "f"), facts.net_content.unit)}:
            issues.append("AMBIGUOUS_OR_CONFLICTING_TITLE_CONTENT")
    editions = {h["value"] for h in hints.get("edition", [])}
    if editions:
        if facts.edition is None:
            if "EDITION_UNPROVEN" not in issues:
                issues.append("EDITION_UNPROVEN")
        elif editions != {facts.edition}:
            issues.append("AMBIGUOUS_OR_CONFLICTING_TITLE_EDITION")
    counts = {
        h["value"]
        for h in hints.get("quantity", [])
        if h["rule"]
        in {"EXPLICIT_UNIT_COUNT_MENTION", "MULTIPACK_COUNT_MENTION", "PACKAGE_COUNT_MENTION"}
    }
    if counts and counts != {facts.quantity}:
        issues.append("AMBIGUOUS_OR_CONFLICTING_TITLE_UNIT_COUNT")
    if "creatina" in facts.model.casefold() and any(
        h["value"] == "POSSIBLE_ACCESSORY_LISTING" for h in hints.get("product_role", [])
    ):
        issues.append("POSSIBLE_ACCESSORY_VERSUS_DOCUMENTED_PRODUCT")
    return issues


def title_differences(left, right):
    differences = []
    a, b = left["inferred_attributes"], right["inferred_attributes"]
    for field in (
        "brand",
        "model",
        "quantity",
        "kit",
        "variant",
        "edition",
        "usage",
        "product_role",
        "net_content",
    ):

        def values(hints, field=field):
            items = hints.get(field, [])
            if field == "net_content":
                return {
                    (h["value"]["amount"], h["value"]["unit"])
                    for h in items
                    if h["value"]["role"] != "DOSE_MENTION"
                }
            if field == "product_role":
                roles = {h["value"] for h in items}
                return (
                    {"POSSIBLE_ACCESSORY_LISTING"}
                    if "POSSIBLE_ACCESSORY_LISTING" in roles
                    else {"PRODUCT_MENTION"}
                    if "PRODUCT_MENTION" in roles
                    else roles
                )
            if field == "kit":
                return {
                    h["value"]
                    for h in items
                    if h["rule"] == "KIT_PHRASE_NOT_CONTENTS_PROOF"
                    and h["value"] not in {"kit", "combo"}
                }
            return {h["value"] for h in items}

        av, bv = values(a), values(b)
        if len(av) == len(bv) == 1 and av != bv:
            differences.append(
                dict(
                    attribute=field,
                    left=list(av)[0],
                    right=list(bv)[0],
                    evidence_level="EXPLICIT_TITLE_DIFFERENCE_NOT_PRODUCT_PROOF",
                )
            )
    return differences


def classified_presentation(rows, groups):
    comparable = [g for g in groups if g["distinct_shops"] >= 2]
    proven_groups = {g["group"] for g in comparable}
    incompatible = [
        r
        for r in rows
        if r.get("mismatched_attributes")
        and r.get("evidence_source") == "MANUAL_DOCUMENTED"
        and not r.get("evidence_conflicts")
    ]
    incompatible_ids = {r["identity"] for r in incompatible}
    for row in rows:
        row["equivalence_classification"] = (
            "PROVEN_INCOMPATIBLE"
            if row["identity"] in incompatible_ids
            else "PROVEN_EQUIVALENT"
            if row.get("group") in proven_groups and row["status"] == "EQUIVALENT"
            else "INCONCLUSIVE"
        )

    def present(row):
        if row["equivalence_classification"] == "PROVEN_EQUIVALENT":
            return row
        return {
            key: value
            for key, value in row.items()
            if key not in {"rank", "difference_from_lowest", "difference_from_lowest_percent"}
        }

    pairs = []
    truncated = False
    for left, right in combinations(rows, 2):
        if left["shop_id"] == right["shop_id"]:
            continue
        if len(pairs) >= 500:
            truncated = True
            break
        differences = []
        for field in left["attributes"]:
            av, bv = left["attributes"][field], right["attributes"].get(field)
            if av is not None and bv is not None and av != bv:
                differences.append(
                    dict(
                        attribute=field,
                        left=av,
                        right=bv,
                        scope="COMMERCIAL" if field == "price_conditions" else "PRODUCT",
                    )
                )
        documented = all(
            r.get("evidence_source") == "MANUAL_DOCUMENTED" and not r.get("evidence_conflicts")
            for r in (left, right)
        )
        classification = (
            "PROVEN_INCOMPATIBLE"
            if documented and differences
            else "PROVEN_EQUIVALENT"
            if left.get("group") and left.get("group") == right.get("group")
            else "INCONCLUSIVE"
        )
        pairs.append(
            dict(
                left_identity=left["identity"],
                right_identity=right["identity"],
                classification=classification,
                proven_differences=differences if documented else [],
                title_differences=title_differences(left, right),
            )
        )
    shops = {}
    for row in rows:
        shop = shops.setdefault(
            row["shop_id"], dict(shop_id=row["shop_id"], listings=[], shop_name_variants=[])
        )
        shop["listings"].append(present(row))
        name = row["seller"].get("shop_name")
        if name and name not in shop["shop_name_variants"]:
            shop["shop_name_variants"].append(name)
    return dict(
        proven_comparisons=comparable,
        proven_incompatible_products=incompatible,
        unproven_candidates=[
            present(r)
            for r in rows
            if r["identity"] not in incompatible_ids
            and r["equivalence_classification"] == "INCONCLUSIVE"
        ],
        filtered_candidates=[
            r for r in rows if r["status"] == "REJECTED" and r["identity"] not in incompatible_ids
        ],
        stores=list(shops.values()),
        pairwise_assessments=pairs,
        pairwise_assessments_truncated=truncated,
        lowest_price_highlights=[
            dict(
                group=g["group"],
                identities=g["lowest_announced_identities"],
                announced_price=g["ranking"][0]["announced_price"],
                semantics="LOWEST_ANNOUNCED_IN_PROVEN_GROUP_NOT_FINAL_PRICE",
            )
            for g in comparable
        ],
    )
