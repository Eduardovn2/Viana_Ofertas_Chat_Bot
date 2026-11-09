"""Observational cross-store comparison, separate from per-item price history.

Names establish relevance only. Equivalence needs documented, exact-quote evidence.
No operation here creates an offer, history, link, approval or publication.
"""

import hashlib
import json
import unicodedata
from copy import deepcopy
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select, text

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError
from app.integrations.registry import ConnectorRegistry
from app.integrations.shopee import ENDPOINT, ShopeeConnector, _positive_id
from app.models import AppSetting, IntegrationEvent, Platform, ShopeeCollectionRun
from app.services.categories import normalize_text
from app.services.comparison import aware_utc, valid_money
from app.services.discovery_operations import DiscoveryOperations
from app.services.discovery_relevance import SearchTerm, relevance
from app.services.leases import conflict_insert
from app.services.shopee_attributes import ATTRIBUTE_COVERAGE, title_inferences
from app.services.shopee_classification import (
    NetContent,
    classified_presentation,
    documented_title_conflicts,
)
from app.services.shopee_opportunities import attach_opportunities
from app.services.shopee_review_priority import prioritize_for_review
from app.services.shopee_single_read import ReadApproval, ReadConfirmation, ShopeeSingleReadAdapter
from app.services.simulation_safety import require_simulation_scope

PREFIX = "shopee_multistore:"
# Keys that carry product or affiliate URLs; stripped from the durable sanitized result.
_URL_KEYS = frozenset({"canonical_url", "productLink", "url"})


def _known_fact(value):
    # Relevance tokenization drops punctuation: X+ and X would collapse to the
    # same model. Proven facts retain those distinctions when grouping products.
    value = " ".join(unicodedata.normalize("NFC", value).casefold().split())
    if normalize_text(value) in {
        "",
        "unknown",
        "desconhecido",
        "unconfirmed",
        "n a",
        "nao informado",
        "nao confirmado",
    }:
        raise ValueError("Atributo não comprovado.")
    return value


class ProductTarget(BaseModel):
    """Search intent. Optional dimensions stay unknown until evidence supplies them."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    brand: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    quantity: int = Field(strict=True, ge=1, le=10000)
    condition: str | None = Field(default=None, min_length=1, max_length=100)
    packaging: str | None = Field(default=None, min_length=1, max_length=100)
    kit: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=30)
    variant: str | None = Field(default=None, min_length=1, max_length=100)
    price_conditions: str | None = Field(default=None, min_length=1, max_length=500)
    net_content: NetContent | None = None
    edition: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator(
        "brand", "model", "condition", "packaging", "variant", "price_conditions", "edition"
    )
    @classmethod
    def known(cls, value):
        return _known_fact(value) if value is not None else None

    @field_validator("edition")
    @classmethod
    def edition_name(cls, value):
        return {
            "comum": "standard",
            "tradicional": "standard",
            "padrao": "standard",
            "padrão": "standard",
        }.get(value, value)

    @field_validator("kit")
    @classmethod
    def contents(cls, value):
        if value is None:
            return None
        result = tuple(sorted(_known_fact(v) for v in value))
        if len(set(result)) != len(result):
            raise ValueError("Conteúdo do kit inválido.")
        return result

    def mismatches(self, facts):
        return [
            field
            for field, expected in self.model_dump(exclude_none=True).items()
            if facts.model_dump()[field] is not None and facts.model_dump()[field] != expected
        ]

    def search_query(self):
        return SearchTerm(query=f"{self.brand} {self.model}").query


class ProductFacts(ProductTarget):
    """Complete attested attributes, rather than defaults copied from search intent."""

    condition: str = Field(min_length=1, max_length=100)
    packaging: str = Field(min_length=1, max_length=100)
    kit: tuple[str, ...] = Field(min_length=1, max_length=30)
    variant: str = Field(min_length=1, max_length=100)
    price_conditions: str = Field(min_length=1, max_length=500)


class EquivalenceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    facts: ProductFacts
    source: Literal["MANUAL_DOCUMENTED"]
    quote_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    reference: str = Field(min_length=3, max_length=500)
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    verified_at: datetime

    @field_validator("verified_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)


def compare_offers(page, query, *, evidence=None, target=None, exclusions=(), intent="generic"):
    """Return candidates and proven groups; never a verified final-price winner."""
    term = SearchTerm(query=query, intent=intent)
    exclusions = _multistore_exclusions(query, exclusions)
    evidence = evidence or {}
    target = ProductTarget.model_validate(target) if target is not None else None
    if len({aware_utc(s.observed_at) for s in page.items}) > 1:
        raise ValueError("Comparação transversal exige a mesma coleta, não histórico misturado.")
    if len({s.external_item_id for s in page.items}) != len(page.items):
        raise ValueError("A comparação contém identidades de anúncio duplicadas.")
    rows, groups = [], {}
    for snapshot in page.items:
        if snapshot.platform != Platform.SHOPEE or snapshot.source != "shopee:official_api":
            raise ValueError("Fonte incompatível com a comparação Shopee.")
        try:
            shop, item = snapshot.external_item_id.split(":")
            _positive_id(shop)
            _positive_id(item)
        except (ValueError, IntegrationError):
            raise ValueError("Identidade Shopee inválida na comparação.") from None
        state, _, reason = relevance(snapshot.name, term, exclusions)
        row = dict(
            identity=snapshot.external_item_id,
            shop_id=shop,
            item_id=item,
            name=snapshot.name,
            canonical_url=snapshot.canonical_url,
            image_url=snapshot.image_url,
            source=snapshot.source,
            source_reference=snapshot.source_reference,
            announced_price=snapshot.current_price,
            reported_price_min=snapshot.reported_price_min,
            reported_price_max=snapshot.reported_price_max,
            currency=snapshot.currency,
            observed_at=snapshot.observed_at,
            quote_fingerprint=snapshot.raw_fingerprint,
            seller=snapshot.evidence_metadata.get("seller_information", {}),
            documented_ranking_signals=snapshot.evidence_metadata.get("documented_ranking_signals"),
            provider_observation={
                k: snapshot.evidence_metadata.get(k)
                for k in (
                    "price_context",
                    "price_basis",
                    "commission_rate",
                    "commission_amount",
                    "advertised_discount_percent",
                    "offer_period_start_raw",
                    "offer_period_end_raw",
                    "period_time_unit",
                    "period_semantics",
                    "affiliate_link_candidate",
                )
                if k in snapshot.evidence_metadata
            },
            attributes=dict.fromkeys(ProductFacts.model_fields),
            unknown_attributes=list(ProductFacts.model_fields),
            price_status="VALID_ANNOUNCED_PRICE"
            if _comparable_announced_price(snapshot)
            else "AMBIGUOUS_OR_INVALID_PRICE",
            status="CANDIDATE",
            reason="EQUIVALENCE_UNPROVEN",
        )
        if state == "FILTERED":
            row.update(status="REJECTED", reason=reason)
        elif (proof_data := evidence.get(snapshot.external_item_id)) is not None:
            try:
                proof = EquivalenceEvidence.model_validate(proof_data)
            except ValidationError:
                row.update(reason="INCOMPLETE_EQUIVALENCE_EVIDENCE")
                rows.append(row)
                continue
            if (
                proof.quote_fingerprint == snapshot.raw_fingerprint
                and proof.verified_at >= aware_utc(snapshot.observed_at)
            ):
                row.update(
                    attributes=proof.facts.model_dump(),
                    unknown_attributes=[
                        key for key, value in proof.facts.model_dump().items() if value is None
                    ],
                    evidence_reference=proof.reference,
                    evidence_actor=proof.actor,
                    evidence_source=proof.source,
                )
            if proof.quote_fingerprint != snapshot.raw_fingerprint:
                row.update(reason="EVIDENCE_FOR_DIFFERENT_QUOTE")
            elif proof.verified_at < aware_utc(snapshot.observed_at):
                row.update(reason="EVIDENCE_PREDATES_QUOTE")
            else:
                key = assess_documented_row(row, proof.facts, target, state, reason)
                if key:
                    groups.setdefault(key, []).append(row)
        elif state != "SELECTED":
            row.update(reason=reason)
        rows.append(row)
    return _comparison_result(rows, query, target, groups)


def assess_documented_row(row, facts, target, state, reason):
    """Shared equivalence gate for raw-quote evidence and documented observation reviews."""
    if issues := documented_title_conflicts(row["name"], facts):
        row.update(reason="DOCUMENTED_ATTRIBUTES_REQUIRE_REVIEW", evidence_conflicts=issues)
    elif target is not None and (
        target.net_content is not None
        and facts.net_content is None
        or target.edition is not None
        and facts.edition is None
    ):
        row.update(reason="REQUESTED_ATTRIBUTE_UNPROVEN")
    elif target is not None and (mismatches := target.mismatches(facts)):
        row.update(
            status="REJECTED",
            reason="INCOMPATIBLE_PRODUCT_OR_CONDITIONS",
            mismatched_attributes=mismatches,
        )
    elif state != "SELECTED":
        row.update(reason=reason)
    elif row["price_status"] != "VALID_ANNOUNCED_PRICE":
        row.update(reason="AMBIGUOUS_OR_INVALID_PRICE")
    else:
        key = fingerprint(facts.model_dump())
        row.update(status="EQUIVALENT", reason="DOCUMENTED_EQUIVALENCE", group=key)
        return key
    return None


def _multistore_exclusions(query, exclusions=()):
    accessory_terms = {"capa", "pelicula", "adaptador", "carregador", "cabo", "reposicao"}
    return tuple(normalize_text(part) for part in exclusions) + tuple(
        sorted(accessory_terms - set(normalize_text(query).split()))
    )


def _comparison_result(rows, query, target=None, groups=None):
    groups = groups or {}
    for row in rows:
        row["inferred_attributes"] = title_inferences(row["name"], target)
        row["attribute_evidence_level"] = (
            "DOCUMENTED_FACTS_REQUIRE_REVIEW"
            if row.get("evidence_conflicts")
            else "DOCUMENTED_QUOTE_BOUND_FACTS"
            if row.get("evidence_source") == "MANUAL_DOCUMENTED"
            else "UNPROVEN"
        )
        row["reported_fields"] = dict(
            shopId=row["shop_id"],
            itemId=row["item_id"],
            productName=row["name"],
            announced_price=row["announced_price"],
            currency=row["currency"],
            seller_information=row["seller"],
        )
    ranked = []
    for key, candidates in groups.items():
        # Keep the cheapest comparable listing per shop, not an arbitrary first listing.
        unique = {}
        for row in sorted(candidates, key=lambda r: (r["announced_price"], r["identity"])):
            unique.setdefault(row["shop_id"], row)
        sellers = list(unique.values())
        minimum = sellers[0]["announced_price"]
        prices = sorted(r["announced_price"] for r in sellers)
        count = len(prices)
        mean = sum(prices, Decimal(0)) / count
        median = (prices[(count - 1) // 2] + prices[count // 2]) / 2
        for row in sellers:
            row.update(
                rank=1
                + sum(other["announced_price"] < row["announced_price"] for other in sellers),
                difference_from_lowest=(row["announced_price"] - minimum).quantize(Decimal("0.01")),
                difference_from_lowest_percent=(
                    (row["announced_price"] - minimum) / minimum * 100
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                below_group_mean_percent=((mean - row["announced_price"]) / mean * 100).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if count >= 2
                else None,
            )
        ranked.append(
            dict(
                group=key,
                distinct_shops=len(sellers),
                price_statistics=dict(
                    sample_size=count,
                    minimum=minimum,
                    mean=mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    median=median.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    currency="BRL",
                    semantics="SAME_CAPTURE_PROVEN_EQUIVALENCE_ONE_QUOTE_PER_SHOP_NOT_HISTORICAL_DISCOUNT",
                )
                if count >= 2
                else None,
                ranking=sellers,
                spread_amount=(sellers[-1]["announced_price"] - minimum).quantize(Decimal("0.01")),
                spread_semantics="CROSS_STORE_ANNOUNCED_PRICE_NOT_HISTORICAL_DISCOUNT",
                lowest_announced_identities=[
                    r["identity"] for r in sellers if r["announced_price"] == minimum
                ]
                if len(sellers) >= 2
                else [],
                status="COMPARABLE_ANNOUNCED_QUOTES"
                if len(sellers) >= 2
                else "INSUFFICIENT_STORES",
            )
        )
    candidate_order = _candidate_price_order(rows)
    usages = {hint["value"] for row in rows for hint in row["inferred_attributes"].get("usage", [])}
    warnings = []
    if len(usages) > 1:
        warnings.append("MIXED_USAGE_MENTIONS_REQUIRES_REVIEW")
    if candidate_order:
        minimum = candidate_order[0]["announced_price"]
        lowest = [r for r in rows if r["status"] != "REJECTED" and r["announced_price"] == minimum]
        if (
            any(r["status"] != "EQUIVALENT" for r in lowest)
            or len({r.get("group") for r in lowest}) != 1
            or not any(
                g["distinct_shops"] >= 2 and g["group"] == lowest[0].get("group") for g in ranked
            )
        ):
            warnings.append("LOWEST_CANDIDATE_PRICE_EQUIVALENCE_UNPROVEN")
    presentation = classified_presentation(rows, ranked)
    review_queue = prioritize_for_review(rows, query, target)
    priorities = {r["identity"]: r["textual_review_priority"] for r in rows}
    for row in presentation["unproven_candidates"]:
        row["textual_review_priority"] = priorities[row["identity"]]
    for store in presentation["stores"]:
        for row in store["listings"]:
            row["textual_review_priority"] = priorities[row["identity"]]
    return attach_opportunities(
        dict(
            **presentation,
            review_queue=review_queue,
            review_priority_semantics="TEXTUAL_TRIAGE_ONLY_NOT_COMMERCIAL_PROOF",
            query=query,
            requested_product=target.model_dump(exclude_none=True) if target else None,
            candidates=rows,
            incompatible_products=[r for r in rows if r["status"] == "REJECTED"],
            inconclusive_products=[r for r in rows if r["status"] == "CANDIDATE"],
            groups=ranked,
            # This ordering is informational only; unequal/unproven products have no
            # price difference or winner. Proven groups above carry those comparisons.
            candidate_price_order=candidate_order,
            candidate_ranking_semantics="PRICE_ORDER_ONLY_NOT_EQUIVALENCE_OR_BUY_RECOMMENDATION",
            all_listing_price_order=[
                dict(
                    identity=r["identity"],
                    shop_id=r["shop_id"],
                    announced_price=r["announced_price"],
                )
                for r in sorted(
                    (
                        r
                        for r in rows
                        if r["status"] != "REJECTED"
                        and r["price_status"] == "VALID_ANNOUNCED_PRICE"
                    ),
                    key=lambda r: (r["announced_price"], r["identity"]),
                )
            ],
            distinct_shops=len({r["shop_id"] for r in rows}),
            returned_listings=len(rows),
            review_warnings=warnings,
            attribute_coverage=deepcopy(ATTRIBUTE_COVERAGE),
            status="COMPARABLE_ANNOUNCED_QUOTES"
            if any(g["distinct_shops"] >= 2 for g in ranked)
            else "INSUFFICIENT_EQUIVALENCE",
            price_semantics="ANNOUNCED_NOT_CONFIRMED_FINAL",
            comparison_kind="CROSS_STORE_SAME_OBSERVATION",
            commercial_unknowns=["final_price", "shipping", "stock", "seller_reputation"],
            publication_authorized=False,
            history_writes=0,
        )
    )


def _comparable_announced_price(snapshot):
    return (
        snapshot.currency == "BRL"
        and valid_money(snapshot.current_price)
        and valid_money(snapshot.reported_price_min)
        and valid_money(snapshot.reported_price_max)
        and snapshot.reported_price_min == snapshot.reported_price_max == snapshot.current_price
    )


def _candidate_price_order(rows):
    unique = {}
    valid = [
        row
        for row in rows
        if row["status"] != "REJECTED" and row["price_status"] == "VALID_ANNOUNCED_PRICE"
    ]
    for row in sorted(valid, key=lambda r: (r["announced_price"], r["identity"])):
        unique.setdefault(row["shop_id"], row)
    return [
        dict(
            identity=row["identity"],
            shop_id=row["shop_id"],
            announced_price=row["announced_price"],
            currency="BRL",
            equivalence_status=row["status"],
            rank=1
            + sum(other["announced_price"] < row["announced_price"] for other in unique.values()),
            name=row["name"],
            seller=row["seller"],
            same_shop_listing_count=sum(r["shop_id"] == row["shop_id"] for r in valid),
            same_price_identities=[
                r["identity"]
                for r in valid
                if r["shop_id"] == row["shop_id"] and r["announced_price"] == row["announced_price"]
            ],
        )
        for row in unique.values()
    ]


class ShopeeMultistoreService:
    def __init__(self, collection):
        self.collection = collection
        self.single = ShopeeSingleReadAdapter(collection)

    def discover_offline(self, query, connector, *, limit=3):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Limite offline deve ser inteiro entre 1 e 100.")
        self.single._mode(True)
        with self.collection.factory() as session:
            require_simulation_scope(session, self.collection.settings)
            preferences, _ = DiscoveryOperations().load(session)
            if (
                preferences.max_queries_per_period < 1
                or preferences.max_results_per_evaluation < limit
            ):
                raise ValueError("Preferências locais não permitem esta coleta mockada.")
        SearchTerm(query=query)
        if (
            type(connector) is not ShopeeConnector
            or type(connector.transport.client._transport_for_url(httpx.URL(ENDPOINT)))
            is not httpx.MockTransport
        ):
            raise ValueError("Comparação offline exige conector com MockTransport.")
        return connector.discover_offers(query, page=1, limit=limit, profile="multistore")

    def prepare_search(self, authorization_id, *, actor, offline=False):
        """Durable PREPARED only; this stage exposes no real dispatcher/approval."""
        self.single._mode(offline)
        identity = str(UUID(str(authorization_id)))
        # Validate actor without inventing any equivalence evidence.
        from app.services.shopee_single_read import ReadConfirmation

        ReadConfirmation(scope_sha256="0" * 64, actor=actor, confirmed_single_read=True)
        query = ShopeeConnector.discovery_query(
            "Cockpit simulador Brave Bull", page=1, limit=3, profile="multistore"
        )
        from app.integrations.registry import ConnectorRegistry

        registry = ConnectorRegistry(
            self.collection.settings, session_factory=self.collection.factory
        )
        try:
            if not registry.configured(Platform.SHOPEE) or registry.is_blocked(Platform.SHOPEE):
                raise ValueError("Conta ausente ou bloqueada.")
            account = registry.credential_scope(Platform.SHOPEE)
        finally:
            registry.close()
        scope = dict(
            authorization_id=identity,
            mode="MOCK_ONLY" if offline else "REAL_SEARCH_PREPARED",
            actor=actor,
            credential_scope=account,
            endpoint=ENDPOINT,
            operation="productOfferV2",
            keyword="Cockpit simulador Brave Bull",
            page=1,
            limit=3,
            max_http_calls=1,
            payload_sha256=fingerprint({"query": query}),
            profile="multistore",
            retry=False,
            fallback=False,
            history_writes=0,
            publication_authorized=False,
        )
        value = dict(
            state="PREPARED",
            scope=scope,
            scope_sha256=fingerprint(scope),
            consumed=False,
            approval=None,
        )
        with self.collection.factory() as session:
            if not offline and session.execute(
                text("SELECT current_database(),current_schema()")
            ).one() != ("ofertas_bot", "public"):
                raise ValueError("Preparação real exige banco operacional correto.")
            if session.scalar(
                select(AppSetting.id).where(
                    AppSetting.key.in_(
                        ["shopee_identity_read:" + identity, "shopee_recurring:" + identity]
                    )
                )
            ):
                raise ValueError("UUID já pertence a outra autorização.")
            inserted = session.execute(
                conflict_insert(session, AppSetting)
                .values(
                    key=PREFIX + identity,
                    value=value,
                    description="Busca multiloja preparada; sem permissão de despacho.",
                )
                .on_conflict_do_nothing(index_elements=[AppSetting.key])
                .returning(AppSetting.id)
            ).scalar_one_or_none()
            row = self.single._row(session, PREFIX + identity)
            if row.value != value:
                raise ValueError("Escopo existente incompatível.")
            if inserted is not None:
                session.add(
                    IntegrationEvent(
                        platform=Platform.SHOPEE,
                        operation="multistore_search_prepared",
                        status="PREPARED",
                        source_reference=PREFIX + identity,
                        observed_at=self.collection.clock(),
                    )
                )
            session.commit()
        return dict(authorization_id=identity, **value, payload={"query": query})

    def execute_real(self, *args, **kwargs):
        raise ValueError(
            "Busca real bloqueada: exige autorização posterior e despacho administrativo com claim."
        )


class MultistoreSearchScope(BaseModel):
    """First bounded search only; changing its scope requires a new authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    authorization_id: UUID
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    operation: Literal["productOfferV2"] = "productOfferV2"
    keyword: str = Field(default="Cockpit simulador Brave Bull", min_length=1, max_length=100)
    page: int = Field(default=1, strict=True, ge=1, le=1)
    limit: int = Field(default=3, strict=True, ge=3, le=3)
    not_before: datetime
    expires_at: datetime

    @field_validator("keyword")
    @classmethod
    def bounded_keyword(cls, value):
        return SearchTerm(query=value).query

    @field_validator("not_before", "expires_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)

    @model_validator(mode="after")
    def ordered(self):
        if self.expires_at <= self.not_before:
            raise ValueError("Janela vazia ou invertida.")
        return self


class ShopeeMultistoreAdapter(ShopeeSingleReadAdapter):
    """Specialize the existing durable one-shot dispatcher; no ingestion pipeline."""

    prefix = "shopee_multistore_search:"
    reservation_prefix = "multistore-search:"
    reservation_operation = "MULTISTORE_NO_INGEST"

    @staticmethod
    def key(authorization_id):
        return ShopeeMultistoreAdapter.prefix + str(UUID(str(authorization_id)))

    def _mode(self, offline):
        super()._mode(offline)
        if not offline:
            with self.factory() as session:
                if session.execute(text("SELECT current_database(),current_schema()")).one() != (
                    "ofertas_bot",
                    "public",
                ):
                    raise ValueError("Busca real exige banco operacional correto.")
        return "MOCK_ONLY" if offline else "REAL_MULTISTORE_SEARCH"

    @staticmethod
    def _credential_binding(app_id, secret):
        if not app_id or not secret:
            raise ValueError("Credencial Shopee não configurada.")
        return fingerprint(
            {
                "app_id": app_id.get_secret_value(),
                "secret": secret.get_secret_value(),
            }
        )

    def _scope(self, session, request, mode):
        preferences, _ = DiscoveryOperations().load(session)
        if (
            preferences.max_queries_per_period < 1
            or preferences.max_results_per_evaluation < request.limit
        ):
            raise ValueError("Preferências locais não permitem esta busca limitada.")
        query = ShopeeConnector.discovery_query(
            request.keyword, page=request.page, limit=request.limit, profile="multistore"
        )
        body = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":")).encode()
        registry = ConnectorRegistry(self.settings, session_factory=self.factory)
        try:
            if not registry.configured(Platform.SHOPEE) or registry.is_blocked(Platform.SHOPEE):
                raise ValueError("Conta ausente ou bloqueada.")
        finally:
            registry.close()
        return (
            {
                **request.model_dump(mode="json"),
                "mode": mode,
                "endpoint": ENDPOINT,
                "credential_scope": self._credential_binding(
                    self.settings.shopee_app_id, self.settings.shopee_secret
                ),
                "payload_sha256": hashlib.sha256(body).hexdigest(),
                "profile": "multistore",
                "max_http_calls": 1,
                "retry": False,
                "fallback": False,
                "pagination": False,
                "history_writes": 0,
                "publication_authorized": False,
            },
            request,
            query,
        )

    def _audit(self, session, value, operation, status):
        session.add(
            IntegrationEvent(
                platform=Platform.SHOPEE,
                operation="multistore_search_" + operation,
                status=status,
                error_code=value.get("error_code"),
                observed_at=self.clock(),
                source_reference=self.key(value["request"]["authorization_id"]),
            )
        )

    def prepare(self, data, *, offline=False):
        mode = self._mode(offline)
        request = MultistoreSearchScope.model_validate(data)
        key = self.key(request.authorization_id)
        with self.factory() as session:
            preferences, _ = DiscoveryOperations().load(session)
            if (
                request.not_before <= aware_utc(self.clock())
                or (request.expires_at - request.not_before).total_seconds()
                > preferences.quota_period_seconds
            ):
                raise ValueError("Prepare uma janela futura dentro do período administrativo.")
            scope, _, _ = self._scope(session, request, mode)
            if session.get(ShopeeCollectionRun, str(request.authorization_id)) or session.scalar(
                select(AppSetting.id).where(
                    AppSetting.key.endswith(":" + str(request.authorization_id)),
                    AppSetting.key != key,
                )
            ):
                raise ValueError("UUID já utilizado por outra autorização ou tentativa.")
            value = dict(
                version=2,
                mode=mode,
                state="PREPARED",
                request=request.model_dump(mode="json"),
                scope=scope,
                scope_sha256=fingerprint(scope),
                consumed=False,
                approval=None,
                result={},
                prepared_at=self.clock().isoformat(),
            )
            inserted = session.execute(
                conflict_insert(session, AppSetting)
                .values(
                    key=key,
                    value=value,
                    description="Busca multiloja de uma tentativa; sem ingestão.",
                )
                .on_conflict_do_nothing(index_elements=[AppSetting.key])
                .returning(AppSetting.id)
            ).scalar_one_or_none()
            row = self._row(session, key)
            if row.value["scope"] != scope or row.value["consumed"]:
                raise ValueError("UUID já preparado com outro escopo ou consumido.")
            if inserted is not None:
                self._audit(session, value, "prepared", "PREPARED")
            session.commit()
        return self.read(request.authorization_id)

    def _validate_scope(self, session, value, mode, expected_hash):
        request = MultistoreSearchScope.model_validate(value["request"])
        scope, ref, query = self._scope(session, request, mode)
        if (
            value.get("version") != 2
            or value["mode"] != mode
            or value["scope"] != scope
            or value["scope_sha256"] != fingerprint(scope)
            or expected_hash != fingerprint(scope)
        ):
            raise ValueError("Payload, credencial, janela ou busca diverge do escopo aprovado.")
        if value.get("approval"):
            approval = ReadApproval.model_validate(value["approval"])
            if approval.scope_sha256 != expected_hash or approval.expires_at != request.expires_at:
                raise ValueError("Aprovação diverge da janela ou hash selado.")
        return request, ref, query

    def approve(self, authorization_id, data, *, offline=False):
        mode = self._mode(offline)
        confirmation = ReadConfirmation.model_validate(data)
        with self.factory() as session:
            row = self._row(session, self.key(authorization_id))
            if row is None:
                raise LookupError("Autorização multiloja inexistente.")
            value = row.value
            request, _, _ = self._validate_scope(session, value, mode, confirmation.scope_sha256)
            if aware_utc(self.clock()) >= request.expires_at:
                raise ValueError("Janela de autorização expirada.")
            approval = ReadApproval(
                **confirmation.model_dump(), expires_at=request.expires_at
            ).model_dump(mode="json")
            if value["state"] != "PREPARED":
                if value["state"] == "APPROVED" and value["approval"] == approval:
                    return self.read(authorization_id)
                raise ValueError("Autorização já consumida ou encerrada.")
            row.value = {**value, "state": "APPROVED", "approval": approval}
            self._audit(session, row.value, "approved", "APPROVED")
            session.commit()
        return self.read(authorization_id)

    def _validate_dispatch_window(self, value):
        if aware_utc(self.clock()) < datetime.fromisoformat(value["scope"]["not_before"]):
            raise ValueError("Busca fora da janela aprovada; tentativa não executada.")

    def _dispatch(self, connector, request):
        if self._credential_binding(connector.app_id, connector.secret) != self._credential_binding(
            self.settings.shopee_app_id, self.settings.shopee_secret
        ):
            raise IntegrationError(
                "SCOPE_VIOLATION", "Credencial do conector diverge da autorização."
            )
        return connector.discover_offers(
            request.keyword, page=request.page, limit=request.limit, profile="multistore"
        )

    def _sanitize_dispatched(self, page, calls, mode, request):
        return self._sanitized(page, calls, mode, query=request.keyword)

    def _sanitized(self, page, calls, mode, *, query="Cockpit simulador Brave Bull"):
        result = compare_offers(page, query)
        # No structured brand/model/variant evidence is returned by this API.
        # Missing proof must remain missing, even after an authorized real query.
        result.update(
            incompatible_products=[r for r in result["candidates"] if r["status"] == "REJECTED"],
            proven_equivalences=result["groups"],
            has_next_not_followed=page.has_next,
            http_calls=calls,
            mode=mode,
        )
        result["candidates"] = [r for r in result["candidates"] if r["status"] != "REJECTED"]

        def clean(value):
            if isinstance(value, dict):
                # The durable result never keeps product or affiliate URLs; identity
                # (shop:item), price and fingerprints are enough to review a quote.
                # The discovery pilot builds its own rows through compare_offers.
                return {k: clean(v) for k, v in value.items() if k not in _URL_KEYS}
            if isinstance(value, (tuple, list)):
                return [clean(v) for v in value]
            if isinstance(value, str):
                value = "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in value)
                for secret in (self.settings.shopee_app_id, self.settings.shopee_secret):
                    if secret and secret.get_secret_value():
                        value = value.replace(secret.get_secret_value(), "[REDACTED]")
            return value

        return json.loads(json.dumps(clean(result), default=str))

    def _result_summary(self, quote, calls, mode):
        return dict(
            http_calls=calls,
            mode=mode,
            http_status=quote["http_status"],
            comparison_sha256=fingerprint(quote),
            comparison=quote,
            raw_response_persisted=False,
        )

    def read(self, authorization_id):
        result = super().read(authorization_id)
        with self.factory() as session:
            value = session.scalar(
                select(AppSetting.value).where(AppSetting.key == self.key(authorization_id))
            )
        request = MultistoreSearchScope.model_validate(value["request"])
        return {
            **result,
            "scope": value["scope"],
            "payload": {
                "query": ShopeeConnector.discovery_query(
                    request.keyword, page=request.page, limit=request.limit, profile="multistore"
                )
            },
        }
