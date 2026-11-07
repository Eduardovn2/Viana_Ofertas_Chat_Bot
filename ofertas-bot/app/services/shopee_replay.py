"""Read-only analysis of sanitized Shopee observations, without reconstructing raw API data."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError
from app.integrations.shopee import ShopeeConnector, _positive_id
from app.services.comparison import aware_utc, valid_money
from app.services.discovery_relevance import SearchTerm, relevance
from app.services.shopee_multistore import (
    ProductFacts,
    ProductTarget,
    _comparison_result,
    _multistore_exclusions,
    assess_documented_row,
)
from app.services.shopee_opportunities import attach_opportunities


class SanitizedListing(BaseModel):
    # Saved reports also contain legacy assessments. They are deliberately ignored.
    model_config = ConfigDict(extra="ignore")
    identity: str
    shop_id: str
    item_id: str
    name: str = Field(min_length=1, max_length=500)
    announced_price: Decimal | None = None
    currency: Literal["BRL"]
    observed_at: datetime
    canonical_url: str | None = None
    source: str | None = None
    image_url: str | None = None
    provider_observation: dict[str, Any] = Field(default_factory=dict)
    seller: dict[str, Any] = Field(default_factory=dict)
    price_status: Literal["VALID_ANNOUNCED_PRICE", "AMBIGUOUS_OR_INVALID_PRICE"] = (
        "AMBIGUOUS_OR_INVALID_PRICE"
    )

    @field_validator("shop_id", "item_id", mode="before")
    @classmethod
    def positive_identity(cls, value):
        try:
            return _positive_id(value)
        except IntegrationError:
            raise ValueError("Identificador Shopee sanitizado inválido.") from None

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Nome sanitizado inválido.")
        return value.strip()

    @field_validator("announced_price", mode="before")
    @classmethod
    def decimal_encoding(cls, value):
        if value is not None and not isinstance(value, (str, Decimal)):
            raise ValueError("Preço sanitizado exige string decimal, sem float ou bool.")
        return value

    @field_validator("announced_price")
    @classmethod
    def valid_price(cls, value):
        if value is not None and not valid_money(value):
            raise ValueError("Preço sanitizado inválido.")
        return value.quantize(Decimal("0.01")) if value is not None else None

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)

    @model_validator(mode="after")
    def consistent(self):
        if self.identity != f"{self.shop_id}:{self.item_id}":
            raise ValueError("Identidade sanitizada diverge de shopId:itemId.")
        if self.price_status == "VALID_ANNOUNCED_PRICE" and self.announced_price is None:
            raise ValueError("Preço válido declarado sem valor disponível.")
        return self


def analyze_sanitized_comparison(comparison, query, *, target=None, documented_reviews=None):
    term = SearchTerm(query=query)
    target = ProductTarget.model_validate(target) if target is not None else None
    raw = comparison.get("candidates")
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("Comparação sanitizada exige lista limitada de candidatos.")
    listings = [SanitizedListing.model_validate(row) for row in raw]
    unique = {row.identity: row for row in listings}
    if len(unique) != len(listings):
        raise ValueError("Anúncios sanitizados duplicados.")
    rejected = comparison.get("incompatible_products", [])
    if not isinstance(rejected, list) or len(rejected) > 100:
        raise ValueError("Lista de incompatíveis inválida.")
    for raw_row in rejected:
        row = SanitizedListing.model_validate(raw_row)
        if row.identity in unique:
            if row != unique[row.identity]:
                raise ValueError("Cópias conflitantes do mesmo anúncio.")
        else:
            listings.append(row)
            unique[row.identity] = row
    if len(listings) > 100:
        raise ValueError("Limite total de anúncios sanitizados excedido.")
    if len({row.observed_at for row in listings}) > 1:
        raise ValueError("Comparação transversal exige a mesma coleta.")
    rows, groups = [], {}
    comparison_hash = fingerprint(comparison)
    for listing in listings:
        state, _, reason = relevance(listing.name, term, _multistore_exclusions(query))
        info = listing.seller
        seller = ShopeeConnector._seller_information(
            dict(
                shopName=info.get("shop_name"),
                ratingStar=info.get("rating_star"),
                sales=info.get("sales"),
                shopType=info.get("shop_types"),
            )
        )
        binding = listing.model_dump(mode="json")
        # New optional preservation fields must not invalidate a legacy review
        # merely because its old recording never contained those fields.
        for optional in ("image_url", "provider_observation"):
            if optional not in listing.model_fields_set:
                binding.pop(optional, None)
        observation_hash = fingerprint({**binding, "seller": seller})
        row = dict(
            identity=listing.identity,
            shop_id=listing.shop_id,
            item_id=listing.item_id,
            name=listing.name,
            canonical_url=listing.canonical_url,
            source=listing.source,
            image_url=listing.image_url,
            provider_observation=listing.provider_observation,
            announced_price=listing.announced_price,
            currency=listing.currency,
            observed_at=listing.observed_at,
            quote_fingerprint=None,
            observation_fingerprint=observation_hash,
            seller=seller,
            attributes=dict.fromkeys(ProductFacts.model_fields),
            unknown_attributes=list(ProductFacts.model_fields),
            price_status=listing.price_status,
            price_validation_source="RECORDED_CONNECTOR_ASSESSMENT_RAW_RANGE_NOT_AVAILABLE",
            status="REJECTED" if state == "FILTERED" else "CANDIDATE",
            reason=reason if state != "SELECTED" else "EQUIVALENCE_UNPROVEN",
        )
        review = (documented_reviews or {}).get(listing.identity)
        if review:
            row["documentary_review"] = dict(
                state=review.get("state"),
                revision=review.get("revision"),
                pending_fields=sorted(
                    k for k, v in review.get("facts", {}).items() if v is not None
                )
                if review.get("state") == "DRAFT"
                else [],
                source_current=review.get("observation_fingerprint") == observation_hash
                and review.get("source_comparison_sha256") == comparison_hash,
            )
        if review and review.get("state") == "DOCUMENTED" and state != "FILTERED":
            proof = review.get("proof", {})
            try:
                if (
                    review.get("state") != "DOCUMENTED"
                    or review.get("evidence_sha256") != fingerprint(proof)
                    or proof["observation_fingerprint"] != observation_hash
                    or proof["source_comparison_sha256"] != comparison_hash
                    or aware_utc(datetime.fromisoformat(proof["confirmed_at"]))
                    < listing.observed_at
                ):
                    raise ValueError("Revisão pertence a outra observação.")
                facts = ProductFacts.model_validate(proof["facts"])
                row.update(
                    attributes=facts.model_dump(),
                    unknown_attributes=[k for k, v in facts.model_dump().items() if v is None],
                    evidence_source="MANUAL_DOCUMENTED",
                    evidence_actor=proof["actor"],
                    evidence_reference="administrative_review:" + proof["identity"],
                    evidence_binding_kind="SANITIZED_OBSERVATION_NOT_RAW_QUOTE",
                    documentary_sources=proof["field_sources"],
                    documentary_validation="ADMIN_DECLARED_NOT_FETCHED_OR_PLATFORM_CONFIRMED",
                )
                key = assess_documented_row(row, facts, target, state, reason)
                if key:
                    groups.setdefault(key, []).append(row)
            except (ValueError, KeyError, TypeError):
                row["review_issue"] = "INVALID_OR_STALE_DOCUMENTED_REVIEW"
        rows.append(row)
    result = _comparison_result(rows, query, target, groups)
    result.update(
        mode="SANITIZED_REPLAY_NO_HTTP",
        external_http_calls=0,
        source_comparison_sha256=fingerprint(comparison),
        proven_equivalences=result["proven_comparisons"],
        raw_price_range_revalidated=False,
    )
    return result


def analyze_recorded_search(report, *, target=None, documented_reviews=None):
    if report.get("status") != "SUCCEEDED":
        raise ValueError("Não há consulta concluída com sucesso para reanalisar.")
    comparison = report.get("result", {}).get("comparison")
    scope = report.get("scope", {})
    if not isinstance(comparison, dict) or comparison.get("query") != scope.get("keyword"):
        raise ValueError("Resultado ausente ou palavra-chave divergente do registro.")
    result = analyze_sanitized_comparison(
        comparison, scope["keyword"], target=target, documented_reviews=documented_reviews
    )
    return attach_opportunities(
        dict(
            **result,
            source_authorization_id=report.get("authorization_id"),
            source_scope_sha256=report.get("scope_sha256"),
            observation_mode=report.get("mode"),
            authorization_granted=False,
        )
    )
