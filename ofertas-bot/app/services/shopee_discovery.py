"""Bounded discovery laboratory. Real transports and production databases fail closed."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import httpx
from sqlalchemy import func, select

from app.integrations.contracts import DiscoveryPage, ProductRef
from app.integrations.errors import IntegrationError
from app.integrations.registry import ConnectorRegistry
from app.integrations.shopee import ENDPOINT, ShopeeConnector
from app.models import (
    Availability,
    Category,
    DestinationChannel,
    OfferStatus,
    Platform,
    PriceAccess,
    Product,
    PublicationDestination,
    PublicationQueueItem,
    QueueStatus,
    ShopeeCollectionRun,
    ShopeeDiscoveryFinding,
    ShopeeDiscoveryPolicy,
    ShopeeDiscoveryProduct,
    ShopeeDiscoveryRun,
    SimulationPolicy,
)
from app.services.categories import CategoryService
from app.services.category_routing import destination_accepts
from app.services.comparison import PriceComparisonService, PriceDirection
from app.services.discovery_operations import DiscoveryOperations
from app.services.discovery_relevance import DiscoveryPolicyInput, SearchTerm, relevance
from app.services.leases import acquire_lease, conflict_insert, release_lease, renew_lease
from app.services.marketplaces import upsert_discovered_product
from app.services.message_templates import MessageData, render_message
from app.services.monitoring import apply_product_snapshot
from app.services.offers import OfferEngine, has_own_affiliate_link, latest_observation
from app.services.product_filters import matches_product
from app.services.publication_queue import SimulatedQueueService
from app.services.scoring import OfferScoringService, best_coupon
from app.services.shopee_collection import (
    CollectionRequest,
    ShopeeCollectionService,
    normalized_observations,
)
from app.services.shopee_evidence_review import ShopeeEvidenceReview
from app.services.shopee_multistore import (
    ProductTarget,
    ShopeeMultistoreAdapter,
    ShopeeMultistoreService,
    compare_offers,
)
from app.services.shopee_opportunities import consolidate_opportunities
from app.services.shopee_replay import analyze_recorded_search
from app.services.simulation_safety import require_simulation_scope

LEASE = "shopee:discovery-cycle"
COLLECTION_LEASE = "shopee:controlled-collection"
JOB_IDS = ("shopee-discovery-prepared", "shopee-selected-prepared", "shopee-evaluation-prepared")


class ShopeeDiscoveryService:
    def __init__(self, factory, settings, *, registry=None, clock=None, affiliate_links=None):
        self.factory, self.settings, self.registry = factory, settings, registry
        self.clock = clock or (lambda: datetime.now(UTC))
        self.collection = ShopeeCollectionService(factory, settings, self.clock)
        self.operations = DiscoveryOperations()
        self._prepared_runtime = None
        # No generator is installed by the production application.
        self.affiliate_links = affiliate_links

    def _offline(self):
        self.collection._safe()
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
        if type(self.registry) is not ConnectorRegistry:
            raise ValueError("Execução exige registro isolado com transporte mockado.")
        connector = self.registry.get(Platform.SHOPEE)
        if (
            type(connector) is not ShopeeConnector
            or type(connector.transport.client._transport_for_url(httpx.URL(ENDPOINT)))
            is not httpx.MockTransport
        ):
            raise ValueError("Somente MockTransport é permitido nesta fase.")
        connector.transport.max_retries = 0
        connector.transport.cache_seconds = 0
        return connector

    @staticmethod
    def _json(value):
        return json.loads(json.dumps(value, default=str))

    def _audit(self, session, operation, specification, result, category_id=None):
        row = ShopeeDiscoveryRun(
            id=str(uuid4()),
            operation=operation,
            status="SUCCEEDED",
            category_id=category_id,
            specification=specification,
            result=result,
            started_at=self.clock(),
            finished_at=self.clock(),
        )
        session.add(row)
        session.flush()
        return row.id

    def preferences(self, destination_id=None):
        with self.factory() as session:
            return self.operations.read(session, destination_id)

    def search_multistore(
        self, query, *, evidence=None, target=None, exclusions=(), intent="generic", limit=3
    ):
        """One mock discovery page and a read-only cross-store assessment.

        No ingestion/evaluation jobs are run: historical discount, coupon and
        publication eligibility are independent of this informational comparison.
        """
        SearchTerm(query=query, intent=intent)
        if target is not None:
            target = ProductTarget.model_validate(target)
        exclusions = DiscoveryPolicyInput.exclusions_normalized(list(exclusions))
        connector = self._offline()
        page = ShopeeMultistoreService(self.collection).discover_offline(
            query, connector, limit=limit
        )
        return dict(
            **compare_offers(
                page, query, evidence=evidence, target=target, exclusions=exclusions, intent=intent
            ),
            mode="OFFLINE",
            mock_http_calls=1,
            external_http_calls=0,
            has_next_not_followed=page.has_next,
        )

    def search_defined_product(self, product, *, evidence=None, exclusions=(), limit=20):
        """Search by explicit brand/model; quantity requires evidence, never a title default."""
        product = ProductTarget.model_validate(product)
        return self.search_multistore(
            product.search_query(),
            target=product,
            evidence=evidence,
            exclusions=exclusions,
            limit=limit,
        )

    def review_defined_product(self, page, product, *, evidence=None, exclusions=()):
        """Reassess a captured page offline; no connector call or history write."""
        self.collection._safe()
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
        product = ProductTarget.model_validate(product)
        exclusions = DiscoveryPolicyInput.exclusions_normalized(list(exclusions))
        return dict(
            **compare_offers(
                page,
                product.search_query(),
                target=product,
                evidence=evidence,
                exclusions=exclusions,
            ),
            mode="OFFLINE_REVIEW",
            mock_http_calls=0,
            external_http_calls=0,
            has_next_not_followed=page.has_next,
        )

    def prepare_multistore_search(self, data, *, offline=False):
        return ShopeeMultistoreAdapter(self.collection).prepare(data, offline=offline)

    def multistore_search_status(self, authorization_id):
        return ShopeeMultistoreAdapter(self.collection).read(authorization_id)

    def analyze_multistore_search(self, authorization_id, *, target=None):
        """Analyze the saved sanitized result; does not claim or execute an authorization."""
        return analyze_recorded_search(
            self.multistore_search_status(authorization_id),
            target=target,
            documented_reviews=ShopeeEvidenceReview(self.collection).records(authorization_id),
        )

    def multistore_opportunities(self, authorization_id, *, target=None, selection_limit=2):
        """Consolidated preview only; never calls OfferEngine or writes an outbox."""
        return consolidate_opportunities(
            self.analyze_multistore_search(authorization_id, target=target),
            selection_limit=selection_limit,
        )

    def multistore_commercial_preview(
        self, authorization_id, identity, *, target=None, offer_id=None
    ):
        from app.services.shopee_commercial_handoff import commercial_preview, saved_analysis

        with self.factory() as session:
            return commercial_preview(
                session,
                self.settings,
                saved_analysis(session, authorization_id, target),
                identity,
                now=self.clock(),
                offer_id=offer_id,
            )

    def approve_multistore_search(self, authorization_id, data, *, offline=False):
        return ShopeeMultistoreAdapter(self.collection).approve(
            authorization_id, data, offline=offline
        )

    def execute_multistore_search(self, authorization_id, data, *, offline=False, connector=None):
        result = ShopeeMultistoreAdapter(self.collection).execute(
            authorization_id, data, offline=offline, connector=connector
        )
        result.pop("quote", None)  # Sanitized comparison is available in the durable result.
        return result

    def update_preferences(self, changes, expected_revision, actor, destination_id=None):
        self.collection._safe()
        with self.factory() as session:
            preferences, _ = self.operations.load(session)
        owner = acquire_lease(self.factory, LEASE, preferences.lease_seconds, self.clock)
        if owner is None:
            raise ValueError("Discovery is busy; preferences were not modified.")
        try:
            with self.factory() as session:
                before, after, result = self.operations.save(
                    session, changes, expected_revision, destination_id
                )
                audit = self._audit(
                    session,
                    "CONFIGURE",
                    {
                        "actor": actor,
                        "scope": "operations",
                        "destination_id": destination_id,
                        "before": before,
                    },
                    {"after": after, "authorization_granted": False},
                )
                self._fence(session, [(LEASE, owner)])
                session.commit()
            self.refresh_schedule()
            return {**result, "audit_id": audit}
        finally:
            release_lease(self.factory, LEASE, owner)

    def _policy_due(self, session, policy, config, *, monitoring=False):
        preferences, metadata = self.operations.load(session, category_policy=config)
        if (
            preferences.enabled_category_ids
            and policy.category_id not in preferences.enabled_category_ids
        ):
            return False
        if not preferences.open_at(self.clock()):
            return False
        if policy.state.get("authorization_review_required"):
            return False
        if policy.state.get("failures", 0) and policy.next_at and policy.next_at > self.clock():
            return False
        if metadata["global_revision"] == 0:
            return policy.next_at is None or policy.next_at <= self.clock()
        return (
            monitoring
            or policy.last_run_at is None
            or policy.last_run_at + timedelta(seconds=preferences.discovery_interval_seconds)
            <= self.clock()
        )

    def configure(self, category_id, data, actor):
        self.collection._safe()
        policy = DiscoveryPolicyInput.model_validate(data)
        with self.factory() as session:
            category = session.get(Category, category_id)
            if category is None:
                raise ValueError("Categoria cadastrada inexistente.")
            destinations = set(
                session.scalars(
                    select(PublicationDestination.id).where(
                        PublicationDestination.id.in_(policy.destinations)
                    )
                )
            )
            if destinations != set(policy.destinations):
                raise ValueError("Destino inexistente.")
            session.execute(
                conflict_insert(session, ShopeeDiscoveryPolicy)
                .values(category_id=category_id, config=policy.model_dump(mode="json"), state={})
                .on_conflict_do_nothing(index_elements=[ShopeeDiscoveryPolicy.category_id])
            )
            row = session.scalar(
                select(ShopeeDiscoveryPolicy)
                .where(ShopeeDiscoveryPolicy.category_id == category_id)
                .with_for_update()
            )
            before = row.config
            row.config = policy.model_dump(mode="json")
            # Configuration changes cannot erase quota/backoff or retry a failed page early.
            audit = self._audit(
                session,
                "CONFIGURE",
                {"actor": actor, "before": before},
                {"after": row.config, "real_activation": False},
                category_id,
            )
            session.commit()
            return {
                "category_id": category_id,
                "config": row.config,
                "audit_id": audit,
                "execution": "OFFLINE_ONLY",
            }

    def status(self):
        with self.factory() as session:
            rows = []
            for c in session.scalars(select(Category).order_by(Category.id)):
                p = session.get(ShopeeDiscoveryPolicy, c.id)
                rows.append(
                    {
                        "id": c.id,
                        "slug": c.slug,
                        "name": c.name,
                        "active": c.active,
                        "config": p.config if p else None,
                        "next_at": p.next_at if p else None,
                    }
                )
        return {
            "mode": "OFFLINE_ONLY",
            "commercial_jobs": "PAUSED",
            "categories": rows,
            "real_calls_authorized": False,
            "automatic_commercial_approval": False,
            "capabilities_pending": [
                "account_quotas",
                "sorting",
                "coupons",
                "campaign_contracts",
                "public_price",
                "stock",
                "own_affiliate_links",
            ],
        }

    def read(self, request_id):
        with self.factory() as session:
            row = session.get(ShopeeDiscoveryRun, str(request_id))
            if row is None:
                raise LookupError("Execução inexistente.")
            return {
                "id": row.id,
                "operation": row.operation,
                "status": row.status,
                "mode": row.mode,
                "requests": row.requests,
                "error_code": row.error_code,
                "category_id": row.category_id,
                "result": row.result,
            }

    def execute(self, request_id, operation="DISCOVER", actor="offline administrator"):
        connector = self._offline()
        run_id = str(UUID(str(request_id)))
        if operation not in ("DISCOVER", "MONITOR", "EVALUATE", "RECOVER"):
            raise ValueError("Operação inválida.")
        spec = {"operation": operation, "actor": actor}
        with self.factory() as session:
            row = session.get(ShopeeDiscoveryRun, run_id)
            if row:
                if row.specification != spec:
                    raise ValueError("Chave de idempotência pertence a outra execução.")
                return self.read(run_id)
        with self.factory() as session:
            preferences, metadata = self.operations.load(session)
        owners = []
        for name in (LEASE, COLLECTION_LEASE):
            owner = acquire_lease(self.factory, name, preferences.lease_seconds, self.clock)
            if owner is None:
                for held, token in reversed(owners):
                    release_lease(self.factory, held, token)
                return {"status": "BUSY", "requests": 0}
            owners.append((name, owner))
        try:
            with self.factory() as session:
                inserted = session.execute(
                    conflict_insert(session, ShopeeDiscoveryRun)
                    .values(
                        id=run_id,
                        operation=operation,
                        status="RESERVED",
                        mode="OFFLINE",
                        specification=spec,
                        result={
                            "operational_preferences": preferences.model_dump(mode="json"),
                            "preference_revisions": metadata,
                        },
                        requests=0,
                        started_at=self.clock(),
                    )
                    .on_conflict_do_nothing(index_elements=[ShopeeDiscoveryRun.id])
                    .returning(ShopeeDiscoveryRun.id)
                ).scalar_one_or_none()
                session.commit()
            if inserted is None:
                with self.factory() as session:
                    if session.get(ShopeeDiscoveryRun, run_id).specification != spec:
                        raise ValueError("Idempotency specification mismatch.")
                return self.read(run_id)
            try:
                if operation in ("DISCOVER", "MONITOR") and not preferences.open_at(self.clock()):
                    with self.factory() as session:
                        self._finish(
                            session,
                            run_id,
                            {
                                "reason": "OUTSIDE_OPERATION_WINDOW",
                                "preference_revisions": metadata,
                            },
                        )
                elif operation == "DISCOVER":
                    self._discover(run_id, connector, owners)
                elif operation == "MONITOR":
                    self._monitor(run_id, connector, owners)
                elif operation == "EVALUATE":
                    self._evaluate(run_id, owners)
                else:
                    self._recover(run_id)
            except Exception as error:
                code = (
                    error.code
                    if isinstance(error, IntegrationError)
                    else "INVALID_DISCOVERY_RESULT"
                )
                with self.factory() as session:
                    row = session.get(ShopeeDiscoveryRun, run_id)
                    row.status = (
                        "UNCERTAIN"
                        if code in ("TIMEOUT", "NETWORK_ERROR", "LEASE_LOST")
                        else "FAILED"
                    )
                    row.error_code, row.finished_at = code, self.clock()
                    if row.category_id:
                        policy = session.get(ShopeeDiscoveryPolicy, row.category_id)
                        state = dict(policy.state)
                        state["failures"] = min(state.get("failures", 0) + 1, 8)
                        if code in ("AUTHORIZATION_DENIED", "UPSTREAM_GRAPHQL_ERROR"):
                            state["authorization_review_required"] = True
                        policy.state = state
                        policy.next_at = self.clock() + timedelta(
                            seconds=min(
                                preferences.backoff_max_seconds,
                                preferences.backoff_initial_seconds * 2 ** (state["failures"] - 1),
                            )
                        )
                    session.commit()
            return self.read(run_id)
        finally:
            for name, token in reversed(owners):
                release_lease(self.factory, name, token)

    def _fence(self, session, owners):
        preferences, _ = self.operations.load(session)
        if not all(
            renew_lease(session, n, t, preferences.lease_seconds, self.clock) for n, t in owners
        ):
            raise IntegrationError("LEASE_LOST", "Reserva expirada.")

    def _budget(self, session, category_id, policy):
        preferences, _ = self.operations.load(session, category_policy=policy)
        since = self.clock() - timedelta(seconds=preferences.quota_period_seconds)
        base = select(func.coalesce(func.sum(ShopeeDiscoveryRun.requests), 0)).where(
            ShopeeDiscoveryRun.started_at >= since
        )
        total = session.scalar(base)
        category = session.scalar(base.where(ShopeeDiscoveryRun.category_id == category_id))
        return (
            total < preferences.max_queries_per_period
            and category < preferences.max_queries_per_category_per_period
        )

    def _request(self, run_id, connector, callback, owners):
        # Durable claim precedes I/O, including failures before a response. No retry/fallback.
        with self.factory() as session:
            self._fence(session, owners)
            row = session.get(ShopeeDiscoveryRun, run_id)
            policy = DiscoveryPolicyInput.model_validate(
                session.get(ShopeeDiscoveryPolicy, row.category_id).config
            )
            preferences, _ = self.operations.load(session, category_policy=policy)
            if not preferences.open_at(self.clock()):
                raise IntegrationError(
                    "OPERATION_WINDOW_CLOSED", "Outside configured operating window."
                )
            if row.requests >= preferences.max_requests_per_run or not self._budget(
                session, row.category_id, policy
            ):
                raise IntegrationError("LOCAL_REQUEST_BUDGET", "Orçamento local esgotado.")
            row.requests += 1
            session.commit()
        calls = [0]

        def guard(request):
            query = json.loads(request.content).get("query", "")
            if (
                calls[0]
                or request.method != "POST"
                or str(request.url) != ENDPOINT
                or "mutation" in query
                or "productOfferV2(" not in query
            ):
                raise ValueError("Somente uma query productOfferV2 mockada por reserva.")
            calls[0] += 1

        connector.transport.client.event_hooks["request"].append(guard)
        try:
            return callback()
        finally:
            connector.transport.client.event_hooks["request"].remove(guard)

    def _discover(self, run_id, connector, owners):
        with self.factory() as session:
            policies = session.scalars(
                select(ShopeeDiscoveryPolicy)
                .join(Category)
                .where(
                    Category.active.is_(True),
                )
                .order_by(
                    ShopeeDiscoveryPolicy.last_run_at.asc().nullsfirst(),
                    ShopeeDiscoveryPolicy.category_id,
                )
            ).all()
            policies = [
                p
                for p in policies
                if p.config["enabled"]
                and self._policy_due(session, p, DiscoveryPolicyInput.model_validate(p.config))
            ]
            preferences, metadata = self.operations.load(session)
            policies.sort(
                key=lambda p: (
                    p.last_run_at or datetime.min.replace(tzinfo=UTC),
                    -(p.category_id in preferences.priority_category_ids),
                    -p.config["category_priority"],
                    p.category_id,
                )
            )
            policy = next(
                (
                    p
                    for p in policies
                    if self._budget(
                        session, p.category_id, DiscoveryPolicyInput.model_validate(p.config)
                    )
                ),
                None,
            )
            if policy is None:
                self._finish(session, run_id, {"reason": "NO_DUE_POLICY_OR_BUDGET"})
                return
            config = DiscoveryPolicyInput.model_validate(policy.config)
            terms = sorted(config.searches, key=lambda t: -t.priority)
            index = policy.state.get("term_index", 0) % len(terms)
            term = terms[index]
            first = policy.state.get("next_page", 1)
            row = session.get(ShopeeDiscoveryRun, run_id)
            row.category_id = policy.category_id
            row.result = {
                "pages": [],
                "term": term.model_dump(),
                "config": config.model_dump(mode="json"),
                "operational_preferences": preferences.model_dump(mode="json"),
                "preference_revisions": metadata,
            }
            policy.last_run_at = self.clock()
            session.commit()
            category_id = policy.category_id
        for page_number in range(first, config.max_pages + 1):
            page = self._request(
                run_id,
                connector,
                lambda page_number=page_number: connector.discover_offers(
                    term.query, page=page_number, limit=config.page_size
                ),
                owners,
            )
            if page.page != page_number or len(page.items) > config.page_size:
                raise ValueError("Resposta excede página ou lote solicitado.")
            # Analyze the page already fetched by the existing discovery cycle.
            # Separate batches preserve observation times; no extra request or history baseline.
            opportunity = compare_offers(
                page, term.query, exclusions=config.exclusions, intent=term.intent
            )["opportunity_view"]
            with self.factory() as session:
                self._fence(session, owners)
                self._ingest(session, run_id, category_id, config, term, page)
                current_run = session.get(ShopeeDiscoveryRun, run_id)
                current_run.result = {
                    **current_run.result,
                    "multistore_opportunities": [
                        *current_run.result.get("multistore_opportunities", []),
                        {"page": page_number, **self._json(opportunity)},
                    ],
                }
                policy = session.get(ShopeeDiscoveryPolicy, category_id)
                continue_page = page.has_next and page_number < config.max_pages
                policy.state = {
                    **policy.state,
                    "term_index": index if continue_page else (index + 1) % len(terms),
                    "next_page": page_number + 1 if continue_page else 1,
                    "failures": 0,
                }
                effective, _ = self.operations.load(session, category_policy=config)
                policy.next_at = self.clock() + timedelta(
                    seconds=effective.discovery_interval_seconds
                )
                self._fence(session, owners)
                session.commit()
            if not continue_page:
                break
            with self.factory() as session:
                current = session.get(ShopeeDiscoveryRun, run_id)
                effective, _ = self.operations.load(session, category_policy=config)
                if current.requests >= effective.max_requests_per_run or not self._budget(
                    session, category_id, config
                ):
                    current.result = {
                        **current.result,
                        "reason": "LOCAL_BATCH_BUDGET_REACHED",
                        "next_page_preserved": True,
                    }
                    session.commit()
                    break
        with self.factory() as session:
            self._fence(session, owners)
            self._finish(session, run_id, session.get(ShopeeDiscoveryRun, run_id).result)

    def _ingest(self, session, run_id, category_id, config, term, page):
        selected, findings = [], []
        category = session.get(Category, category_id)
        seen = set(
            session.scalars(
                select(ShopeeDiscoveryFinding.product_id).where(
                    ShopeeDiscoveryFinding.run_id == run_id
                )
            )
        )
        for snapshot in normalized_observations(page.items):
            product = upsert_discovered_product(session, snapshot)
            apply_product_snapshot(product, snapshot)
            selection = session.get(ShopeeDiscoveryProduct, product.id)
            disposition, score, reason = relevance(
                product.name,
                term,
                config.exclusions,
                selection.manual_decision if selection else None,
            )
            active = {
                link.category.slug
                for link in CategoryService().classify(session, product)
                if link.category.active
            }
            if product.id in seen:
                disposition, reason = "DUPLICATE", "IDENTITY_SEEN_IN_RUN"
            elif (
                category.slug not in active
                or not product.monitoring_enabled
                or not matches_product(
                    product.id, product.name, config.product_ids, config.terms, conservative=True
                )
                or (
                    config.max_price is not None
                    and (
                        snapshot.current_price is None or snapshot.current_price > config.max_price
                    )
                )
            ):
                disposition, reason = "FILTERED", "CATEGORY_PRODUCT_OR_MAX_PRICE_FILTER"
            elif snapshot.current_price is None and disposition == "SELECTED":
                disposition, reason = "REVIEW", "UNRESOLVED_PRICE_OR_VARIANT"
            finding = ShopeeDiscoveryFinding(
                run_id=run_id,
                page=page.page,
                product_id=product.id,
                disposition=disposition,
                score=score,
                assessment={
                    "reason": reason,
                    "term": term.model_dump(),
                    "identity": {
                        "platform": "SHOPEE",
                        "shop_item": product.external_id,
                        "variant": product.variant_id,
                    },
                },
            )
            session.add(finding)
            findings.append(finding)
            if disposition in ("SELECTED", "REVIEW"):
                selected.append(snapshot)
                if selection is None:
                    selection = ShopeeDiscoveryProduct(
                        product_id=product.id,
                        category_id=category_id,
                        state=disposition,
                        priority=score,
                    )
                    session.add(selection)
                selection.category_id = category_id
                selection.state = disposition
                effective, _ = self.operations.load(session, category_policy=config)
                selection.next_at = self.clock() + timedelta(
                    seconds=effective.monitor_interval_seconds
                )
                selection.plan = {
                    **(selection.plan or {}),
                    "term": term.model_dump(),
                    "reason": reason,
                }
            elif (
                disposition == "FILTERED"
                and selection is not None
                and selection.category_id == category_id
            ):
                selection.state = "PAUSED"
                selection.plan = {
                    **(selection.plan or {}),
                    "term": term.model_dump(),
                    "reason": reason,
                }
            seen.add(product.id)
        session.flush()
        candidates = self._persist(session, run_id, category_id, config, term, page.page, selected)
        by_product = {c["product_id"]: c for c in candidates}
        for finding in findings:
            c = by_product.get(finding.product_id)
            if c is None or finding.disposition == "DUPLICATE":
                continue
            finding.history_id = c["history_id"]
            selection = session.get(ShopeeDiscoveryProduct, finding.product_id)
            selection.candidate_id = c["id"]
            score = self._score(session, finding.product_id, c, config, term, finding.score)
            finding.score = selection.priority = score
            finding.assessment = {**finding.assessment, "candidate": c, "score": str(score)}
        row = session.get(ShopeeDiscoveryRun, run_id)
        pages = list(row.result.get("pages", []))
        pages.append(
            {
                "page": page.page,
                "has_next": page.has_next,
                "received": len(page.items),
                "dispositions": {
                    key: sum(f.disposition == key for f in findings)
                    for key in ("SELECTED", "REVIEW", "FILTERED", "DUPLICATE")
                },
                "candidates": len(candidates),
            }
        )
        row.result = {
            **row.result,
            "pages": pages,
            "publication_calls": 0,
            "real_commercial_calls": 0,
        }

    def _persist(self, session, run_id, category_id, config, term, page_number, snapshots):
        if not snapshots:
            return []
        ids = [
            session.scalar(
                select(Product.id).where(
                    Product.platform == Platform.SHOPEE,
                    Product.external_id == s.external_item_id,
                    Product.variant_id == s.variant_id,
                )
            )
            for s in snapshots
        ]
        request = CollectionRequest(
            request_id=uuid5(UUID(run_id), f"page:{page_number}"),
            query=term.query,
            actor="offline discovery",
            limit=config.page_size,
            filters={
                "categories": [session.get(Category, category_id).slug],
                "product_ids": ids,
                "max_price": config.max_price,
                "min_discount": config.min_discount,
            },
        )
        session.add(
            ShopeeCollectionRun(
                id=str(request.request_id),
                mode="OFFLINE",
                status="RESERVED",
                requests=1,
                actor=request.actor,
                specification=request.model_dump(mode="json"),
                result={},
            )
        )
        session.flush()
        return self.collection.persist_batch(
            session, request, snapshots, [1], page_number=page_number
        )

    def _score(
        self, session, product_id, candidate, config, term, relevance_score, preferences=None
    ):
        product = session.get(Product, product_id)
        if preferences is None:
            preferences, _ = self.operations.load(session, category_policy=config)
        comparison = candidate["verified_change"]
        drop = comparison["direction"] == "DROP"
        score = OfferScoringService(self.settings).score(
            historical_drop=drop,
            confirmed_coupon=best_coupon(
                session,
                product,
                latest_observation(session, product_id).current_price,
                self.clock(),
                self.settings.price_max_age_seconds,
            )
            is not None
            if latest_observation(session, product_id).current_price is not None
            else False,
            priority=product.priority
            or product_id in preferences.priority_product_ids
            or any(
                link.category_id in preferences.priority_category_ids
                for link in product.category_links
            ),
            discount_percent=-Decimal(comparison["percentage"]) if drop else Decimal(0),
            available=latest_observation(session, product_id).availability
            == Availability.AVAILABLE,
        )
        return relevance_score + score + Decimal(config.category_priority + term.priority)

    def _monitor(self, run_id, connector, owners):
        with self.factory() as session:
            selections = session.scalars(
                select(ShopeeDiscoveryProduct)
                .where(
                    ShopeeDiscoveryProduct.state == "SELECTED",
                )
                .order_by(
                    ShopeeDiscoveryProduct.next_at.asc().nullsfirst(),
                    ShopeeDiscoveryProduct.priority.desc(),
                    ShopeeDiscoveryProduct.product_id,
                )
            ).all()
            preferences, _ = self.operations.load(session)
            selections.sort(
                key=lambda item: (
                    (item.plan or {}).get("last_monitored_at", ""),
                    -(item.product_id in preferences.priority_product_ids),
                    -(item.category_id in preferences.priority_category_ids),
                    -item.priority,
                    item.product_id,
                )
            )
            selected = None
            for item in selections:
                policy = session.get(ShopeeDiscoveryPolicy, item.category_id)
                if (
                    policy
                    and policy.config["enabled"]
                    and session.get(Category, policy.category_id).active
                    and self._policy_due(
                        session,
                        policy,
                        DiscoveryPolicyInput.model_validate(policy.config),
                        monitoring=True,
                    )
                ):
                    config = DiscoveryPolicyInput.model_validate(policy.config)
                    preferences, metadata = self.operations.load(session, category_policy=config)
                    latest = latest_observation(session, item.product_id)
                    due = (
                        (item.next_at is None or item.next_at <= self.clock())
                        if metadata["global_revision"] == 0
                        else (
                            latest is None
                            or latest.observed_at
                            + timedelta(seconds=preferences.monitor_interval_seconds)
                            <= self.clock()
                        )
                    )
                    if due and self._budget(session, item.category_id, config):
                        selected = item
                        break
            if selected is None:
                self._finish(session, run_id, {"reason": "NO_DUE_SELECTED_PRODUCT_OR_BUDGET"})
                return
            row = session.get(ShopeeDiscoveryRun, run_id)
            row.category_id = selected.category_id
            product = session.get(Product, selected.product_id)
            ref = ProductRef(
                Platform.SHOPEE, product.external_id, product.original_url, product.variant_id
            )
            product_id, category_id = product.id, selected.category_id
            term = SearchTerm.model_validate(selected.plan["term"])
            session.commit()
        name = f"monitor:product:{product_id}"
        owner = acquire_lease(self.factory, name, preferences.lease_seconds, self.clock)
        if owner is None:
            raise IntegrationError("MONITORING_BUSY", "Produto em execução.")
        try:
            all_owners = [*owners, (name, owner)]
            snapshot = self._request(
                run_id, connector, lambda: connector.fetch_product(ref), all_owners
            )
            with self.factory() as session:
                self._fence(session, all_owners)
                self._ingest(
                    session, run_id, category_id, config, term, DiscoveryPage([snapshot], 1, False)
                )
                selection = session.get(ShopeeDiscoveryProduct, product_id)
                effective, _ = self.operations.load(session, category_policy=config)
                selection.next_at = self.clock() + timedelta(
                    seconds=effective.monitor_interval_seconds
                )
                selection.plan = {**selection.plan, "last_monitored_at": self.clock().isoformat()}
                self._fence(session, all_owners)
                self._finish(session, run_id, session.get(ShopeeDiscoveryRun, run_id).result)
        finally:
            release_lease(self.factory, name, owner)

    def _evaluate(self, run_id, owners, *, _prepare_links=True, _link_results=None):
        preparing = _prepare_links and self.affiliate_links is not None
        pending_links = []
        if preparing:
            self.affiliate_links.require_offline()
        with self.factory() as session:
            self._fence(session, owners)
            results = []
            global_preferences, global_metadata = self.operations.load(session)
            for selection in session.scalars(
                select(ShopeeDiscoveryProduct)
                .order_by(ShopeeDiscoveryProduct.priority.desc(), ShopeeDiscoveryProduct.product_id)
                .limit(global_preferences.max_results_per_evaluation)
            ):
                policy = session.get(ShopeeDiscoveryPolicy, selection.category_id)
                if not policy or not policy.config["enabled"]:
                    continue
                config = DiscoveryPolicyInput.model_validate(policy.config)
                product = session.get(Product, selection.product_id)
                history = latest_observation(session, product.id)
                term = SearchTerm.model_validate(selection.plan["term"])
                disposition, relevance_score, reason = relevance(
                    product.name, term, config.exclusions, selection.manual_decision
                )
                for destination_id in config.destinations:
                    destination = session.get(PublicationDestination, destination_id)
                    preferences, metadata = self.operations.load(session, destination_id, config)
                    reasons = []
                    if not preferences.open_at(self.clock()):
                        reasons.append("OUTSIDE_DESTINATION_WINDOW")
                    if (
                        preferences.enabled_category_ids
                        and selection.category_id not in preferences.enabled_category_ids
                    ):
                        reasons.append("OPERATIONAL_CATEGORY_FILTER")
                    destination_policy = session.get(SimulationPolicy, destination_id)
                    if destination_policy:
                        if not destination_policy.enabled:
                            reasons.append("DESTINATION_POLICY_PAUSED")
                        if not matches_product(
                            product.id,
                            product.name,
                            destination_policy.product_ids,
                            destination_policy.terms,
                            conservative=True,
                        ):
                            reasons.append("DESTINATION_PRODUCT_FILTER")
                        if destination_policy.max_price is not None and (
                            history is None
                            or history.current_price is None
                            or history.current_price > destination_policy.max_price
                        ):
                            reasons.append("DESTINATION_MAX_PRICE_FILTER")
                    if selection.state != "SELECTED" or disposition != "SELECTED":
                        reasons.append(reason if disposition != "SELECTED" else "MANUAL_REVIEW")
                    if (
                        history is None
                        or history.availability != Availability.AVAILABLE
                        or history.price_access != PriceAccess.PUBLIC
                    ):
                        reasons.append("UNVERIFIED_STOCK_OR_PUBLIC_PRICE")
                    if not has_own_affiliate_link(product, require_verified=True):
                        reasons.append("OWN_AFFILIATE_LINK_REQUIRED")
                    if not destination or not destination.enabled:
                        reasons.append("DESTINATION_PAUSED")
                    elif destination.channel != DestinationChannel.SIMULATED:
                        reasons.append("SIMULATED_DESTINATION_REQUIRED")
                    elif not destination_accepts(session, product, destination):
                        reasons.append("DESTINATION_CATEGORY_FILTER")
                    category = session.get(Category, selection.category_id)
                    active = {
                        link.category.slug
                        for link in CategoryService().classify(session, product)
                        if link.category.active
                    }
                    if not category.active or category.slug not in active:
                        reasons.append("CATEGORY_INACTIVE_OR_CHANGED")
                    if not matches_product(
                        product.id,
                        product.name,
                        config.product_ids,
                        config.terms,
                        conservative=True,
                    ):
                        reasons.append("PRODUCT_FILTER")
                    if config.max_price is not None and (
                        history is None
                        or history.current_price is None
                        or history.current_price > config.max_price
                    ):
                        reasons.append("MAX_PRICE_FILTER")
                    comparison = (
                        PriceComparisonService().compare(
                            session,
                            history,
                            now=self.clock(),
                            max_age_seconds=self.settings.price_max_age_seconds,
                        )
                        if history
                        else None
                    )
                    if not comparison or comparison.direction != PriceDirection.DROP:
                        reasons.append("FRESH_COMPARABLE_DROP_REQUIRED")
                    elif (
                        config.min_discount is not None
                        and -comparison.percentage < config.min_discount
                    ):
                        reasons.append("MIN_VERIFIED_DISCOUNT_FILTER")
                    discount = (
                        -comparison.percentage
                        if comparison and comparison.direction == PriceDirection.DROP
                        else None
                    )
                    exceptional = (
                        preferences.exceptional_enabled
                        and discount is not None
                        and discount >= preferences.exceptional_min_discount
                    )
                    required_discount = (
                        preferences.exceptional_min_discount
                        if exceptional
                        else preferences.normal_min_discount
                    )
                    required_score = (
                        preferences.exceptional_min_score
                        if exceptional
                        else preferences.normal_min_score
                    )
                    score = (
                        self._score(
                            session,
                            product.id,
                            {"verified_change": self._json(asdict(comparison))},
                            config,
                            term,
                            relevance_score,
                            preferences=preferences,
                        )
                        if comparison
                        else Decimal(0)
                    )
                    if score < required_score:
                        reasons.append("MIN_SCORE")
                    if discount is None or discount < required_discount:
                        reasons.append("OPERATIONAL_DISCOUNT_THRESHOLD")
                    if (
                        destination_policy
                        and destination_policy.min_discount is not None
                        and (discount is None or discount < destination_policy.min_discount)
                    ):
                        reasons.append("DESTINATION_MIN_DISCOUNT_FILTER")
                    previous = session.scalars(
                        select(ShopeeDiscoveryRun).where(
                            ShopeeDiscoveryRun.operation == "EVALUATE",
                            ShopeeDiscoveryRun.status == "SUCCEEDED",
                            ShopeeDiscoveryRun.finished_at
                            > self.clock()
                            - timedelta(
                                seconds=max(
                                    preferences.quota_period_seconds,
                                    global_preferences.quota_period_seconds,
                                    3600,
                                )
                            ),
                        )
                    ).all()
                    approved = [
                        {**r, "_at": prev.finished_at}
                        for prev in previous
                        for r in prev.result.get("candidates", [])
                        if r.get("decision") == "MOCK_WOULD_APPROVE"
                    ] + [
                        {**r, "_at": self.clock()}
                        for r in results
                        if r.get("decision") == "MOCK_WOULD_APPROVE"
                    ]
                    period = [
                        r
                        for r in approved
                        if r["_at"]
                        > self.clock() - timedelta(seconds=preferences.quota_period_seconds)
                    ]
                    if (
                        sum(
                            r["_at"]
                            > self.clock()
                            - timedelta(seconds=global_preferences.quota_period_seconds)
                            for r in approved
                        )
                        >= global_preferences.max_predictions_per_period
                    ):
                        reasons.append("GLOBAL_PREDICTION_LIMIT")
                    scope_period = [r for r in period if r["destination_id"] == destination_id]
                    if len(scope_period) >= preferences.max_predictions_per_period:
                        reasons.append("SCOPE_PREDICTION_LIMIT")
                    if (
                        sum(r["category_id"] == selection.category_id for r in scope_period)
                        >= preferences.max_predictions_per_category_per_period
                    ):
                        reasons.append("CATEGORY_PREDICTION_LIMIT")
                    if (
                        sum(r["destination_id"] == destination_id for r in period)
                        >= preferences.max_predictions_per_destination_per_period
                    ):
                        reasons.append("DESTINATION_PREDICTION_LIMIT")
                    destination_history = [
                        r for r in approved if r["destination_id"] == destination_id
                    ]
                    if (
                        destination
                        and sum(
                            r["_at"] > self.clock() - timedelta(hours=1)
                            for r in destination_history
                        )
                        >= destination.max_messages_per_hour
                    ):
                        reasons.append("DESTINATION_NATIVE_HOURLY_LIMIT")
                    if destination and any(
                        r["_at"]
                        > self.clock() - timedelta(seconds=destination.min_interval_seconds)
                        for r in destination_history
                    ):
                        reasons.append("DESTINATION_MIN_INTERVAL")
                    item_data = {
                        "history_id": history.id if history else None,
                        "candidate_id": selection.candidate_id,
                        "score": str(score),
                        "opportunity_kind": "EXCEPTIONAL" if exceptional else "NORMAL",
                        "preference_revisions": metadata,
                        "operational_preferences": preferences.model_dump(mode="json"),
                        "evaluated_at": self.clock().isoformat(),
                        "product_id": product.id,
                        "category_id": selection.category_id,
                        "destination_id": destination_id,
                        "reasons": reasons,
                        "decision": "BLOCKED",
                        "publication_authorized": False,
                        "real_send_calls": 0,
                    }
                    if _link_results and (product.id, destination_id) in _link_results:
                        item_data["affiliate_link"] = _link_results[(product.id, destination_id)]
                    if (
                        preparing
                        and preferences.affiliate_links_simulation_enabled
                        and reasons == ["OWN_AFFILIATE_LINK_REQUIRED"]
                    ):
                        pending_links.append(
                            (
                                product.id,
                                destination_id,
                                history.id,
                                preferences.max_affiliate_links_per_evaluation,
                            )
                        )
                    if not reasons and not preparing:
                        existing = session.scalar(
                            select(PublicationQueueItem).where(
                                PublicationQueueItem.product_id == product.id,
                                PublicationQueueItem.destination_id == destination_id,
                                PublicationQueueItem.active_slot == 1,
                            )
                        )
                        if existing:
                            reasons.append("ACTIVE_DUPLICATE")
                        else:
                            offer = OfferEngine(self.settings, self.clock).process(
                                session, product, history, enqueue=False
                            )
                            queue = SimulatedQueueService(self.settings, self.clock)
                            item = queue.enqueue(session, offer, destination) if offer else None
                            if item is None:
                                reasons.append("COOLDOWN_OR_REVALIDATION")
                            else:
                                item.status, item.validated_at = QueueStatus.PENDING, None
                                item.blocked_reason = "DISCOVERY_DRY_RUN_ONLY"
                                offer.status = OfferStatus.PENDING
                                rendered = render_message(
                                    MessageData(
                                        product.name,
                                        history.current_price,
                                        product.affiliate_link.url,
                                    )
                                )
                                item_data.update(
                                    decision="MOCK_WOULD_APPROVE",
                                    queue_id=item.id,
                                    preview=rendered.text,
                                    preview_hash=hashlib.sha256(rendered.text.encode()).hexdigest(),
                                    queue_status="PENDING",
                                    simulated=True,
                                )
                    results.append(item_data)
            self._fence(session, owners)
            if preparing:
                # Release the evaluation transaction before the independently
                # committed reservation/mock I/O, especially on SQLite.
                session.commit()
            else:
                self._finish(
                    session,
                    run_id,
                    {
                        "candidates": results,
                        "publication_calls": 0,
                        "operational_preferences": global_preferences.model_dump(mode="json"),
                        "preference_revisions": global_metadata,
                    },
                )
        if preparing:
            link_results = {}
            counts = {}
            started = 0
            for product_id, destination_id, history_id, limit in pending_links:
                if (
                    started >= global_preferences.max_affiliate_links_per_evaluation
                    or counts.get(destination_id, 0) >= limit
                ):
                    continue
                # Renew the existing discovery fence between bounded operations.
                with self.factory() as session:
                    self._fence(session, owners)
                    session.commit()
                try:
                    result = self.affiliate_links.process(
                        product_id,
                        destination_id,
                        history_id,
                        lease_seconds=global_preferences.lease_seconds,
                    )
                except (IntegrationError, ValueError):
                    result = {
                        "status": "BLOCKED",
                        "error_code": "AFFILIATE_SAFEGUARD",
                        "real_calls": 0,
                    }
                link_results[(product_id, destination_id)] = result
                if not result.get("reused") and result.get("attempts") == 1:
                    started += 1
                    counts[destination_id] = counts.get(destination_id, 0) + 1
            # Reuse all original filters, quotas, deduplication and revalidation.
            self._evaluate(run_id, owners, _prepare_links=False, _link_results=link_results)

    def manual_decision(self, product_id, decision, actor):
        self._offline()
        if decision not in ("ACCEPT", "REJECT"):
            raise ValueError("Decisão inválida.")
        with self.factory() as session:
            row = session.scalar(
                select(ShopeeDiscoveryProduct)
                .where(ShopeeDiscoveryProduct.product_id == product_id)
                .with_for_update()
            )
            if row is None:
                raise LookupError("Produto não selecionado.")
            before = row.manual_decision
            row.manual_decision, row.manual_actor = decision, actor
            row.state = "PAUSED" if decision == "REJECT" else "SELECTED"
            audit = self._audit(
                session,
                "CONFIGURE",
                {"actor": actor, "product_id": product_id, "before": before},
                {"decision": decision},
            )
            session.commit()
            return {"product_id": product_id, "decision": decision, "audit_id": audit}

    def _recover(self, run_id):
        with self.factory() as session:
            rows = session.scalars(
                select(ShopeeDiscoveryRun).where(
                    ShopeeDiscoveryRun.status == "RESERVED",
                    ShopeeDiscoveryRun.id != run_id,
                    ShopeeDiscoveryRun.started_at
                    < self.clock()
                    - timedelta(seconds=self.operations.load(session)[0].lease_seconds),
                )
            ).all()
            for row in rows:
                row.status, row.error_code, row.finished_at = (
                    "UNCERTAIN",
                    "ABANDONED_EXECUTION",
                    self.clock(),
                )
            self._finish(session, run_id, {"recovered": [row.id for row in rows], "retry": False})

    def _finish(self, session, run_id, result):
        row = session.get(ShopeeDiscoveryRun, run_id)
        row.status, row.finished_at, row.result = "SUCCEEDED", self.clock(), self._json(result)
        session.commit()

    def refresh_schedule(self):
        runtime = self._prepared_runtime
        if runtime is None:
            return
        with self.factory() as session:
            preferences, _ = self.operations.load(session)
        intervals = (
            preferences.discovery_interval_seconds,
            preferences.monitor_interval_seconds,
            preferences.evaluation_interval_seconds,
        )
        for job_id, interval in zip(JOB_IDS, intervals, strict=True):
            runtime.update_prepared_job_interval(job_id, interval)

    def prepare_schedule(self, runtime, *, offline=False):
        self.collection._safe()
        if offline:
            self._offline()
        with self.factory() as session:
            preferences, _ = self.operations.load(session)
        self._prepared_runtime = runtime
        intervals = (
            preferences.discovery_interval_seconds,
            preferences.monitor_interval_seconds,
            preferences.evaluation_interval_seconds,
        )
        for job_id, operation, interval in zip(
            JOB_IDS, ("DISCOVER", "MONITOR", "EVALUATE"), intervals, strict=True
        ):
            callback = (
                (lambda op=operation: self.execute(uuid4(), op)) if offline else self.blocked_tick
            )
            runtime.prepare_paused_job(job_id, callback, interval)
        runtime.observe_configuration("shopee-discovery-operations", self.refresh_schedule)
        return {
            "status": "PAUSED",
            "jobs": list(JOB_IDS),
            "mode": "OFFLINE_ONLY" if offline else "BLOCKED",
            "real_activation": False,
        }

    @staticmethod
    def blocked_tick():
        return {"status": "BLOCKED", "reason": "FUTURE_OPERATIONAL_AUTHORIZATION_REQUIRED"}
