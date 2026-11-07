"""Explicit bounded reads, shared ingestion and informational candidates; no publisher."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from sqlalchemy import func, select

from app.integrations.contracts import DiscoveryPage, ProductRef, fingerprint
from app.integrations.errors import IntegrationError
from app.integrations.registry import ConnectorRegistry
from app.integrations.shopee import ENDPOINT, ShopeeConnector, _positive_id
from app.models import (
    Availability,
    IntegrationEvent,
    OfferStatus,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    ShopeeCollectionCandidate,
    ShopeeCollectionRun,
)
from app.services.categories import CATALOG, CategoryService
from app.services.comparison import PriceComparisonService, PriceDirection
from app.services.discovery_operations import DiscoveryOperations
from app.services.leases import acquire_lease, conflict_insert, release_lease, renew_lease
from app.services.marketplaces import ingest_discovery
from app.services.monitoring import record_price
from app.services.offers import OfferEngine, latest_observation
from app.services.product_filters import matches_product
from app.services.simulation_safety import SimulationPolicyInput, require_simulation_scope


def normalized_observations(items):
    normalized = []
    for snapshot in items:
        if (
            snapshot.platform != Platform.SHOPEE
            or snapshot.is_simulated
            or snapshot.source != "shopee:official_api"
        ):
            raise ValueError("Proveniência Shopee incompatível.")
        metadata = dict(snapshot.evidence_metadata)
        metadata.update(
            price_min=str(snapshot.reported_price_min)
            if snapshot.reported_price_min is not None
            else None,
            price_max=str(snapshot.reported_price_max)
            if snapshot.reported_price_max is not None
            else None,
            price_range="single" if snapshot.current_price is not None else "unresolved",
        )
        normalized.append(replace(snapshot, evidence_metadata=metadata))
    return normalized


class CollectionFilters(SimulationPolicyInput):
    enabled: Literal[False] = False
    automatic_approval: Literal[False] = False
    categories: list[str] = Field(min_length=1, max_length=9)

    @field_validator("categories")
    @classmethod
    def categories_known(cls, value):
        if len(set(value)) != len(value) or set(value) - {slug for slug, _ in CATALOG}:
            raise ValueError("Selecione categorias existentes, sem duplicações.")
        return value


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    query: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=1, ge=1, le=5, strict=True)
    actor: str = Field(min_length=3, max_length=100)
    confirmed_real_read: StrictBool = False
    authorization: str | None = Field(default=None, min_length=10, max_length=300)
    filters: CollectionFilters

    @field_validator("query", "actor", "authorization")
    @classmethod
    def clean_text(cls, value):
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("Texto vazio ou com caracteres de controle.")
        return value.strip() if value is not None else value

    @model_validator(mode="after")
    def specific_filter(self):
        if not self.filters.terms and not self.filters.product_ids:
            raise ValueError("Escolha termos específicos ou produtos; busca não é classificação.")
        return self


class ObservationRequest(BaseModel):
    """Future one-shot scope. No authorization field or flag enables live execution."""

    model_config = ConfigDict(extra="forbid")
    operation: Literal["OBSERVE"] = "OBSERVE"
    request_id: UUID
    actor: str = Field(min_length=3, max_length=100)
    product_id: int = Field(strict=True, gt=0, le=2**63 - 1)
    shop_id: str
    item_id: str
    include_offer_link: StrictBool = False

    @field_validator("shop_id", "item_id", mode="before")
    @classmethod
    def official_id(cls, value):
        try:
            return _positive_id(value)
        except IntegrationError:
            raise ValueError("Identificador Shopee inválido.") from None

    @field_validator("actor")
    @classmethod
    def actor_known(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Responsável inválido.")
        return value.strip()


class ShopeeCollectionService:
    def __init__(self, factory, settings, clock=None):
        self.factory, self.settings = factory, settings
        self.clock = clock or (lambda: datetime.now(UTC))

    def _safe(self):
        c = self.settings
        if any(
            (
                c.shopee_enabled,
                c.mercado_livre_enabled,
                c.telegram_publisher_enabled,
                c.telegram_real_sends_authorized,
                c.simulation_automation_enabled,
            )
        ):
            raise ValueError("Coleta controlada exige flags globais reais desativadas.")

    def read(self, request_id):
        with self.factory() as session:
            run = session.get(ShopeeCollectionRun, str(request_id))
            if run is None:
                raise LookupError("Coleta inexistente.")
            return {
                "id": run.id,
                "mode": run.mode,
                "status": run.status,
                "requests_reserved": run.requests,
                "error_code": run.error_code,
                "result": run.result,
                "actor": run.actor,
                "publication_mode": "dry-run",
                "automatic_monitoring": False,
            }

    def assess(self, session, product, history, filters):
        links = CategoryService().classify(session, product)
        active = {link.category.slug for link in links if link.category.active}
        comparison = PriceComparisonService()
        quoted = comparison.reported_quotes(session, history)
        verified = comparison.compare(
            session, history, now=self.clock(), max_age_seconds=self.settings.price_max_age_seconds
        )
        reasons = []
        if not active.intersection(filters.categories):
            reasons.append("CATEGORY_FILTER")
        if not matches_product(
            product.id, product.name, filters.product_ids, filters.terms, conservative=True
        ):
            reasons.append("PRODUCT_FILTER_OR_AMBIGUOUS_INTENT")
        if filters.max_price is not None and (
            history.current_price is None or history.current_price > filters.max_price
        ):
            reasons.append("MAX_PRICE_FILTER")
        if filters.min_discount is not None and (
            verified.direction != PriceDirection.DROP or -verified.percentage < filters.min_discount
        ):
            reasons.append("MIN_VERIFIED_DISCOUNT_FILTER")
        if history.observed_at > self.clock() or history.observed_at < self.clock() - timedelta(
            seconds=self.settings.price_max_age_seconds
        ):
            reasons.append("STALE_OR_FUTURE_OBSERVATION")
            status = "EXPIRED"
        elif reasons:
            status = "FILTERED"
        elif quoted.direction == PriceDirection.INITIAL:
            status = "INITIAL"
        elif verified.direction != PriceDirection.DROP:
            status = "UNVERIFIED"
        else:
            status = "MANUAL_REVIEW"
        return status, {
            "reported_change": asdict(quoted),
            "verified_change": asdict(verified),
            "reasons": reasons,
            "categories": sorted(active),
            "requires_manual_approval": True,
            "publication_authorized": False,
        }

    @staticmethod
    def _json(value):
        import json

        return json.loads(json.dumps(value, default=str))

    @staticmethod
    def _observation_product(session, request, *, lock=True):
        statement = select(Product).where(Product.id == request.product_id)
        product = session.scalar(statement.with_for_update() if lock else statement)
        if (
            product is None
            or product.platform != Platform.SHOPEE
            or product.external_id != f"{request.shop_id}:{request.item_id}"
            or product.variant_id
        ):
            raise ValueError("Produto conhecido ou escopo de variante incompatível.")
        return product

    def _observation_assessment(self, session, history):
        comparison = PriceComparisonService()
        quoted = comparison.reported_quotes(session, history)
        status = "INITIAL" if quoted.direction == PriceDirection.INITIAL else "UNVERIFIED"
        if (
            not self.clock() - timedelta(seconds=self.settings.price_max_age_seconds)
            <= history.observed_at
            <= self.clock()
        ):
            status = "EXPIRED"
        return status, {
            "reported_change": asdict(quoted),
            "verified_change": asdict(comparison.compare(session, history, now=self.clock())),
            "availability": history.availability.value,
            "price_access": history.price_access.value,
            "commercial_verified": False,
            "observation_only": True,
            "publication_authorized": False,
            "requires_manual_approval": True,
            "price_basis": history.evidence_metadata["price_basis"],
            "reasons": ["OBSERVATIONAL_ONLY", "COMMERCIAL_CONDITIONS_UNKNOWN"],
        }

    def observe(self, request, *, connector=None, offline=False):
        request = ObservationRequest.model_validate(request)
        return self.execute(request, connector=connector, offline=offline)

    def _persist_observation(self, session, request, snapshot, calls):
        product = self._observation_product(session, request)
        if snapshot.external_item_id != product.external_id or snapshot.variant_id:
            raise ValueError("Resposta não pertence ao anúncio solicitado.")
        snapshot = replace(
            snapshot,
            availability=Availability.UNKNOWN,
            price_access=PriceAccess.UNKNOWN,
            previous_price=None,
            raw_fingerprint=fingerprint(
                {"quote": snapshot.raw_fingerprint, "mode": "OBSERVATIONAL_ONLY"}
            ),
            evidence_metadata={
                **snapshot.evidence_metadata,
                "observation_only": True,
                "commercial_verified": False,
            },
        )
        history = record_price(session, product, snapshot, now=self.clock())
        status, assessment = self._observation_assessment(session, history)
        entry = ShopeeCollectionCandidate(
            run_id=str(request.request_id),
            product_id=product.id,
            history_id=history.id,
            status=status,
            assessment=self._json(assessment),
        )
        session.add(entry)
        session.flush()
        run = session.get(ShopeeCollectionRun, str(request.request_id))
        run.status, run.finished_at = "SUCCEEDED", self.clock()
        run.result = {
            "products": 1,
            "http_calls": calls[0],
            "publication_calls": 0,
            "observational_only": True,
            "candidates": [
                {
                    "id": entry.id,
                    "product_id": product.id,
                    "history_id": history.id,
                    "status": status,
                    **self._json(assessment),
                }
            ],
        }
        self._observation_audit(session, run)

    @staticmethod
    def _observation_audit(session, run, *, reserved=False):
        session.add(
            IntegrationEvent(
                platform=Platform.SHOPEE,
                operation="observation_reserved" if reserved else "observation_result",
                status=run.status,
                source_reference=f"collection:{run.id}:observational",
                error_code=run.error_code,
            )
        )

    def persist_batch(self, session, request, normalized, calls, *, page_number=1, has_next=False):
        ids = ingest_discovery(
            session,
            DiscoveryPage(normalized, page_number, has_next),
            Platform.SHOPEE,
            self.settings,
            clock=self.clock,
            evaluate_offers=False,
        )
        candidates = []
        for product_id in ids:
            product = session.get(Product, product_id)
            snapshot = next(
                s
                for s in normalized
                if s.external_item_id == product.external_id and s.variant_id == product.variant_id
            )
            history = session.scalar(
                select(ProductPriceHistory).where(
                    ProductPriceHistory.product_id == product_id,
                    ProductPriceHistory.source == snapshot.source,
                    ProductPriceHistory.raw_fingerprint == snapshot.raw_fingerprint,
                    ProductPriceHistory.observed_at == snapshot.observed_at,
                )
            )
            status, assessment = self.assess(session, product, history, request.filters)
            if latest_observation(session, product_id).id != history.id:
                status = "EXPIRED"
            if status not in ("FILTERED", "EXPIRED"):
                offer = OfferEngine(self.settings, self.clock).process(
                    session, product, history, enqueue=False
                )
                if offer is not None:
                    offer.status = OfferStatus.PENDING
            entry = ShopeeCollectionCandidate(
                run_id=str(request.request_id),
                product_id=product.id,
                history_id=history.id,
                status=status,
                assessment=self._json(assessment),
            )
            session.add(entry)
            session.flush()
            candidates.append(
                {
                    "id": entry.id,
                    "product_id": product.id,
                    "history_id": history.id,
                    "status": status,
                    **self._json(assessment),
                }
            )
        run = session.get(ShopeeCollectionRun, str(request.request_id))
        run.status, run.finished_at = "SUCCEEDED", self.clock()
        run.result = {
            "products": len(ids),
            "http_calls": calls[0],
            "candidates": candidates,
            "has_next_not_followed": has_next,
            "publication_calls": 0,
        }
        return candidates

    def execute(self, request, *, connector=None, offline=False):
        self._safe()
        observing = isinstance(request, ObservationRequest)
        if observing and not offline:
            raise ValueError(
                "Leitura observacional real bloqueada; exige nova autorização e revisão."
            )
        request = request if observing else CollectionRequest.model_validate(request)
        mode = "OFFLINE" if offline else "REAL"
        if offline:
            with self.factory() as session:
                require_simulation_scope(session, self.settings)
            if (
                type(connector) is not ShopeeConnector
                or type(connector.transport.client._transport_for_url(httpx.URL(ENDPOINT)))
                is not httpx.MockTransport
            ):
                raise ValueError("Replay exige ShopeeConnector com MockTransport e banco isolado.")
        elif connector is not None or not request.confirmed_real_read or not request.authorization:
            raise ValueError(
                "Consulta real exige opt-in explícito por execução e autorização registrada."
            )
        lease_seconds, ref, preferences = 120, None, None
        if observing:
            with self.factory() as session:
                product = self._observation_product(session, request)
                ref = ProductRef(Platform.SHOPEE, product.external_id, product.original_url)
                ShopeeConnector.product_query(ref)
                preferences, _ = DiscoveryOperations().load(session)
                lease_seconds = preferences.lease_seconds
        specification = request.model_dump(mode="json")
        with self.factory() as session:
            existing = session.get(ShopeeCollectionRun, str(request.request_id))
            if existing is not None:
                if existing.specification != specification or existing.mode != mode:
                    raise ValueError("Chave de idempotência já usada para outro conteúdo.")
                return self.read(request.request_id)
            inserted = session.execute(
                conflict_insert(session, ShopeeCollectionRun)
                .values(
                    id=str(request.request_id),
                    mode=mode,
                    status="RESERVED",
                    requests=0,
                    actor=request.actor,
                    authorization=getattr(request, "authorization", None),
                    specification=specification,
                    result={},
                    created_at=self.clock(),
                )
                .on_conflict_do_nothing(index_elements=[ShopeeCollectionRun.id])
                .returning(ShopeeCollectionRun.id)
            ).scalar_one_or_none()
            if inserted is not None and observing:
                self._observation_audit(
                    session,
                    session.get(ShopeeCollectionRun, str(request.request_id)),
                    reserved=True,
                )
            session.commit()
            if inserted is None:
                session.expire_all()
                other = session.get(ShopeeCollectionRun, str(request.request_id))
                if other.specification != specification or other.mode != mode:
                    raise ValueError("Chave de idempotência já usada para outro conteúdo.")
                return self.read(request.request_id)
        lease = acquire_lease(
            self.factory, "shopee:controlled-collection", lease_seconds, self.clock
        )
        registry = None
        calls = [0]

        def budget(http_request):
            if calls[0] or str(http_request.url) != ENDPOINT or http_request.method != "POST":
                raise ValueError("Limite de uma query oficial excedido.")
            import json

            body = json.loads(http_request.content)
            if observing and body != {"query": ShopeeConnector.product_query(ref)}:
                raise ValueError("Consulta diverge da identidade e seleção reservadas.")
            if "mutation" in body.get("query", "") or "productOfferV2(" not in body.get(
                "query", ""
            ):
                raise ValueError("Somente leitura productOfferV2 autorizada.")
            calls[0] += 1

        hook_added = False
        persistence_started = False
        try:
            if lease is None:
                raise IntegrationError("MONITORING_BUSY", "Outra coleta está em execução.")
            if not offline:
                effective = self.settings.model_copy(
                    update={
                        "shopee_enabled": True,
                        "shopee_short_links_enabled": False,
                        "integration_max_retries": 0,
                        "integration_cache_seconds": 0,
                        "integration_min_interval_seconds": 900,
                        "integration_max_retry_wait_seconds": 0,
                    }
                )
                registry = ConnectorRegistry(
                    effective, session_factory=self.factory, clock=self.clock
                )
                if registry.is_blocked(Platform.SHOPEE):
                    raise IntegrationError(
                        "AUTHORIZATION_DENIED", "Conta bloqueada; revisão necessária."
                    )
                connector = registry.get(Platform.SHOPEE)
            connector.transport.max_retries = 0
            connector.transport.cache_seconds = 0
            connector.transport.client.event_hooks["request"].append(budget)
            hook_added = True
            with self.factory() as session:
                run = session.get(ShopeeCollectionRun, str(request.request_id))
                if observing:
                    since = self.clock() - timedelta(seconds=preferences.quota_period_seconds)
                    used = session.scalar(
                        select(func.coalesce(func.sum(ShopeeCollectionRun.requests), 0)).where(
                            ShopeeCollectionRun.created_at >= since
                        )
                    )
                    if used >= preferences.max_queries_per_period:
                        raise IntegrationError("RATE_LIMITED", "Orçamento observacional esgotado.")
                run.requests = 1
                session.commit()
            if observing:
                page = DiscoveryPage([connector.fetch_product(ref)], 1, False)
            else:
                page = connector.discover_offers(request.query, page=1, limit=request.limit)
                if len(page.items) > request.limit or page.page != 1:
                    raise ValueError("Página excede o orçamento aprovado.")
            normalized = normalized_observations(page.items)
            persistence_started = True
            with self.factory() as session:
                if not renew_lease(
                    session, "shopee:controlled-collection", lease, lease_seconds, self.clock
                ):
                    raise IntegrationError(
                        "LEASE_LOST", "Reserva expirada; resultado não aplicado."
                    )
                if observing:
                    self._persist_observation(session, request, normalized[0], calls)
                else:
                    self.persist_batch(session, request, normalized, calls, has_next=page.has_next)
                session.commit()
        except BaseException as error:
            if not observing and not isinstance(error, Exception):
                raise
            code = (
                error.code if isinstance(error, IntegrationError) else "INVALID_COLLECTION_RESULT"
            )
            if registry is not None and code in ("AUTHORIZATION_DENIED", "UPSTREAM_GRAPHQL_ERROR"):
                registry.block(Platform.SHOPEE)
            with self.factory() as session:
                run = session.get(ShopeeCollectionRun, str(request.request_id))
                run.status = (
                    "UNCERTAIN"
                    if code in ("TIMEOUT", "NETWORK_ERROR", "LEASE_LOST")
                    or (observing and (persistence_started or not isinstance(error, Exception)))
                    else "FAILED"
                )
                run.error_code, run.finished_at = code, self.clock()
                run.result = {"http_calls": calls[0], "publication_calls": 0}
                if observing:
                    self._observation_audit(session, run)
                session.commit()
        finally:
            if hook_added:
                connector.transport.client.event_hooks["request"].remove(budget)
            if registry is not None:
                registry.close()
            if lease is not None:
                release_lease(self.factory, "shopee:controlled-collection", lease)
        return self.read(request.request_id)

    def revalidate(self, candidate_id):
        self._safe()
        with self.factory() as session:
            entry = session.scalar(
                select(ShopeeCollectionCandidate)
                .where(ShopeeCollectionCandidate.id == candidate_id)
                .with_for_update()
            )
            if entry is None:
                raise LookupError("Candidata inexistente.")
            run = session.get(ShopeeCollectionRun, entry.run_id)
            product = session.get(Product, entry.product_id)
            history = latest_observation(session, product.id)
            if history is None or history.id != entry.history_id:
                entry.status = "EXPIRED"
            else:
                if run.specification.get("operation") == "OBSERVE":
                    entry.status, assessment = self._observation_assessment(session, history)
                else:
                    entry.status, assessment = self.assess(
                        session,
                        product,
                        history,
                        CollectionRequest.model_validate(run.specification).filters,
                    )
                entry.assessment = self._json(assessment)
            session.add(
                IntegrationEvent(
                    platform=Platform.SHOPEE,
                    product_id=product.id,
                    operation="collection_revalidate",
                    status=entry.status,
                    source_reference=f"collection:{run.id}:candidate:{entry.id}",
                )
            )
            session.commit()
            return {"id": entry.id, "status": entry.status, "assessment": entry.assessment}

    def prepare_schedule(self, monitoring_runtime):
        self._safe()
        monitoring_runtime.prepare_paused_job("shopee-collection-prepared", self.blocked_tick, 900)
        return {
            "status": "PAUSED",
            "interval_seconds": 900,
            "requests_per_run": 1,
            "max_products": 5,
            "commercial_polling_authorized": False,
        }

    @staticmethod
    def blocked_tick():
        return {"status": "BLOCKED", "reason": "EXPLICIT_PER_RUN_OPT_IN_REQUIRED"}
