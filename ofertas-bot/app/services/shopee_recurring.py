"""Bounded identity reads: isolated recurring laboratory and separately approved pilot.

General operational recurrence stays blocked; the narrow experimental pilot requires
its own explicit approval. No scheduler is installed. Flags never authorize HTTP.
"""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from sqlalchemy import select, text, update

from app.integrations.contracts import fingerprint
from app.integrations.registry import ConnectorRegistry
from app.models import AppSetting, IntegrationEvent, Platform, Product
from app.services.comparison import PriceComparisonService, aware_utc
from app.services.discovery_operations import DiscoveryOperations
from app.services.leases import conflict_insert
from app.services.monitoring import record_price
from app.services.shopee_collection import ObservationRequest
from app.services.shopee_quote_ingestion import quote_snapshot
from app.services.shopee_single_read import ShopeeSingleReadAdapter

PREFIX = "shopee_recurring:"


class RecurringScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: UUID
    product_id: int = Field(strict=True, gt=0)
    shop_id: str
    item_id: str
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    valid_from: datetime
    expires_at: datetime
    max_requests: int = Field(strict=True, ge=1, le=100)
    min_interval_seconds: int = Field(strict=True, ge=60, le=604800)

    @field_validator("valid_from", "expires_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)

    @model_validator(mode="after")
    def bounded(self):
        if not self.valid_from < self.expires_at <= self.valid_from + timedelta(days=7):
            raise ValueError("Autorize uma janela positiva de no máximo sete dias.")
        ObservationRequest.model_validate(self.request(self.authorization_id))
        return self

    def request(self, identity):
        return dict(
            request_id=str(identity),
            product_id=self.product_id,
            shop_id=self.shop_id,
            item_id=self.item_id,
            actor=self.actor,
        )


class RecurringApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    confirmed_recurring_read: StrictBool
    quotas_confirmed: StrictBool
    quota_evidence: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def explicit(self):
        if self.confirmed_recurring_read is not True or self.quotas_confirmed is not True:
            raise ValueError("Autorização e evidência de quotas são obrigatórias.")
        return self


class PilotConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    confirmed_experimental_pilot: StrictBool
    acknowledge_unknown_quotas: StrictBool

    @model_validator(mode="after")
    def explicit(self):
        if (
            self.confirmed_experimental_pilot is not True
            or self.acknowledge_unknown_quotas is not True
        ):
            raise ValueError(
                "Piloto exige autorização e ciência explícita das quotas desconhecidas."
            )
        return self


class ShopeeRecurringMonitor:
    def __init__(self, collection):
        self.single = ShopeeSingleReadAdapter(collection)
        self.factory, self.settings, self.clock = (
            collection.factory,
            collection.settings,
            collection.clock,
        )

    def _laboratory(self, offline):
        if offline is not True:
            raise ValueError(
                "Recorrência real bloqueada: exige liberação posterior e quotas oficiais."
            )
        self.single._mode(True)

    @staticmethod
    def _key(identity):
        return PREFIX + str(UUID(str(identity)))

    def _row(self, session, identity):
        row = self.single._row(session, self._key(identity))
        if row is None:
            raise LookupError("Autorização recorrente inexistente.")
        return row

    def _audit(self, session, scope, status, reference):
        session.add(
            IntegrationEvent(
                platform=Platform.SHOPEE,
                product_id=scope.product_id,
                operation="recurring_observation",
                status=status,
                source_reference=reference,
                observed_at=self.clock(),
            )
        )

    def prepare(self, data, *, offline=False):
        self._laboratory(offline)
        scope = RecurringScope.model_validate(data)
        return self._prepare(scope, "MOCK_ONLY")

    def _pilot_mode(self, offline):
        mode = self.single._mode(offline)
        if not offline:
            with self.factory() as session:
                if session.execute(text("SELECT current_database(), current_schema()")).one() != (
                    "ofertas_bot",
                    "public",
                ):
                    raise ValueError("Piloto exige banco operacional correto.")
            if not self.settings.shopee_app_id or not self.settings.shopee_secret:
                raise ValueError("Configuração local indispensável ausente.")
            registry = ConnectorRegistry(
                self.settings, session_factory=self.factory, clock=self.clock
            )
            try:
                if not registry.configured(Platform.SHOPEE):
                    raise ValueError("Configuração local indispensável ausente.")
                if registry.is_blocked(Platform.SHOPEE):
                    raise ValueError("Conta bloqueada; piloto não pode contornar restrições.")
            finally:
                registry.close()
        return "MOCK_PILOT" if mode == "MOCK_ONLY" else "REAL_EXPERIMENTAL_PILOT"

    @staticmethod
    def _pilot_policy():
        path = Path(__file__).resolve().parents[2] / "docs/CONTRATO_FONTES_SHOPEE_20261026.json"
        raw = path.read_bytes()
        contract = json.loads(raw)
        if contract.get("recurring_queries_prohibited") is True:
            raise ValueError("Proibição conhecida; piloto bloqueado.")
        return dict(
            quotas="UNCONFIRMED",
            max_posts=2,
            minimum_spacing_seconds=21600,
            maximum_window_seconds=86400,
            publication_authorized=False,
            retry=False,
            contract_sha256=hashlib.sha256(raw).hexdigest(),
            known_periodic_prohibition="NOT_IDENTIFIED_IN_LOCAL_MATERIAL",
        )

    @staticmethod
    def _pilot_bounds(scope):
        if (
            scope.max_requests > 2
            or scope.min_interval_seconds < 21600
            or scope.expires_at - scope.valid_from > timedelta(hours=24)
        ):
            raise ValueError(
                "Piloto limitado a 2 POSTs, intervalo mínimo 6h e validade máxima 24h."
            )

    @staticmethod
    def _digest(value, read_scope):
        content = {"scope": value["scope"], "read_scope": read_scope}
        if value.get("pilot_policy") is not None:
            content["pilot_policy"] = value["pilot_policy"]
            content["mode"] = value["mode"]
        return fingerprint(content)

    def prepare_pilot(self, data, *, offline=False):
        """Persist PREPARED only: no approval, scheduler activation or HTTP."""
        mode = self._pilot_mode(offline)
        scope = RecurringScope.model_validate(data)
        self._pilot_bounds(scope)
        if not offline and (scope.product_id, scope.shop_id, scope.item_id) != (
            2,
            "1904573791",
            "23199869203",
        ):
            raise ValueError("Piloto restrito ao Cockpit autorizado.")
        return self._prepare(scope, mode)

    def _prepare(self, scope, mode):
        with self.factory() as session:
            read_scope, _, _ = self.single._scope(
                session,
                ObservationRequest.model_validate(scope.request(scope.authorization_id)),
                "REAL_SINGLE_READ" if mode == "REAL_EXPERIMENTAL_PILOT" else "MOCK_ONLY",
            )
            value = dict(
                mode=mode,
                scope=scope.model_dump(mode="json"),
                read_scope=read_scope,
                state="PREPARED",
                used=0,
                active_cycle=None,
                interval_seconds=scope.min_interval_seconds,
                next_due=scope.valid_from.isoformat(),
            )
            if mode != "MOCK_ONLY":
                value["pilot_policy"] = self._pilot_policy()
            value["scope_sha256"] = self._digest(value, read_scope)
            inserted = session.execute(
                conflict_insert(session, AppSetting)
                .values(
                    key=self._key(scope.authorization_id),
                    value=value,
                    description="Escopo recorrente; PREPARED não autoriza execução.",
                )
                .on_conflict_do_nothing(index_elements=[AppSetting.key])
                .returning(AppSetting.id)
            ).scalar_one_or_none()
            row = self._row(session, scope.authorization_id)
            if row.value["scope_sha256"] != value["scope_sha256"]:
                raise ValueError("Autorização existente possui outro escopo.")
            if inserted is not None:
                self._audit(session, scope, "PREPARED", self._key(scope.authorization_id))
            session.commit()
        return self.status(scope.authorization_id)

    def status(self, identity):
        with self.factory() as session:
            row = session.scalar(select(AppSetting).where(AppSetting.key == self._key(identity)))
            if row is None:
                raise LookupError("Autorização recorrente inexistente.")
            value = dict(row.value)
            if value["active_cycle"]:
                value["state"] = "UNCERTAIN"
            return value

    def approve(self, identity, data, *, offline=False):
        self._laboratory(offline)
        approval = RecurringApproval.model_validate(data)
        return self._approve(identity, approval, "MOCK_ONLY")

    def approve_pilot(self, identity, data, *, offline=False):
        mode = self._pilot_mode(offline)
        return self._approve(identity, PilotConfirmation.model_validate(data), mode)

    def _approve(self, identity, approval, mode):
        with self.factory() as session:
            row = self._row(session, identity)
            value = row.value
            scope = RecurringScope.model_validate(value["scope"])
            if value["mode"] != mode:
                raise ValueError("Modo diverge da autorização; laboratório não pode virar real.")
            if mode != "MOCK_ONLY":
                self._pilot_bounds(scope)
                if value.get("pilot_policy") != self._pilot_policy():
                    raise ValueError("Regras disponíveis mudaram; requer nova revisão.")
            if value["state"] != "PREPARED" or self.clock() >= scope.expires_at:
                raise ValueError("Autorização encerrada ou já aprovada.")
            if approval.scope_sha256 != value["scope_sha256"]:
                raise ValueError("Aprovação não corresponde ao escopo.")
            row.value = {**value, "state": "APPROVED", "approval": approval.model_dump(mode="json")}
            self._audit(session, scope, "APPROVED", self._key(identity))
            session.commit()
        return self.status(identity)

    def set_interval(self, identity, seconds, *, offline=False):
        self._laboratory(offline)
        with self.factory() as session:
            row = self._row(session, identity)
            scope = RecurringScope.model_validate(row.value["scope"])
            if type(seconds) is not int or not scope.min_interval_seconds <= seconds <= 604800:
                raise ValueError("Preferência não pode ampliar a autorização.")
            if row.value["active_cycle"]:
                raise ValueError("Ciclo em andamento; preferência preservada.")
            value = {**row.value, "interval_seconds": seconds}
            if value.get("last_claim_at"):
                anchor = max(
                    datetime.fromisoformat(value["last_claim_at"]),
                    datetime.fromisoformat(value.get("last_completed_at", value["last_claim_at"])),
                )
                value["next_due"] = (anchor + timedelta(seconds=seconds)).isoformat()
            row.value = value
            self._audit(session, scope, "PREFERENCE_CHANGED", self._key(identity))
            session.commit()

    def prepare_scheduler(self, runtime, identity, connector, *, offline=False):
        self._laboratory(offline)
        # Registered paused on an explicitly supplied test runtime only; never in app startup.
        value = self.status(identity)
        runtime.prepare_paused_job(
            self._key(identity),
            lambda: self.execute(
                identity,
                uuid5(UUID(str(identity)), self.clock().isoformat()),
                offline=True,
                connector=connector,
            ),
            value["interval_seconds"],
        )
        runtime.observe_configuration(
            self._key(identity),
            lambda: runtime.update_prepared_job_interval(
                self._key(identity), self.status(identity)["interval_seconds"]
            ),
        )

    def execute(self, identity, cycle_id, *, offline=False, connector=None):
        self._laboratory(offline)
        return self._execute(identity, cycle_id, True, connector, "MOCK_ONLY")

    def execute_pilot(self, identity, cycle_id, data, *, offline=False, connector=None):
        mode = self._pilot_mode(offline)
        confirmation = PilotConfirmation.model_validate(data)
        return self._execute(identity, cycle_id, offline, connector, mode, confirmation)

    def _execute(self, identity, cycle_id, offline, connector, mode, confirmation=None):
        cycle_id = str(UUID(str(cycle_id)))
        cycle_key = self._key(identity) + ":cycle:" + cycle_id
        read_id = uuid5(UUID(str(identity)), cycle_id)
        with self.factory() as session:
            row = self._row(session, identity)
            value = row.value
            scope = RecurringScope.model_validate(value["scope"])
            if value["mode"] != mode:
                raise ValueError("Modo diverge da autorização; laboratório não pode virar real.")
            if confirmation is not None:
                self._pilot_bounds(scope)
                if value.get("pilot_policy") != self._pilot_policy():
                    raise ValueError("Regras disponíveis mudaram; requer nova revisão.")
                if confirmation.scope_sha256 != value["scope_sha256"]:
                    raise ValueError("Confirmação diverge do escopo do piloto.")
                if value.get("approval") is not None:
                    PilotConfirmation.model_validate(value["approval"])
            previous = session.scalar(select(AppSetting).where(AppSetting.key == cycle_key))
            if previous:
                return {
                    **previous.value,
                    "status": "UNCERTAIN"
                    if previous.value["status"] == "CLAIMED"
                    else previous.value["status"],
                }
            if value["state"] != "APPROVED" or not value.get("approval") or value["active_cycle"]:
                raise ValueError("Sem autorização disponível ou ciclo com resultado incerto.")
            if not scope.valid_from <= self.clock() < scope.expires_at:
                raise ValueError("Autorização fora da validade.")
            if value["used"] >= scope.max_requests:
                raise ValueError("Orçamento da autorização esgotado.")
            if self.clock() < datetime.fromisoformat(value["next_due"]):
                raise ValueError("Intervalo ainda não decorrido.")
            read_scope, _, _ = self.single._scope(
                session,
                ObservationRequest.model_validate(scope.request(read_id)),
                "MOCK_ONLY" if offline else "REAL_SINGLE_READ",
            )
            if (
                read_scope != value["read_scope"]
                or self._digest(value, read_scope) != value["scope_sha256"]
            ):
                raise ValueError("Produto, conta ou payload divergente da autorização.")
            self._reserve(session, row, value, cycle_key, cycle_id, scope, read_id)
            session.commit()
        try:
            plan = self.single.prepare(scope.request(read_id), offline=offline)
            with self.factory() as session:
                preferences, _ = DiscoveryOperations().load(session)
            confirmation = dict(
                actor=scope.actor, scope_sha256=plan["scope_sha256"], confirmed_single_read=True
            )
            self.single.approve(
                read_id,
                {
                    **confirmation,
                    "expires_at": min(
                        scope.expires_at,
                        self.clock() + timedelta(seconds=preferences.quota_period_seconds),
                    ).isoformat(),
                },
                offline=offline,
            )
            response = self.single.execute(
                read_id, confirmation, offline=offline, connector=connector
            )
            return self._finish(identity, cycle_key, scope, read_id, response)
        except BaseException:
            # A durable parent claim is never refunded, reconstructed or retried.
            self._finish(identity, cycle_key, scope, read_id, {"status": "UNCERTAIN"})
            raise

    def _reserve(self, session, row, value, key, cycle, scope, read_id):
        claimed = {
            **value,
            "used": value["used"] + 1,
            "active_cycle": cycle,
            "last_claim_at": self.clock().isoformat(),
            "next_due": (self.clock() + timedelta(seconds=value["interval_seconds"])).isoformat(),
        }
        # Compare-and-swap also fences SQLite, where FOR UPDATE is ineffective.
        changed = session.execute(
            update(AppSetting)
            .where(
                AppSetting.key == row.key,
                AppSetting.value["used"].as_integer() == value["used"],
                AppSetting.value["state"].as_string() == "APPROVED",
                AppSetting.value["active_cycle"].as_string().is_(None),
            )
            .values(value=claimed)
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed != 1:
            raise ValueError("Outro ciclo já reservou a autorização.")
        session.add(
            AppSetting(
                key=key,
                value=dict(
                    status="CLAIMED", read_id=str(read_id), reserved_at=self.clock().isoformat()
                ),
                description="Reserva recorrente sem retry.",
            )
        )
        self._audit(session, scope, "CLAIMED", key)

    def _finish(self, identity, key, scope, read_id, response):
        with self.factory() as session:
            parent = self._row(session, identity)
            cycle = self.single._row(session, key)
            if cycle.value["status"] != "CLAIMED":
                return cycle.value
            status = response["status"]
            result = dict(
                status=status,
                mode=parent.value["mode"],
                read_id=str(read_id),
                publication_authorized=False,
                error_code=response.get("error_code"),
                http_status=response.get("result", {}).get("http_status"),
            )
            if status == "SUCCEEDED":
                # Verify result provenance against the child adapter's durable digest.
                quote = response["quote"]
                saved = self.single._row(session, self.single.key(read_id)).value
                if (
                    saved["state"] != "SUCCEEDED"
                    or not saved["consumed"]
                    or saved["result"]["quote_sha256"] != fingerprint(quote)
                ):
                    raise ValueError("Resultado não corresponde à leitura auditada.")
                product = session.scalar(
                    select(Product).where(Product.id == scope.product_id).with_for_update()
                )
                history = record_price(
                    session, product, quote_snapshot(quote, str(read_id)), now=self.clock()
                )
                comparison = PriceComparisonService().reported_quotes(session, history)
                result.update(
                    history_id=history.id,
                    direction=comparison.direction,
                    observed_at=history.observed_at.isoformat(),
                )
            cycle.value = result
            parent.value = {
                **parent.value,
                "active_cycle": None,
                "state": "APPROVED" if status == "SUCCEEDED" else "PAUSED",
            }
            if status == "SUCCEEDED":
                parent.value = {
                    **parent.value,
                    "last_completed_at": self.clock().isoformat(),
                    "next_due": (
                        self.clock() + timedelta(seconds=parent.value["interval_seconds"])
                    ).isoformat(),
                }
            self._audit(session, scope, status, key)
            session.commit()
        return result
