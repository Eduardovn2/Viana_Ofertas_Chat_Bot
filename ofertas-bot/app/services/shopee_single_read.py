"""One authorized identity read; reuse collection reservations, never ingest a quote.

Preparation and approval perform no HTTP. Mock authorizations cannot become real.
No caller, flag or scheduler can bypass the durable exact-scope claim.
"""

import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import func, select, text, update

from app.integrations.contracts import ProductRef, fingerprint
from app.integrations.errors import IntegrationError, http_diagnostics
from app.integrations.registry import ConnectorRegistry
from app.integrations.shopee import ENDPOINT, ShopeeConnector
from app.models import AppSetting, IntegrationEvent, Platform, ShopeeCollectionRun
from app.services.comparison import aware_utc
from app.services.discovery_operations import DiscoveryOperations
from app.services.leases import acquire_lease, conflict_insert, release_lease, renew_lease
from app.services.shopee_collection import ObservationRequest
from app.services.simulation_safety import require_simulation_scope

PREFIX = "shopee_identity_read:"
LEASE = "shopee:controlled-collection"
TERMINAL = {"SUCCEEDED", "FAILED", "UNCERTAIN", "EXPIRED"}


class ReadConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=3, max_length=80, pattern=r"^[\w.@-]+$")
    confirmed_single_read: StrictBool

    @field_validator("confirmed_single_read")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("Confirmação explícita de uma leitura é obrigatória.")
        return value


class ReadApproval(ReadConfirmation):
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value):
        return aware_utc(value)


class ShopeeSingleReadAdapter:
    reservation_prefix = "identity-read:"
    reservation_operation = "OBSERVE_NO_INGEST"

    def __init__(self, collection):
        self.collection = collection
        self.factory, self.settings, self.clock = (
            collection.factory,
            collection.settings,
            collection.clock,
        )

    @staticmethod
    def key(authorization_id):
        return PREFIX + str(UUID(str(authorization_id)))

    def _mode(self, offline):
        self.collection._safe()
        if self.settings.shopee_short_links_enabled:
            raise ValueError("Geração de links deve permanecer desativada.")
        with self.factory() as session:
            if offline:
                require_simulation_scope(session, self.settings)
                return "MOCK_ONLY"
            if self.settings.app_env == "test" or session.get_bind().dialect.name != "postgresql":
                raise ValueError("Testes exigem modo mockado e banco isolado.")
            database, schema = session.execute(
                text("SELECT current_database(), current_schema()")
            ).one()
            if database.endswith("_test") or schema != "public":
                raise ValueError("Autorização real não pode consumir registros do laboratório.")
        return "REAL_SINGLE_READ"

    def _scope(self, session, request, mode):
        product = self.collection._observation_product(session, request, lock=False)
        ref = ProductRef(Platform.SHOPEE, product.external_id, product.original_url)
        query = (
            ShopeeConnector.product_query(ref, include_offer_link=True)
            if request.include_offer_link
            else ShopeeConnector.product_query(ref)
        )
        body = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":")).encode()
        credential = ConnectorRegistry(self.settings).credential_scope(Platform.SHOPEE)
        scope = {
            "operation": "productOfferV2",
            "mode": mode,
            "product_id": product.id,
            "shop_id": request.shop_id,
            "item_id": request.item_id,
            "variant_id": "",
            "endpoint": ENDPOINT,
            "origin_url_sha256": hashlib.sha256(product.original_url.encode()).hexdigest(),
            "credential_scope": credential,
            "payload_sha256": hashlib.sha256(body).hexdigest(),
            "page": 1,
            "limit": 1,
            "max_http_calls": 1,
            "ingest": False,
            "persist_quote": False,
        }
        if request.include_offer_link:
            from app.services.shopee_evidence import account_evidence, credential_binding

            proof = account_evidence(session, self.settings, now=self.clock())
            if proof is None:
                raise ValueError("Leitura de offerLink exige prova vigente da conta/credencial.")
            scope.update(
                include_offer_link=True,
                account_evidence_id=proof["id"],
                credential_fingerprint=credential_binding(self.settings),
            )
        return scope, ref, query

    def plan(self, data, *, offline=False):
        mode = self._mode(offline)
        request = ObservationRequest.model_validate(data)
        with self.factory() as session:
            scope, _, query = self._scope(session, request, mode)
        return {
            "authorization_id": str(request.request_id),
            "status": "PLAN_ONLY",
            "mode": mode,
            "scope_sha256": fingerprint(scope),
            "payload": {"query": query},
            "max_http_calls": 1,
            "price_history_writes": 0,
            "publication_authorized": False,
        }

    @staticmethod
    def _row(session, key):
        return session.scalar(
            select(AppSetting)
            .where(AppSetting.key == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _audit(self, session, value, operation, status):
        session.add(
            IntegrationEvent(
                platform=Platform.SHOPEE,
                product_id=value["request"]["product_id"],
                operation="identity_read_" + operation,
                status=status,
                error_code=value.get("error_code"),
                observed_at=self.clock(),
                source_reference="identity-read:" + value["request"]["request_id"],
            )
        )

    def prepare(self, data, *, offline=False):
        mode = self._mode(offline)
        request = ObservationRequest.model_validate(data)
        key = self.key(request.request_id)
        with self.factory() as session:
            scope, _, _ = self._scope(session, request, mode)
            value = {
                "version": 1,
                "mode": mode,
                "state": "PREPARED",
                "request": request.model_dump(mode="json"),
                "scope": scope,
                "scope_sha256": fingerprint(scope),
                "consumed": False,
                "prepared_at": self.clock().isoformat(),
                "approval": None,
                "result": {},
            }
            if session.get(ShopeeCollectionRun, str(request.request_id)) is not None:
                raise ValueError(
                    "UUID já utilizado; autorizações históricas não são reutilizáveis."
                )
            inserted = session.execute(
                conflict_insert(session, AppSetting)
                .values(
                    key=key, value=value, description="Uma leitura por identidade; sem ingestão."
                )
                .on_conflict_do_nothing(index_elements=[AppSetting.key])
                .returning(AppSetting.id)
            ).scalar_one_or_none()
            row = self._row(session, key)
            if row.value["request"] != value["request"] or row.value["scope"] != scope:
                raise ValueError("Autorização existente possui outro escopo ou modo.")
            if inserted is not None:
                self._audit(session, value, "prepared", "PREPARED")
            session.commit()
        return self.read(request.request_id)

    def read(self, authorization_id):
        authorization_id = str(UUID(str(authorization_id)))
        key = self.key(authorization_id)
        with self.factory() as session:
            row = session.scalar(select(AppSetting).where(AppSetting.key == key))
            if row is None:
                raise LookupError("Autorização de leitura inexistente.")
            value = row.value
            run = session.get(ShopeeCollectionRun, str(authorization_id))
            return {
                "authorization_id": str(authorization_id),
                "mode": value["mode"],
                "status": "UNCERTAIN" if value["state"] == "CLAIMED" else value["state"],
                "stored_state": value["state"],
                "scope_sha256": value["scope_sha256"],
                "consumed": value["consumed"],
                "requests_reserved": run.requests if run else 0,
                "approval": value["approval"],
                "result": value["result"],
                "error_code": value.get("error_code"),
                "publication_authorized": False,
                "price_history_writes": 0,
            }

    def approve(self, authorization_id, data, *, offline=False):
        authorization_id = str(UUID(str(authorization_id)))
        mode = self._mode(offline)
        approval = ReadApproval.model_validate(data)
        with self.factory() as session:
            row = self._row(session, self.key(authorization_id))
            if row is None:
                raise LookupError("Autorização de leitura inexistente.")
            value = row.value
            self._validate_scope(session, value, mode, approval.scope_sha256)
            preferences, _ = DiscoveryOperations().load(session)
            if (
                not self.clock()
                < approval.expires_at
                <= self.clock() + timedelta(seconds=preferences.quota_period_seconds)
            ):
                raise ValueError("Validade da aprovação fora da janela administrativa configurada.")
            if value["state"] != "PREPARED":
                if value["state"] == "APPROVED" and value["approval"] == approval.model_dump(
                    mode="json"
                ):
                    return self.read(authorization_id)
                raise ValueError("Autorização já aprovada, consumida ou encerrada.")
            row.value = {**value, "state": "APPROVED", "approval": approval.model_dump(mode="json")}
            self._audit(session, row.value, "approved", "APPROVED")
            session.commit()
        return self.read(authorization_id)

    def _validate_scope(self, session, value, mode, expected_hash):
        request = ObservationRequest.model_validate(value["request"])
        scope, ref, query = self._scope(session, request, mode)
        if (
            value["mode"] != mode
            or value["scope"] != scope
            or value["scope_sha256"] != fingerprint(scope)
            or expected_hash != fingerprint(scope)
        ):
            raise ValueError("Conteúdo, credencial, produto ou modo diverge do escopo aprovado.")
        return request, ref, query

    def _real_registry(self):
        effective = self.settings.model_copy(
            update={
                "shopee_enabled": True,
                "shopee_short_links_enabled": False,
                "integration_max_retries": 0,
                "integration_cache_seconds": 0,
                "integration_max_retry_wait_seconds": 0,
            }
        )
        return ConnectorRegistry(effective, session_factory=self.factory, clock=self.clock)

    def _validate_dispatch_window(self, value):
        """Specialized one-shot searches may seal a start time as well as expiry."""

    def _dispatch(self, connector, ref):
        return connector.fetch_product(ref)

    def _sanitize_dispatched(self, observation, calls, mode, ref):
        return self._sanitized(observation, calls, mode)

    def _result_summary(self, quote, calls, mode):
        return {
            "http_calls": calls,
            "mode": mode,
            "http_status": quote["http_status"],
            "quote_sha256": fingerprint(quote),
            "quote_persisted": False,
        }

    def execute(self, authorization_id, data, *, offline=False, connector=None):
        authorization_id = str(UUID(str(authorization_id)))
        mode = self._mode(offline)
        confirmation = ReadConfirmation.model_validate(data)
        if offline:
            if (
                type(connector) is not ShopeeConnector
                or type(connector.transport.client._transport_for_url(httpx.URL(ENDPOINT)))
                is not httpx.MockTransport
            ):
                raise ValueError("Teste de leitura única exige ShopeeConnector com MockTransport.")
        elif connector is not None:
            raise ValueError("Execução real não aceita transporte injetado.")
        with self.factory() as session:
            preferences, _ = DiscoveryOperations().load(session)
        owner = acquire_lease(self.factory, LEASE, preferences.lease_seconds, self.clock)
        if owner is None:
            raise ValueError(
                "Outra coleta está em execução; nenhuma nova tentativa foi autorizada."
            )
        registry, hooked, claimed = None, False, False
        quote = None
        calls, response_codes = [0], []
        try:
            with self.factory() as session:
                row = self._row(session, self.key(authorization_id))
                if row is None:
                    raise LookupError("Autorização de leitura inexistente.")
                value = row.value
                request, ref, query = self._validate_scope(
                    session, value, mode, confirmation.scope_sha256
                )
                if value["state"] in TERMINAL or value["state"] == "CLAIMED":
                    return self.read(authorization_id)
                if value["state"] != "APPROVED":
                    raise ValueError("Aprovação explícita do escopo é obrigatória.")
                if self.clock() >= datetime.fromisoformat(value["approval"]["expires_at"]):
                    row.value = {**value, "state": "EXPIRED"}
                    self._audit(session, row.value, "expired", "EXPIRED")
                    session.commit()
                    return self.read(authorization_id)
                self._validate_dispatch_window(value)
                since = self.clock() - timedelta(seconds=preferences.quota_period_seconds)
                used = session.scalar(
                    select(func.coalesce(func.sum(ShopeeCollectionRun.requests), 0)).where(
                        ShopeeCollectionRun.created_at >= since
                    )
                )
                if used >= preferences.max_queries_per_period:
                    raise ValueError("Orçamento local esgotado; não comprova quota oficial.")
                if not renew_lease(session, LEASE, owner, preferences.lease_seconds, self.clock):
                    raise ValueError("Lease perdido antes do claim.")
                claimed_value = {
                    **value,
                    "state": "CLAIMED",
                    "consumed": True,
                    "claimed_at": self.clock().isoformat(),
                    "executor": confirmation.actor,
                    "claim_owner": owner,
                }
                changed = session.execute(
                    update(AppSetting)
                    .where(
                        AppSetting.key == self.key(authorization_id),
                        AppSetting.value["state"].as_string() == "APPROVED",
                        AppSetting.value["scope_sha256"].as_string() == confirmation.scope_sha256,
                    )
                    .values(value=claimed_value)
                    .execution_options(synchronize_session=False)
                ).rowcount
                if changed != 1:
                    raise ValueError("Autorização já reclamada; nenhuma chamada executada.")
                inserted = session.execute(
                    conflict_insert(session, ShopeeCollectionRun)
                    .values(
                        id=str(authorization_id),
                        mode="OFFLINE" if offline else "REAL",
                        status="RESERVED",
                        requests=1,
                        actor=confirmation.actor,
                        authorization=self.reservation_prefix + str(authorization_id),
                        specification={
                            "operation": self.reservation_operation,
                            "scope_sha256": confirmation.scope_sha256,
                        },
                        result={},
                        created_at=self.clock(),
                    )
                    .on_conflict_do_nothing(index_elements=[ShopeeCollectionRun.id])
                    .returning(ShopeeCollectionRun.id)
                ).scalar_one_or_none()
                if inserted is None:
                    raise ValueError(
                        "UUID reservado anteriormente; nenhuma autorização histórica será consumida."
                    )
                self._audit(session, claimed_value, "claimed", "CLAIMED")
                session.commit()
                claimed = True
            if not offline:
                registry = self._real_registry()
                if registry.is_blocked(Platform.SHOPEE):
                    raise IntegrationError(
                        "AUTHORIZATION_DENIED", "Conta bloqueada; revisão necessária."
                    )
                connector = registry.get(Platform.SHOPEE)
            connector.transport.max_retries = connector.transport.cache_seconds = 0
            connector.transport.max_retry_wait = 0
            connector.transport.clear_cache()

            def guard(http_request):
                if (
                    calls[0]
                    or http_request.method != "POST"
                    or str(http_request.url) != ENDPOINT
                    or json.loads(http_request.content) != {"query": query}
                    or hashlib.sha256(http_request.content).hexdigest()
                    != value["scope"]["payload_sha256"]
                ):
                    raise IntegrationError("SCOPE_VIOLATION", "HTTP fora da autorização exata.")
                self.collection._safe()
                if self.settings.shopee_short_links_enabled:
                    raise IntegrationError(
                        "SCOPE_VIOLATION", "Geração de links deve continuar bloqueada."
                    )
                with self.factory() as session:
                    current = self._row(session, self.key(authorization_id))
                    run = session.get(ShopeeCollectionRun, str(authorization_id))
                    if (
                        current.value["state"] != "CLAIMED"
                        or not current.value["consumed"]
                        or current.value.get("claim_owner") != owner
                        or run is None
                        or run.requests != 1
                        or run.status != "RESERVED"
                        or run.specification.get("scope_sha256") != confirmation.scope_sha256
                    ):
                        raise IntegrationError(
                            "SCOPE_VIOLATION", "Claim não está disponível para este HTTP."
                        )
                    if self.clock() >= datetime.fromisoformat(
                        current.value["approval"]["expires_at"]
                    ):
                        raise IntegrationError(
                            "SCOPE_VIOLATION", "Aprovação expirada antes do HTTP."
                        )
                    self._validate_scope(session, current.value, mode, confirmation.scope_sha256)
                    self._validate_dispatch_window(current.value)
                    if not renew_lease(
                        session, LEASE, owner, preferences.lease_seconds, self.clock
                    ):
                        raise IntegrationError("LEASE_LOST", "Lease perdido antes do HTTP.")
                    session.commit()
                calls[0] += 1

            def response_code(response):
                response_codes.append(response.status_code)

            connector.transport.client.event_hooks["request"].append(guard)
            connector.transport.client.event_hooks["response"].append(response_code)
            hooked = True
            if getattr(request, "include_offer_link", False):
                snapshot = connector.fetch_product(ref, include_offer_link=True)
            else:
                snapshot = self._dispatch(connector, ref)
            quote = self._sanitize_dispatched(snapshot, calls[0], mode, ref)
            quote["http_status"] = response_codes[-1] if response_codes else None
            if getattr(request, "include_offer_link", False):
                quote.update(
                    official_offer_link=snapshot.evidence_metadata["official_offer_link"],
                    account_evidence_id=value["scope"]["account_evidence_id"],
                    credential_fingerprint=value["scope"]["credential_fingerprint"],
                    link_source="productOfferV2.offerLink",
                    link_ownership_automatically_attested=False,
                )
            result = self._result_summary(quote, calls[0], mode)
            if getattr(request, "include_offer_link", False):
                result.update(quote_persisted=True, offer_link_observation=quote)
            self._complete(
                authorization_id, "SUCCEEDED", result, None, owner, preferences.lease_seconds
            )
        except BaseException as error:
            quote = None
            if not claimed:
                raise
            code = error.code if isinstance(error, IntegrationError) else "LOCAL_RESULT_UNCERTAIN"
            status = (
                "FAILED"
                if code
                in {
                    "AUTHORIZATION_DENIED",
                    "UPSTREAM_GRAPHQL_ERROR",
                    "INVALID_PAYLOAD",
                    "PRODUCT_NOT_FOUND",
                    "UPSTREAM_REJECTED",
                    "RATE_LIMITED",
                    "SCOPE_VIOLATION",
                }
                else "UNCERTAIN"
            )
            try:
                self._complete(
                    authorization_id,
                    status,
                    {
                        "http_calls": calls[0],
                        "mode": mode,
                        "http_status": response_codes[-1] if response_codes else None,
                        "http_diagnostics": http_diagnostics(error),
                    },
                    code,
                )
            except BaseException:
                # The durable claim is already consumed. Never reconstruct or retry the HTTP.
                return {
                    "authorization_id": str(authorization_id),
                    "status": "UNCERTAIN",
                    "consumed": True,
                    "requests_reserved": 1,
                    "error_code": "AUDIT_PERSISTENCE_UNCERTAIN",
                    "price_history_writes": 0,
                    "publication_authorized": False,
                }
        finally:
            if hooked:
                connector.transport.client.event_hooks["request"].remove(guard)
                connector.transport.client.event_hooks["response"].remove(response_code)
            if registry is not None:
                registry.close()
            try:
                release_lease(self.factory, LEASE, owner)
            except Exception:
                pass  # Expiring lease is never authority to retry an already consumed claim.
        saved = self.read(authorization_id)
        return {**saved, "quote": quote} if quote is not None else saved

    def _complete(
        self, authorization_id, status, result, error_code, owner=None, lease_seconds=None
    ):
        with self.factory() as session:
            row = self._row(session, self.key(authorization_id))
            if row.value["state"] != "CLAIMED":
                raise ValueError("Resultado não pertence a um claim pendente.")
            if owner and not renew_lease(session, LEASE, owner, lease_seconds, self.clock):
                raise IntegrationError(
                    "LEASE_LOST", "Resultado não confirmado após perda de lease."
                )
            row.value = {
                **row.value,
                "state": status,
                "finished_at": self.clock().isoformat(),
                "result": result,
                "error_code": error_code,
            }
            run = session.get(ShopeeCollectionRun, str(authorization_id))
            run.status, run.result, run.error_code, run.finished_at = (
                status,
                result,
                error_code,
                self.clock(),
            )
            self._audit(session, row.value, "result", status)
            session.commit()

    def _sanitized(self, snapshot, calls, mode):
        metadata = snapshot.evidence_metadata
        name = "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in snapshot.name)
        for secret in (self.settings.shopee_app_id, self.settings.shopee_secret):
            if secret and secret.get_secret_value():
                name = name.replace(secret.get_secret_value(), "[REDACTED]")
        return {
            "http_calls": calls,
            "mode": mode,
            "identity": snapshot.external_item_id,
            "name": name,
            "currency": snapshot.currency,
            "price": str(snapshot.current_price) if snapshot.current_price is not None else None,
            "price_min": str(snapshot.reported_price_min)
            if snapshot.reported_price_min is not None
            else None,
            "price_max": str(snapshot.reported_price_max)
            if snapshot.reported_price_max is not None
            else None,
            "advertised_discount_percent": metadata.get("advertised_discount_percent"),
            "offer_period_start_raw": metadata.get("offer_period_start_raw"),
            "offer_period_end_raw": metadata.get("offer_period_end_raw"),
            "period_time_unit": "UNCONFIRMED",
            "period_semantics": "UNCONFIRMED",
            "availability": "UNKNOWN",
            "price_access": "UNKNOWN",
            "commercial_verified": False,
            "observed_at": snapshot.observed_at.isoformat(),
            "price_history_writes": 0,
            "publication_authorized": False,
        }
