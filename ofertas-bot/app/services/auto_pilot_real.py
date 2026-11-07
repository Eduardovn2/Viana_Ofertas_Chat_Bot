"""Signed real AUTO_PILOT adapter. Construction/preparation never performs HTTP.

The lab adapter is unchanged. Tests of this adapter require explicit MockTransport
and an isolated database; production requires ofertas_bot/public and a signed grant.
"""

import hashlib
import hmac
import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select, text

from app.integrations.contracts import fingerprint
from app.integrations.http import JsonTransport
from app.integrations.shopee import ENDPOINT, ShopeeConnector
from app.integrations.telegram import TelegramPublisher
from app.models import (
    AppSetting,
    MonitoringLease,
    PublicationDestination,
    TelegramPublication,
    TelegramPublicationStatus,
)
from app.services.announcements import MODE as ANNOUNCEMENT_ONLY
from app.services.announcements import RULES as ANNOUNCEMENT_RULES
from app.services.announcements import evidence_hash, render_announcement
from app.services.auto_pilot import PREFIX, TERMINAL, AutoPilotPlan, AutoPilotService
from app.services.leases import acquire_lease, conflict_insert, release_lease, renew_lease
from app.services.shopee_multistore import compare_offers
from app.services.simulation_safety import require_simulation_scope

OWNER_PREFIX = "auto_pilot_real_owner:"
RULES = dict(
    version="REAL_AUTO_PILOT_V1",
    require_documented_identity=True,
    require_current_public_price=True,
    require_available=True,
    require_verified_existing_link=True,
    textual_score_is_not_evidence=True,
    coupon_required=False,
    historical_drop_required=False,
    individual_offer_requires_multistore=False,
    uncertain_never_retries=True,
    temporary_destination_activation=True,
    worker_tick_seconds=30,
    links_created=0,
    uncertain_query_payload_never_retried=True,
    max_commercial_age_seconds=600,
)


class PilotApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    confirmed_pilot_scope: StrictBool
    acknowledge_unconfirmed_quotas_and_permissions: StrictBool

    @field_validator("confirmed_pilot_scope", "acknowledge_unconfirmed_quotas_and_permissions")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("Confirmação explícita obrigatória.")
        return value


class _GuardedTransport(httpx.BaseTransport):
    def __init__(self, owner, delegate):
        self.owner, self.delegate = owner, delegate

    def handle_request(self, request):
        paced_shopee = self.owner._authorize_http(request)
        response = self.delegate.handle_request(request)
        if paced_shopee and response.status_code == 429:
            from app.services.daily_queue import defer_shopee

            defer_shopee(self.owner.factory, self.owner.settings, response, self.owner._now())
        return response

    def close(self):
        self.delegate.close()


class RealAutoPilotService(AutoPilotService):
    mode = "REAL_AUTO_PILOT"
    active_state = "ACTIVE_REAL"

    def __init__(
        self,
        factory,
        settings,
        *,
        isolated_testing=False,
        shopee_transport=None,
        telegram_transport=None,
        clock=None,
    ):
        self.isolated_testing = isolated_testing
        self._context = threading.local()
        if isolated_testing:
            if (
                type(shopee_transport) is not httpx.MockTransport
                or type(telegram_transport) is not httpx.MockTransport
            ):
                raise ValueError("Teste do adaptador real exige dois MockTransport explícitos.")
        elif shopee_transport is not None or telegram_transport is not None:
            raise ValueError("Transportes injetados são exclusivos do laboratório isolado.")
        self._shopee_guard = _GuardedTransport(
            self, shopee_transport or httpx.HTTPTransport(retries=0)
        )
        self._telegram_guard = _GuardedTransport(
            self, telegram_transport or httpx.HTTPTransport(retries=0)
        )
        self._shopee_client = httpx.Client(
            transport=self._shopee_guard, timeout=5, follow_redirects=False, trust_env=False
        )
        self._telegram_client = httpx.Client(
            transport=self._telegram_guard, timeout=10, follow_redirects=False, trust_env=False
        )
        scoped = settings.model_copy(
            update={
                "scheduler_enabled": False,
                "telegram_publisher_enabled": True,
                "telegram_real_sends_authorized": True,
            }
        )
        publisher = TelegramPublisher(scoped, client=self._telegram_client)
        super().__init__(factory, settings, publisher=publisher, clock=clock)
        self.connector = ShopeeConnector(
            JsonTransport(
                client=self._shopee_client,
                max_retries=0,
                cache_seconds=0,
                min_interval=0,
                clock=self.clock,
            ),
            enabled=True,
            app_id=settings.shopee_app_id,
            secret=settings.shopee_secret,
            clock=self.clock,
        )

    def close(self):
        self._shopee_client.close()
        self._telegram_client.close()

    def _safe(self, *, execution=False):
        if (
            self.publisher._client is not self._telegram_client
            or self.connector.transport.client is not self._shopee_client
        ):
            raise ValueError("Cliente HTTP do piloto substituído.")
        with self.factory() as session:
            if self.isolated_testing:
                require_simulation_scope(session, self.settings)
            else:
                if session.get_bind().dialect.name != "postgresql" or session.execute(
                    text("SELECT current_database(),current_schema()")
                ).one() != ("ofertas_bot", "public"):
                    raise ValueError("Despacho real exige ofertas_bot/public.")
                configured = session.scalar(
                    select(AppSetting.value).where(AppSetting.key == "monitoring_config")
                )
                if not configured or configured.get("scheduler_enabled") is not False:
                    raise ValueError("Scheduler persistido deve permanecer desligado.")
        for client, url, guard in (
            (self._shopee_client, ENDPOINT, self._shopee_guard),
            (self._telegram_client, "https://api.telegram.org", self._telegram_guard),
        ):
            if client._transport_for_url(httpx.URL(url)) is not guard or guard.owner is not self:
                raise ValueError("Transporte do piloto substituído.")
            expected = httpx.MockTransport if self.isolated_testing else httpx.HTTPTransport
            if type(guard.delegate) is not expected:
                raise ValueError("Transporte incompatível com o ambiente selado.")
        if (
            self.connector.app_id != self.settings.shopee_app_id
            or self.connector.secret != self.settings.shopee_secret
        ):
            raise ValueError("Credencial do conector divergente.")
        token = self.publisher.settings.telegram_bot_token
        if (
            not self.connector.credentials_configured
            or not token
            or not re.fullmatch(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,256}", token.get_secret_value())
        ):
            raise ValueError("Credenciais locais ausentes ou com formato inválido.")
        if self.connector.transport.max_retries != 0 or self.connector.transport.cache_seconds != 0:
            raise ValueError("Retry/cache proibidos neste piloto.")

    def _sign(self, kind, value):
        secret = self.settings.shopee_secret
        token = self.publisher.settings.telegram_bot_token
        if not secret or not token:
            raise ValueError("Credenciais necessárias ausentes.")
        key = hashlib.sha256(
            (
                "auto-pilot-v1\0" + secret.get_secret_value() + "\0" + token.get_secret_value()
            ).encode()
        ).digest()
        return hmac.new(key, (kind + ":" + fingerprint(value)).encode(), hashlib.sha256).hexdigest()

    def _scope_extra(self, plan):
        if plan.operating_policy is None and not self.isolated_testing:
            raise ValueError("Novos pilotos reais exigem política operacional por janela.")
        if not plan.daily_grant_id and (
            plan.max_queries > 12 or plan.max_queries + plan.max_messages > 32
        ):
            raise ValueError("Piloto real limitado a 12 consultas e 32 HTTP.")
        with self.factory() as session:
            destination = session.get(PublicationDestination, plan.destination_id)
            if destination.external_id not in self.settings.telegram_allowed_chat_ids:
                raise ValueError("Destino fora da allowlist.")
            daily = {}
            if plan.daily_grant_id:
                from app.services.daily_operation import require_child

                digest, _ = require_child(session, self.settings, plan, self._now(), running=True)
                daily = {"daily_scope_sha256": digest}
        return dict(
            **daily,
            environment="ISOLATED_MOCK" if self.isolated_testing else "PRODUCTION",
            rules=ANNOUNCEMENT_RULES if plan.commercial_mode == ANNOUNCEMENT_ONLY else RULES,
            rules_sha256=fingerprint(
                ANNOUNCEMENT_RULES if plan.commercial_mode == ANNOUNCEMENT_ONLY else RULES
            ),
            worker_tick_seconds=30,
            query_payload_sha256={
                term: hashlib.sha256(
                    json.dumps(
                        {
                            "query": ShopeeConnector.discovery_query(
                                term, page=1, limit=3, profile="multistore"
                            )
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                for term in plan.terms
            },
            **(
                {
                    "terminal_conditions": [
                        "DEADLINE",
                        "ADMIN_STOP",
                        *(
                            []
                            if plan.operating_policy.continue_discovery_after_message_budget
                            else ["MESSAGE_BUDGET"]
                        ),
                        "SHOPEE_PERMISSION_DENIED",
                        "QUERY_UNCERTAINTY_NO_RETRY",
                        *(
                            ["NO_WORK_AFTER_QUERY_BUDGET"]
                            if plan.operating_policy.on_no_work == "STOP_WHEN_DRAINED"
                            else []
                        ),
                    ]
                }
                if plan.operating_policy
                else {}
            ),
        )

    def _seal_new_record(self, value):
        value["scope_signature"] = self._sign("scope", value["scope"])
        value["approval"] = None

    def _load(self, session, pilot_id, *, lock=False):
        row, value = super()._load(session, pilot_id, lock=lock)
        scope = value["scope"]
        if (
            scope["chat_id"] not in self.settings.telegram_allowed_chat_ids
            or scope["chat_id"] not in self.publisher.settings.telegram_allowed_chat_ids
        ):
            raise ValueError("Destino deixou de estar na allowlist.")
        if scope.get("environment") != ("ISOLATED_MOCK" if self.isolated_testing else "PRODUCTION"):
            raise ValueError("Autorização pertence a outro ambiente.")
        expected_rules = (
            ANNOUNCEMENT_RULES
            if scope["plan"].get("commercial_mode") == ANNOUNCEMENT_ONLY
            else RULES
        )
        if (
            scope.get("rules_sha256") != fingerprint(expected_rules)
            or scope.get("rules") != expected_rules
        ):
            raise ValueError("Regras comerciais divergentes.")
        if not hmac.compare_digest(value.get("scope_signature", ""), self._sign("scope", scope)):
            raise ValueError("Assinatura do escopo inválida.")
        if scope["plan"].get("duration_seconds") and value.get("started_at"):
            activation = dict(
                scope_sha256=value["scope_sha256"],
                started_at=value["started_at"],
                runtime_expires_at=value.get("runtime_expires_at"),
            )
            if not hmac.compare_digest(
                value.get("activation_signature", ""), self._sign("activation", activation)
            ):
                raise ValueError("Prazo de ativação inválido.")
            if datetime.fromisoformat(value["runtime_expires_at"]) != self._deadline(value):
                raise ValueError("Prazo não corresponde à duração autorizada.")
        if value["state"] in {"APPROVED", "PAUSED", self.active_state}:
            approval = value.get("approval")
            if (
                not approval
                or approval.get("scope_sha256") != value["scope_sha256"]
                or not hmac.compare_digest(
                    value.get("approval_signature", ""), self._sign("approval", approval)
                )
            ):
                raise ValueError("Autorização expressa do piloto ausente ou inválida.")
        return row, value

    def _deadline(self, value):
        plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
        if plan.duration_seconds and value.get("started_at"):
            return min(
                plan.expires_at,
                datetime.fromisoformat(value["started_at"])
                + timedelta(seconds=plan.duration_seconds),
            )
        return plan.expires_at

    def approve(self, pilot_id, data):
        self._safe()
        confirmation = PilotApproval.model_validate(data)
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            if value["state"] != "PREPARED" or confirmation.scope_sha256 != value["scope_sha256"]:
                raise ValueError("Aprovação exige plano PREPARED e hash exato.")
            plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
            if self._now() >= plan.expires_at:
                raise ValueError("Plano expirado.")
            self._check_daily(session, value)
            approval = dict(**confirmation.model_dump(), approved_at=self._now().isoformat())
            value.update(
                state="APPROVED",
                approval=approval,
                approval_signature=self._sign("approval", approval),
            )
            self._save(row, value)
            session.commit()
        return self.report(pilot_id)

    def start_offline(self, *args, **kwargs):
        raise ValueError("Adaptador real exige aprovação única e start_authorized.")

    def start_authorized(self, pilot_id, scope_sha256):
        self._safe(execution=True)
        with self.factory() as session:
            _, preview = self._load(session, pilot_id)
            destination_id = preview["scope"]["plan"]["destination_id"]
            destination = session.scalar(
                select(PublicationDestination)
                .where(PublicationDestination.id == destination_id)
                .with_for_update()
            )
            row, value = self._load(session, pilot_id, lock=True)
            plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
            if value["scope_sha256"] != scope_sha256 or value["state"] not in {
                "APPROVED",
                "PAUSED",
                self.active_state,
            }:
                raise ValueError("Início exige autorização íntegra e não encerrada.")
            if not plan.not_before <= self._now() < plan.expires_at:
                raise ValueError("Fora da janela autorizada.")
            self._check_daily(session, value)
            if value["state"] == self.active_state:
                session.commit()
                return self.report(pilot_id)
            now = self._now()
            if plan.duration_seconds:
                if value.get("started_at"):
                    if now >= self._deadline(value):
                        raise ValueError("Duração do piloto já encerrada.")
                else:
                    deadline = now + timedelta(seconds=plan.duration_seconds)
                    if deadline > plan.expires_at:
                        raise ValueError("Janela restante insuficiente para a duração autorizada.")
                    value.update(
                        started_at=now.isoformat(), runtime_expires_at=deadline.isoformat()
                    )
                    value["activation_signature"] = self._sign(
                        "activation",
                        dict(
                            scope_sha256=value["scope_sha256"],
                            started_at=value["started_at"],
                            runtime_expires_at=value["runtime_expires_at"],
                        ),
                    )
            key = OWNER_PREFIX + str(destination_id)
            session.execute(
                conflict_insert(session, AppSetting)
                .values(key=key, value={"owner": None})
                .on_conflict_do_nothing(index_elements=[AppSetting.key])
            )
            owner = session.scalar(
                select(AppSetting).where(AppSetting.key == key).with_for_update()
            )
            if owner.value.get("owner"):
                raise ValueError(
                    "Destino já pertence a outro piloto; encerrar/reconciliar primeiro."
                )
            owner.value = dict(
                owner=str(pilot_id),
                destination_id=destination_id,
                chat_id=destination.external_id,
                original_enabled=destination.enabled,
            )
            destination.enabled = True  # Signed scope explicitly covers temporary activation.
            value.update(state=self.active_state)
            if not plan.duration_seconds and (
                self._operating_policy(value) is None or not value.get("started_at")
            ):
                value["started_at"] = now.isoformat()
            value["reason"] = None
            self._save(row, value)
            session.commit()
        return self.report(pilot_id)

    def _require_active(self, value):
        plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
        if value[
            "state"
        ] != self.active_state or not plan.not_before <= self._now() < self._deadline(value):
            raise ValueError("Piloto não ativo ou fora da janela.")
        if (
            len(value["queries"]) > plan.max_queries
            or len(value["reservations"]) > plan.max_messages
        ):
            raise ValueError("Orçamento de reservas excedido.")

    def _check_daily(self, session, value):
        scope = value["scope"]
        if scope["plan"].get("daily_grant_id"):
            from app.services.daily_operation import require_child

            require_child(
                session,
                self.settings,
                scope["plan"],
                self._now(),
                scope_hash=scope["daily_scope_sha256"],
                running=True,
            )

    def _enrich_daily(self, pilot_id):
        from app.services.daily_supply import enrich

        enrich(self, pilot_id)

    def _reserve_query(self, pilot_id):
        self._safe(execution=True)
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
            if not self._active(value) or not self._query_due(value):
                self._save(row, value)
                session.commit()
                return None
            failed = {
                q["term"]
                for q in value["queries"]
                if q["state"] in {"RESERVED", "FAILED_OR_UNCERTAIN"}
                and not q.get("refresh_identity")
            }
            self._check_daily(session, value)
            candidates = (
                plan.terms[len(value["queries"]) % len(plan.terms) :]
                + plan.terms[: len(value["queries"]) % len(plan.terms)]
            )
            from app.services.daily_queue import refresh_candidate, shopee_ready

            limit = plan.operating_policy.shopee_posts_per_minute if plan.operating_policy else None
            if not shopee_ready(session, self.settings, limit, self._now()):
                return None
            refresh = refresh_candidate(session, value, self._now())
            term = (
                refresh["term"]
                if refresh
                else next((term for term in candidates if term not in failed), None)
            )
            if term is None:
                value.update(state="COMPLETED", reason="QUERY_UNCERTAINTY_NO_RETRY")
                self._save(row, value)
                session.commit()
                return None
            reservation = dict(
                id=uuid4().hex, state="RESERVED", at=self._now().isoformat(), term=term
            )
            if refresh:
                reservation["refresh_identity"] = refresh["identity"]
            value["queries"].append(reservation)
            self._save(row, value)
            session.commit()
            return reservation

    @contextmanager
    def _execution(self, pilot_id):
        """Serialize API and workers; the lease is fenced again before HTTP."""
        name = "auto-pilot-execution:" + str(pilot_id)
        existing = getattr(self._context, "execution", None)
        if existing is not None:
            if existing[0] != name:
                raise ValueError("Execução aninhada de outro piloto bloqueada.")
            yield True
            return
        owner = acquire_lease(self.factory, name, 180, self.clock)
        if owner is None:
            yield False
            return
        self._context.execution = (name, owner)
        try:
            yield True
        finally:
            self._context.execution = None
            release_lease(self.factory, name, owner)

    def _recover_interrupted(self, pilot_id):
        """Exclusive executor only; never retry or invent a delivery result."""
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            for query in value["queries"]:
                if query["state"] == "RESERVED":
                    query.update(state="FAILED_OR_UNCERTAIN", error="PROCESS_INTERRUPTED")
            for reservation in value["reservations"]:
                if reservation["state"] != "CLAIMED":
                    continue
                reservation.update(state="UNCERTAIN", reason="PROCESS_INTERRUPTED")
                from app.services.daily_queue import record_dispatch

                record_dispatch(
                    session, value, reservation["identity"], "UNCERTAIN", "PROCESS_INTERRUPTED"
                )
                queued = None
                for item in value["queue"]:
                    if item["id"] == reservation["queue_id"]:
                        item.update(state="UNCERTAIN", reason="PROCESS_INTERRUPTED")
                        queued = item
                publication_id = reservation.get("publication_id")
                publication = (
                    session.get(TelegramPublication, publication_id) if publication_id else None
                )
                if publication is None and queued is not None:
                    publication = session.scalar(
                        select(TelegramPublication)
                        .where(
                            TelegramPublication.offer_id == queued["offer_id"],
                            TelegramPublication.destination_id == queued["destination_id"],
                            TelegramPublication.status == TelegramPublicationStatus.SENDING,
                        )
                        .with_for_update()
                    )
                    if publication:
                        reservation["publication_id"] = publication.id
                if (
                    publication is not None
                    and publication.status == TelegramPublicationStatus.SENDING
                ):
                    publication.status = TelegramPublicationStatus.UNCERTAIN
                    publication.last_error_code = "PROCESS_INTERRUPTED"
            self._close_uncertain_queries(value)
            self._save(row, value)
            session.commit()

    def _close_uncertain_queries(self, value):
        failed = {
            q["term"]
            for q in value["queries"]
            if q["state"] == "FAILED_OR_UNCERTAIN" and not q.get("refresh_identity")
        }
        if value["state"] == self.active_state and set(value["scope"]["plan"]["terms"]) <= failed:
            value.update(state="COMPLETED", reason="QUERY_UNCERTAINTY_NO_RETRY")

    def tick(self, pilot_id):
        self._safe(execution=True)
        with self._execution(pilot_id) as acquired:
            if not acquired:
                return self.report(pilot_id)
            self._recover_interrupted(pilot_id)
            current = self.report(pilot_id)
            if current["state"] != self.active_state or not self._claim_tick(pilot_id):
                return current
            return super().tick(pilot_id)

    def reconcile_interrupted(self, pilot_id, scope_sha256):
        self._safe()
        with self._execution(pilot_id) as acquired:
            if acquired:
                with self.factory() as session:
                    _, value = self._load(session, pilot_id)
                    if value["scope_sha256"] != scope_sha256:
                        raise ValueError("Reconciliação exige hash exato.")
                self._recover_interrupted(pilot_id)
        return self.report(pilot_id)

    def _claim_tick(self, pilot_id):
        """The 30s cadence is global, not multiplied by replica count/restarts."""
        key = "auto_pilot_tick:" + str(pilot_id)
        with self.factory() as session:
            session.execute(
                conflict_insert(session, AppSetting)
                .values(key=key, value={})
                .on_conflict_do_nothing(index_elements=[AppSetting.key])
            )
            row = session.scalar(select(AppSetting).where(AppSetting.key == key).with_for_update())
            if row.value and self._now() < datetime.fromisoformat(
                row.value["started_at"]
            ) + timedelta(seconds=30):
                return False
            row.value = dict(started_at=self._now().isoformat())
            session.commit()
            return True

    def dispatch_batch(self, pilot_id):
        self._safe(execution=True)
        with self._execution(pilot_id) as acquired:
            return super().dispatch_batch(pilot_id) if acquired else self.report(pilot_id)

    def discover_once(self, pilot_id):
        self._safe(execution=True)
        with self._execution(pilot_id) as acquired:
            if acquired:
                self._discover_once_exclusive(pilot_id)

    def _discover_once_exclusive(self, pilot_id):
        super().discover_once(pilot_id)
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            if (
                value["state"] == self.active_state
                and value["queries"]
                and value["queries"][-1].get("error")
                in {"AuthorizationDenied", "ConnectorDisabled"}
            ):
                value.update(state="COMPLETED", reason="SHOPEE_PERMISSION_DENIED")
                self._save(row, value)
                session.commit()
            elif value["state"] == self.active_state:
                self._close_uncertain_queries(value)
                self._save(row, value)
                session.commit()

    def _after_outcome(self, value, state, reason):
        if reason in {"TELEGRAM_UNAUTHORIZED", "TELEGRAM_FORBIDDEN"}:
            value.update(state="COMPLETED", reason="TELEGRAM_PERMISSION_DENIED")

    def _reserve_message(self, pilot_id, queue_id):
        self._safe(execution=True)
        return super()._reserve_message(pilot_id, queue_id)

    def _eligible(self, session, offer_id, quote, *, commercial_mode=None):
        from app.services.comparison import aware_utc

        bundle = super()._eligible(session, offer_id, quote, commercial_mode=commercial_mode)
        if commercial_mode == ANNOUNCEMENT_ONLY:
            return bundle
        verified = aware_utc(bundle.details.verified_at)
        if self._now() - verified > timedelta(seconds=600) or aware_utc(
            bundle.offer.expires_at
        ) > verified + timedelta(seconds=600):
            raise ValueError("Evidência comercial excede a validade de 600 segundos do piloto.")
        return bundle

    def _discover_analysis(self, pilot_id, reservation):
        self._context.permit = (str(pilot_id), "query", reservation["id"])
        try:
            if identity := reservation.get("refresh_identity"):
                from app.integrations.contracts import DiscoveryPage
                from app.integrations.errors import IntegrationError
                from app.services.daily_queue import refresh_ref

                try:
                    snapshot = self.connector.fetch_product(refresh_ref(identity))
                except IntegrationError as exc:
                    if exc.code != "PRODUCT_NOT_FOUND":
                        raise
                    return compare_offers(DiscoveryPage([], 1, False), reservation["term"])
                return compare_offers(DiscoveryPage([snapshot], 1, False), reservation["term"])
            page = self.connector.discover_offers(
                reservation["term"], page=1, limit=3, profile="multistore"
            )
            return compare_offers(page, reservation["term"])
        finally:
            self._context.permit = None

    def _before_http(self, pilot_id, reservation_id):
        super()._before_http(pilot_id, reservation_id)
        self._context.permit = (str(pilot_id), "message", reservation_id)

    def _bind_transport_fence(self, lease, owner):
        self._context.fence = (lease, owner)

    def _authorize_http(self, request):
        self._safe(execution=True)
        permit = getattr(self._context, "permit", None)
        self._context.permit = None
        if permit is None or request.method != "POST":
            raise ValueError("HTTP sem reserva específica.")
        pilot_id, kind, reservation_id = permit
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            self._require_active(value)
            self._check_daily(session, value)
            plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
            owner = session.scalar(
                select(AppSetting.value).where(
                    AppSetting.key == OWNER_PREFIX + str(plan.destination_id)
                )
            )
            destination = session.get(PublicationDestination, plan.destination_id)
            if not owner or owner.get("owner") != pilot_id or not destination.enabled:
                raise ValueError("Destino pausado ou posse do piloto perdida.")
            entries = value["queries"] if kind == "query" else value["reservations"]
            reserved = next(r for r in entries if r["id"] == reservation_id)
            if reserved.get("transport_consumed"):
                raise ValueError("Permissão HTTP já consumida; repetição proibida.")
            used = sum(
                bool(r.get("transport_consumed")) for r in value["queries"] + value["reservations"]
            )
            if used >= value["scope"]["max_external_http"]:
                raise ValueError("Orçamento total HTTP consumido.")
            if kind == "query":
                if reserved["term"] not in plan.terms:
                    raise ValueError("Palavra-chave fora do escopo aprovado.")
                from app.services.daily_queue import continuous, queue_prefix, refresh_ref, setting

                identity = reserved.get("refresh_identity")
                if identity:
                    if not continuous(value):
                        raise ValueError("Revalidação não autorizada.")
                    candidate = setting(session, queue_prefix(value) + identity)
                    if (
                        not candidate
                        or candidate.value["state"] != "PENDING"
                        or candidate.value["term"] != reserved["term"]
                    ):
                        raise ValueError("Candidato fora da fila autorizada.")
                    graphql = ShopeeConnector.product_query(refresh_ref(identity))
                else:
                    graphql = ShopeeConnector.discovery_query(
                        reserved["term"], page=1, limit=3, profile="multistore"
                    )
                expected = json.dumps(
                    {"query": graphql}, ensure_ascii=False, separators=(",", ":")
                ).encode()
                if (
                    str(request.url) != ENDPOINT
                    or request.content != expected
                    or reserved["state"] != "RESERVED"
                ):
                    raise ValueError("Payload/URL Shopee fora do escopo.")
                if (
                    not identity
                    and hashlib.sha256(request.content).hexdigest()
                    != value["scope"]["query_payload_sha256"][reserved["term"]]
                ):
                    raise ValueError("Hash exato do payload diverge do plano.")
                authorization = re.fullmatch(
                    r"SHA256 Credential=([A-Za-z0-9._-]{1,128}), Timestamp=([0-9]+), Signature=([a-f0-9]{64})",
                    request.headers.get("Authorization", ""),
                )
                if authorization is None:
                    raise ValueError("Autenticação HTTP Shopee divergente.")
                app_id, timestamp, signature = authorization.groups()
                expected_signature = hashlib.sha256(
                    self.settings.shopee_app_id.get_secret_value().encode()
                    + timestamp.encode()
                    + request.content
                    + self.settings.shopee_secret.get_secret_value().encode()
                ).hexdigest()
                if (
                    app_id != self.settings.shopee_app_id.get_secret_value()
                    or abs(int(timestamp) - self._now().timestamp()) > 30
                    or not hmac.compare_digest(signature, expected_signature)
                ):
                    raise ValueError("Credencial/assinatura HTTP fora do escopo.")
            else:
                fence = getattr(self._context, "fence", None)
                lease = session.get(MonitoringLease, fence[0]) if fence else None
                if lease is None or lease.owner != fence[1] or lease.expires_at <= self._now():
                    raise ValueError("Worker perdeu a reserva antes do transporte.")
                if reserved["state"] != "CLAIMED" or not reserved.get("http_started"):
                    raise ValueError("Publicação não reservada.")
                item = next(q for q in value["queue"] if q["id"] == reserved["queue_id"])
                entry = session.scalar(
                    select(TelegramPublication).where(
                        TelegramPublication.product_id == item["product_id"],
                        TelegramPublication.destination_id == plan.destination_id,
                        TelegramPublication.status == "SENDING",
                    )
                )
                if (
                    entry is None
                    or entry.offer_id != item["offer_id"]
                    or entry.approved_by != "AUTO_PILOT_RULES:" + pilot_id
                ):
                    raise ValueError("Outbox não corresponde ao piloto.")
                if item.get("commercial_mode") != plan.commercial_mode and (
                    item.get("commercial_mode") == ANNOUNCEMENT_ONLY
                    or plan.commercial_mode == ANNOUNCEMENT_ONLY
                ):
                    raise ValueError("Commercial mode outside scope")
                if (
                    plan.announcement_identities is not None
                    and item["identity"] not in plan.announcement_identities
                ):
                    raise ValueError("Listing outside authorized scope")
                bundle = self._eligible(
                    session, item["offer_id"], item["quote"], commercial_mode=plan.commercial_mode
                )
                if (
                    evidence_hash(bundle) != item["identity_evidence_sha256"]
                    or bundle.product.name != entry.preview_payload["product"]["name"]
                    or bundle.product.variant_id != entry.preview_payload["product"]["variant_id"]
                    or bundle.product.affiliate_link.url
                    != entry.preview_payload["affiliate_link"]["url"]
                    or str(bundle.offer.coupon_id)
                    != str(entry.preview_payload["offer"]["coupon_id"])
                ):
                    raise ValueError("Evidência ou conteúdo mudou antes do HTTP.")
                message = entry.preview_payload["message"]
                if (
                    plan.commercial_mode == ANNOUNCEMENT_ONLY
                    and asdict(
                        render_announcement(
                            bundle, include_image=entry.preview_payload.get("include_image", False)
                        )
                    )
                    != message
                ):
                    raise ValueError("Announcement render changed before transport")
                if (
                    plan.expected_message_sha256 is not None
                    and fingerprint({"chat_id": value["scope"]["chat_id"], "message": message})
                    != plan.expected_message_sha256
                ):
                    raise ValueError("A mensagem difere do texto específico autorizado.")
                photo = message.get("photo_url")
                if photo and (
                    plan.commercial_mode != ANNOUNCEMENT_ONLY
                    or plan.announcement_template != "PRO_V2"
                ):
                    raise ValueError("Photo outside authorized template")
                expected = dict(chat_id=value["scope"]["chat_id"], parse_mode="HTML")
                if photo:
                    expected.update(photo=photo, caption=message["text"])
                else:
                    expected.update(
                        text=message["text"], link_preview_options={"is_disabled": True}
                    )
                method = "sendPhoto" if photo else "sendMessage"
                token = self.publisher.settings.telegram_bot_token.get_secret_value()
                if (
                    str(request.url) != f"https://api.telegram.org/bot{token}/{method}"
                    or json.loads(request.content) != expected
                ):
                    raise ValueError("Conteúdo/destino Telegram fora do escopo.")
                reserved["publication_id"] = entry.id
            executor = getattr(self._context, "execution", None)
            if executor is None or not renew_lease(session, *executor, 180, self.clock):
                raise ValueError("Executor perdeu a reserva exclusiva antes do HTTP.")
            self._require_active(value)
            from app.services.daily_queue import shopee_ready, telegram_ready

            policy = plan.operating_policy
            paced_shopee = bool(kind == "query" and policy and policy.shopee_posts_per_minute)
            if paced_shopee and not shopee_ready(
                session, self.settings, policy.shopee_posts_per_minute, self._now(), reserve=True
            ):
                raise ValueError("Ritmo Shopee temporariamente bloqueado.")
            if (
                kind == "message"
                and policy
                and policy.telegram_per_minute
                and not telegram_ready(
                    session,
                    value["scope"]["chat_id"],
                    policy.telegram_per_minute,
                    self._now(),
                    reserve=True,
                )
            ):
                raise ValueError("Ritmo Telegram temporariamente bloqueado.")
            reserved.update(
                transport_consumed=True,
                transport_started_at=self._now().isoformat(),
                payload_sha256=hashlib.sha256(request.content).hexdigest(),
            )
            self._save(row, value)
            session.commit()  # Durable consumed permit BEFORE delegate.handle_request.
            return paced_shopee

    def _restore_destination(self, pilot_id):
        with self.factory() as session:
            owners = session.scalars(
                select(AppSetting).where(AppSetting.key.startswith(OWNER_PREFIX))
            ).all()
            for owner in owners:
                if owner.value.get("owner") != str(pilot_id):
                    continue
                destination = session.scalar(
                    select(PublicationDestination)
                    .where(PublicationDestination.id == owner.value["destination_id"])
                    .with_for_update()
                )
                session.refresh(owner, with_for_update=True)
                if owner.value.get("owner") != str(pilot_id):
                    continue
                if destination.external_id == owner.value["chat_id"]:
                    destination.enabled = owner.value["original_enabled"]
                owner.value = {**owner.value, "owner": None, "released_at": self._now().isoformat()}
            session.commit()

    def stop(self, pilot_id):
        return self._suspend(pilot_id, "STOPPED", "ADMIN_STOP")

    def pause(self, pilot_id):
        return self._suspend(pilot_id, "PAUSED", "ADMIN_PAUSE")

    def _suspend(self, pilot_id, state, reason):
        # Stopping never requires a live credential/grant and never emits HTTP.
        with self.factory() as session:
            if self.isolated_testing:
                require_simulation_scope(session, self.settings)
            elif session.get_bind().dialect.name != "postgresql" or session.execute(
                text("SELECT current_database(),current_schema()")
            ).one() != ("ofertas_bot", "public"):
                raise ValueError("Parada exige o banco correto.")
            row = session.scalar(
                select(AppSetting)
                .where(AppSetting.key == PREFIX + str(UUID(str(pilot_id))))
                .with_for_update()
            )
            if row is None or row.value["scope"]["mode"] != self.mode:
                raise LookupError("Piloto real inexistente.")
            value = dict(row.value)
            if state == "PAUSED" and value["state"] not in {
                self.active_state,
                "PAUSED",
                "APPROVED",
            }:
                raise ValueError("Pausa exige piloto aprovado ou ativo.")
            if value["state"] not in TERMINAL:
                value.update(state=state, reason=reason)
            self._save(row, value)
            session.commit()
        self._restore_destination(pilot_id)
        return dict(authorization_id=str(pilot_id), state=value["state"], reason=value["reason"])

    def report(self, pilot_id):
        report = super().report(pilot_id)
        if report["state"] in TERMINAL:
            self._restore_destination(pilot_id)
        report.pop("scope_signature", None)
        report.pop("approval_signature", None)
        report.pop("activation_signature", None)
        report["authority"] = "SIGNED_PILOT_SCOPE_AUTOMATIC_RULES"
        plan = AutoPilotPlan.model_validate(report["scope"]["plan"])
        report["real_dispatch_implemented"] = True
        report["effective_expires_at"] = self._deadline(report).isoformat()
        report["latest_activation_at"] = (
            plan.expires_at - timedelta(seconds=plan.duration_seconds or 0)
        ).isoformat()
        report["window_expired"] = self._now() >= self._deadline(report)
        report["real_activation_allowed"] = (
            not self.isolated_testing
            and report["state"] == "APPROVED"
            and plan.not_before <= self._now() < plan.expires_at
            and (
                not plan.duration_seconds
                or self._now() + timedelta(seconds=plan.duration_seconds) <= plan.expires_at
            )
        )
        report["external_http_calls"] = (
            0
            if self.isolated_testing
            else sum(
                bool(r.get("transport_consumed"))
                for r in report["queries"] + report["reservations"]
            )
        )
        report["counts"]["unfinalized_reservations"] = sum(
            r["state"] == "CLAIMED" for r in report["reservations"]
        )
        report["counts"]["transport_permits_consumed"] = sum(
            bool(r.get("transport_consumed")) for r in report["queries"] + report["reservations"]
        )
        return report

    def run_worker(self, pilot_id, scope_sha256, *, wait=time.sleep):
        """Explicit foreground worker only; no global APScheduler or startup hook."""
        try:
            self.start_authorized(pilot_id, scope_sha256)
        except BaseException:
            self._restore_destination(pilot_id)
            raise
        try:
            while True:
                tick_started = time.monotonic()
                report = self.tick(pilot_id)
                if report["state"] in TERMINAL:
                    return report
                remaining = (
                    datetime.fromisoformat(report["effective_expires_at"]) - self._now()
                ).total_seconds()
                if remaining > 0:
                    delay = max(0, 30 - (time.monotonic() - tick_started))
                    if delay > 0:
                        wait(min(delay, remaining))
        finally:
            self.stop(pilot_id)
