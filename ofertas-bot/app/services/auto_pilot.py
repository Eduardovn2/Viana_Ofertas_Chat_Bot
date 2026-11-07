"""Bounded AUTO_PILOT controller over discovery and the existing Telegram outbox.

This release executes only with MockTransport in isolated test databases. It never
manufactures commercial attestations, links, products or price history.
"""

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError, http_diagnostics
from app.integrations.telegram import TelegramPublisher
from app.models import (
    AppSetting,
    DestinationChannel,
    Offer,
    Platform,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
    TelegramPublication,
)
from app.services.announcements import MODE as ANNOUNCEMENT_ONLY
from app.services.announcements import (
    create_announcement,
    evidence_hash,
    read_announcement,
    render_announcement,
)
from app.services.categories import normalize_text
from app.services.commercial_collection import CommercialCollectionPolicy, collect_documents
from app.services.comparison import aware_utc
from app.services.discovery_relevance import SearchTerm, relevance
from app.services.leases import acquire_lease, conflict_insert, release_lease, renew_lease
from app.services.manual_offers import read_manual_offer
from app.services.product_documentation import DocumentedIdentity
from app.services.ranking_signal_import import load_ranking_signals
from app.services.shopee_identity import expected_identity, url_identity
from app.services.shopee_opportunities import pilot_opportunities
from app.services.simulation_safety import require_simulation_scope
from app.services.telegram_publication import TelegramPublicationService
from app.services.telegram_publication import _error as publication_error

PREFIX = "auto_pilot:"
TERMINAL = {"COMPLETED", "STOPPED"}


class OperatingPolicy(BaseModel):
    """Signed policy for new grants. Absent on immutable legacy grants."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["WINDOWED_V1"] = "WINDOWED_V1"
    query_interval_seconds: int | None = Field(default=None, strict=True, ge=30, le=7200)
    continue_discovery_after_message_budget: Literal[True] | None = None
    telegram_per_minute: int | None = Field(default=None, ge=1, le=20, strict=True)
    administrative_messages_per_hour: int | None = Field(default=None, ge=1, strict=True)
    shopee_posts_per_minute: int | None = Field(default=None, ge=1, strict=True)
    on_no_work: Literal["WAIT_UNTIL_END", "STOP_WHEN_DRAINED"] = "WAIT_UNTIL_END"


class AutoPilotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    authorization_id: UUID = Field(default_factory=uuid4)
    profile: str = Field(default="AUTO_PILOT", pattern=r"^AUTO_PILOT$")
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    destination_id: int = Field(strict=True, gt=0)
    not_before: datetime
    expires_at: datetime
    terms: list[str] = Field(min_length=1, max_length=20)
    max_messages: int = Field(default=20, strict=True, ge=1)
    max_queries: int = Field(default=12, strict=True, ge=0)
    expected_message_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    page: int = Field(default=1, strict=True, ge=1, le=1)
    limit: int = Field(default=3, strict=True, ge=1, le=3)
    window_seconds: int = Field(default=600, strict=True, ge=600, le=600)
    messages_per_window: int | None = Field(default=5, strict=True, ge=1)
    cooldown_seconds: int = Field(default=10800, strict=True, ge=10800, le=86400)
    duration_seconds: int | None = Field(default=None, strict=True, ge=1, le=7200)
    operating_policy: OperatingPolicy | None = Field(default_factory=OperatingPolicy)
    commercial_collection: CommercialCollectionPolicy | None = None
    commercial_mode: Literal["VERIFIED_OFFER", "ANNOUNCEMENT_ONLY"] | None = None
    announcement_template: Literal["LEGACY_V1", "PRO_V2"] | None = None
    announcement_selection: Literal["RANKING_100_V1"] | None = None
    daily_grant_id: UUID | None = None
    announcement_identities: list[str] | None = Field(default=None, min_length=1, max_length=20)

    @field_validator("announcement_identities")
    @classmethod
    def valid_announcement_ids(cls, values):
        import re

        if values is not None and (
            len(values) != len(set(values))
            or any(not re.fullmatch(r"[1-9][0-9]{0,18}:[1-9][0-9]{0,18}", v) for v in values)
        ):
            raise ValueError("Invalid announcement listing allowlist")
        return values

    @field_validator("not_before", "expires_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)

    @field_validator("terms")
    @classmethod
    def distinct_terms(cls, values):
        if any(not v.strip() or len(v) > 100 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("Termos inválidos.")
        normalized = [normalize_text(v) for v in values]
        if not all(normalized) or len(set(normalized)) != len(values):
            raise ValueError("Termos vazios ou repetidos.")
        return [v.strip() for v in values]

    @model_validator(mode="after")
    def bounded(self):
        paced = bool(self.operating_policy and self.operating_policy.telegram_per_minute)
        if paced and not self.daily_grant_id:
            raise ValueError("Ritmo de provedor exige autorização diária.")
        if not paced and (self.messages_per_window is None or self.messages_per_window > 5):
            raise ValueError("Cadência inválida para escopo legado.")
        if not self.daily_grant_id and (
            self.max_messages > 20
            or self.max_queries > 24
            or (
                self.operating_policy
                and self.operating_policy.continue_discovery_after_message_budget
            )
        ):
            raise ValueError("Orçamento ampliado exige autorização diária.")
        if self.max_queries == 0 and (
            self.operating_policy is None
            or self.expected_message_sha256 is None
            or self.max_messages != 1
        ):
            raise ValueError(
                "Publicação sem consultas exige política por janela e vínculo a uma única mensagem."
            )
        maximum = timedelta(hours=24 if self.daily_grant_id else 2)
        if not timedelta(0) < self.expires_at - self.not_before <= maximum:
            raise ValueError("AUTO_PILOT limitado a duas horas.")
        if (
            self.duration_seconds
            and self.duration_seconds > (self.expires_at - self.not_before).total_seconds()
        ):
            raise ValueError("Duração excede a janela autorizada.")
        return self


class _PilotPublication(TelegramPublicationService):
    def __init__(self, *args, policy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.policy = policy or {}

    def _rate_limit(self, session, destination, now):
        self.check_rate(session, destination, now, self.policy)

    def _finish(self, *args, **kwargs):
        if (
            self.policy.get("telegram_per_minute")
            and kwargs.get("code") == "TELEGRAM_RATE_LIMITED"
            and not kwargs.get("retry_after")
        ):
            kwargs["retry_after"] = 60
        return super()._finish(*args, **kwargs)

    @staticmethod
    def check_rate(session, destination, now, policy=None):
        # Sliding window replaces the fixed inter-message gap only in AUTO_PILOT.
        # All destination attempts count, including failed/uncertain publications.
        deferred = session.scalar(
            select(func.max(TelegramPublication.retry_at)).where(
                TelegramPublication.destination_id == destination.id,
                TelegramPublication.retry_at > now,
            )
        )
        if deferred:
            raise publication_error("OUTBOX_RATE_LIMITED", retry_at=deferred)
        provider = (policy or {}).get("telegram_per_minute")
        if provider:
            from app.services.daily_queue import telegram_ready

            if not telegram_ready(session, destination.external_id, provider, now):
                raise publication_error("OUTBOX_RATE_LIMITED")
        windows = [(1, 1), (60, provider)] if provider else [(600, 5)]
        hourly = (
            (policy or {}).get("administrative_messages_per_hour")
            if provider
            else destination.max_messages_per_hour
        )
        if hourly is not None:
            windows.append((3600, hourly))
        for seconds, limit in windows:
            count = session.scalar(
                select(func.count())
                .select_from(PublicationLog)
                .where(
                    PublicationLog.destination_id == destination.id,
                    PublicationLog.created_at > now - timedelta(seconds=seconds),
                )
            )
            if count >= limit:
                raise publication_error("OUTBOX_RATE_LIMITED")


class _ReservedSender:
    def __init__(self, sender, before_http):
        self.sender, self.before_http = sender, before_http

    def send(self, chat_id, message):
        self.before_http()
        return self.sender.send(chat_id, message)


class _AnnouncementPublication(_PilotPublication):
    def _read_bundle(self, session, offer_id, now):
        return read_announcement(session, self.settings, offer_id, now)

    @staticmethod
    def _render(bundle, include_image):
        return render_announcement(bundle, include_image=include_image)


class AutoPilotService:
    mode = "MOCK_ONLY"
    active_state = "ACTIVE_MOCK"

    def __init__(self, factory, settings, *, discovery=None, publisher=None, clock=None):
        self.factory, self.settings = factory, settings
        self.discovery, self.publisher = discovery, publisher
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self):
        return aware_utc(self.clock())

    def _safe(self, *, execution=False):
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
        if execution:
            client = getattr(self.publisher, "_client", None)
            if (
                type(self.publisher) is not TelegramPublisher
                or type(client) is not httpx.Client
                or type(client._transport_for_url(httpx.URL("https://api.telegram.org")))
                is not httpx.MockTransport
            ):
                raise ValueError("AUTO_PILOT desta etapa exige Telegram MockTransport explícito.")
            if self.discovery is None:
                raise ValueError("Descoberta offline não configurada.")
            connector = self.discovery._offline()  # MockTransport, no retry/cache.
            if (
                connector.app_id != self.settings.shopee_app_id
                or connector.secret != self.settings.shopee_secret
            ):
                raise ValueError("Credencial do conector difere do plano.")

    def _credentials(self):
        def secret(value):
            return value.get_secret_value() if value else ""

        return fingerprint(
            dict(
                shopee_app=secret(self.settings.shopee_app_id),
                shopee_secret=secret(self.settings.shopee_secret),
                telegram=secret(self.publisher.settings.telegram_bot_token)
                if self.publisher
                else "",
            )
        )

    @staticmethod
    def _save(row, value):
        row.value = json.loads(json.dumps(value, default=str))

    def _load(self, session, pilot_id, *, lock=False):
        query = select(AppSetting).where(AppSetting.key == PREFIX + str(UUID(str(pilot_id))))
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise LookupError("Piloto inexistente.")
        value = copy.deepcopy(row.value)
        if value["scope"]["mode"] != self.mode:
            raise ValueError("Perfil de execução divergente.")
        if fingerprint(value["scope"]) != value["scope_sha256"]:
            raise ValueError("Escopo do piloto alterado.")
        if value["scope"]["credential_fingerprint"] != self._credentials():
            raise ValueError("Credencial divergente do escopo.")
        AutoPilotPlan.model_validate(value["scope"]["plan"])
        destination = session.get(PublicationDestination, value["scope"]["plan"]["destination_id"])
        if (
            destination is None
            or destination.external_id != value["scope"]["chat_id"]
            or destination.channel != DestinationChannel.TELEGRAM
        ):
            raise ValueError("Destino divergente do escopo do piloto.")
        return row, value

    def prepare(self, data):
        self._safe()
        # Version only new, unpinned grants. Existing signed plans and exact
        # previously reviewed messages must retain their original rendering.
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="json", exclude_none=True)
        if (
            data.get("commercial_mode") == ANNOUNCEMENT_ONLY
            and "announcement_template" not in data
            and not data.get("expected_message_sha256")
        ):
            data = dict(data, announcement_template="PRO_V2")
        plan = AutoPilotPlan.model_validate(data)
        with self.factory() as session:
            destination = session.get(PublicationDestination, plan.destination_id)
            if destination is None or destination.channel != DestinationChannel.TELEGRAM:
                raise ValueError("Destino Telegram inexistente.")
            chat_id = destination.external_id
        plan_data = plan.model_dump(mode="json", exclude_none=True)
        if plan.operating_policy and plan.operating_policy.telegram_per_minute:
            plan_data["messages_per_window"] = plan.messages_per_window
        scope = dict(
            plan=plan_data,
            credential_fingerprint=self._credentials(),
            mode=self.mode,
            shopee_operation="productOfferV2",
            max_external_http=plan.max_queries + plan.max_messages,
            shopee_endpoint="https://open-api.affiliate.shopee.com.br/graphql",
            telegram_endpoint="https://api.telegram.org",
            retries=0,
            fallback=False,
            chat_id=chat_id,
        )
        scope.update(self._scope_extra(plan))
        value = dict(
            scope=scope,
            scope_sha256=fingerprint(scope),
            state="PREPARED",
            reason=None,
            queries=[],
            queue=[],
            reservations=[],
            findings=[],
            automatic_approvals=0,
        )
        self._seal_new_record(value)
        key = PREFIX + str(plan.authorization_id)
        with self.factory() as session:
            session.execute(
                conflict_insert(session, AppSetting)
                .values(key=key, value=value)
                .on_conflict_do_nothing(index_elements=[AppSetting.key])
            )
            row, existing = self._load(session, plan.authorization_id)
            if existing["scope"] != scope:
                raise ValueError("UUID já vinculado a outro escopo.")
            session.commit()
        return self.report(plan.authorization_id)

    def _scope_extra(self, plan):
        if plan.daily_grant_id:
            raise ValueError("Operação diária exige adaptador real.")
        if plan.duration_seconds is not None:
            raise ValueError("Duração a partir da ativação exige o adaptador real.")
        return {}

    def _seal_new_record(self, value):
        pass

    def _discover_analysis(self, pilot_id, reservation):
        return self.discovery.search_multistore(reservation["term"], limit=3)

    def _after_outcome(self, value, state, reason):
        pass

    def _bind_transport_fence(self, lease, owner):
        pass

    def _active(self, value):
        if value["state"] in TERMINAL:
            return False
        plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
        if self._now() >= self._deadline(value):
            value.update(state="COMPLETED", reason="DEADLINE")
        elif (
            not self._independent_budgets(value)
            and len(value["reservations"]) >= plan.max_messages
            and not any(r["state"] == "CLAIMED" for r in value["reservations"])
        ):
            value.update(state="COMPLETED", reason="MESSAGE_BUDGET")
        return value[
            "state"
        ] == self.active_state and plan.not_before <= self._now() < self._deadline(value)

    def _deadline(self, value):
        return AutoPilotPlan.model_validate(value["scope"]["plan"]).expires_at

    @staticmethod
    def _operating_policy(value):
        # Never upgrade a previously signed scope by supplying a model default.
        raw = value["scope"]["plan"].get("operating_policy")
        return OperatingPolicy.model_validate(raw) if raw is not None else None

    @staticmethod
    def _independent_budgets(value):
        policy = value["scope"]["plan"].get("operating_policy") or {}
        return policy.get("continue_discovery_after_message_budget") is True

    def _query_interval(self, value):
        if value["scope"]["plan"]["max_queries"] == 0:
            return 0
        policy = self._operating_policy(value)
        if policy is None:
            return 0
        plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
        return policy.query_interval_seconds or max(
            30,
            (plan.duration_seconds or (plan.expires_at - plan.not_before).total_seconds())
            / plan.max_queries,
        )

    def _next_query_at(self, value):
        plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
        if len(value["queries"]) >= plan.max_queries or value["state"] in TERMINAL:
            return None
        anchor = (
            datetime.fromisoformat(value["started_at"])
            if value.get("started_at")
            else plan.not_before
        )
        interval = self._query_interval(value)
        due = anchor + timedelta(seconds=len(value["queries"]) * interval)
        if value["queries"]:
            # Lost ticks/restarts never trigger a burst of catch-up requests.
            due = max(
                due,
                datetime.fromisoformat(value["queries"][-1]["at"]) + timedelta(seconds=interval),
            )
        return due if due < self._deadline(value) else None

    def _query_due(self, value):
        due = self._next_query_at(value)
        return due is not None and self._now() >= due

    def start_offline(self, pilot_id, scope_sha256):
        self._safe(execution=True)
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
            if scope_sha256 != value["scope_sha256"] or value["state"] != "PREPARED":
                raise ValueError("Início exige escopo preparado íntegro e ainda não utilizado.")
            if not plan.not_before <= self._now() < plan.expires_at:
                raise ValueError("Fora da janela do piloto.")
            value["state"] = "ACTIVE_MOCK"
            value["started_at"] = self._now().isoformat()
            self._save(row, value)
            session.commit()
        return self.report(pilot_id)

    def stop(self, pilot_id):
        self._safe()
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            if value["state"] not in TERMINAL:
                value.update(state="STOPPED", reason="ADMIN_STOP")
            self._save(row, value)
            session.commit()
        return self.report(pilot_id)

    def _reserve_query(self, pilot_id):
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
            if not self._active(value) or not self._query_due(value):
                self._save(row, value)
                session.commit()
                return None
            reservation = dict(
                id=uuid4().hex,
                state="RESERVED",
                at=self._now().isoformat(),
                term=plan.terms[len(value["queries"]) % len(plan.terms)],
            )
            value["queries"].append(reservation)
            self._save(row, value)
            session.commit()
            return reservation

    def _eligible(self, session, offer_id, quote, *, commercial_mode=None):
        if commercial_mode == ANNOUNCEMENT_ONLY:
            bundle = read_announcement(session, self.settings, offer_id, self._now())
            if quote != bundle.history.evidence_metadata["announcement_only"]["quote"]:
                raise ValueError("ANNOUNCEMENT_QUOTE_CHANGED")
            return bundle
        bundle = read_manual_offer(session, offer_id, self.settings, self._now())
        p = bundle.product
        proof = DocumentedIdentity.model_validate(
            (bundle.history.evidence_metadata or {}).get("documented_identity") or {}
        )
        if (
            p.platform != Platform.SHOPEE
            or url_identity(p.original_url) != expected_identity(p)
            or p.external_id != quote["identity"]
            or not proof.accepts_observation_name(p.name, quote["name"])
            or quote["price_status"] != "VALID_ANNOUNCED_PRICE"
            or Decimal(str(quote["announced_price"])) != bundle.offer.current_price
            or quote["status"] == "REJECTED"
            or proof.identity != p.external_id
            or proof.variant_id != p.variant_id
            or proof.original_url_sha256 != fingerprint({"original_url": p.original_url})
            or aware_utc(proof.verified_at) > self._now()
        ):
            raise ValueError(
                "Identidade/variante/preço não sustentados pela evidência comercial vigente."
            )
        return bundle

    def discover_once(self, pilot_id):
        self._safe(execution=True)
        reservation = self._reserve_query(pilot_id)
        if reservation is None:
            return
        try:
            with self.factory() as session:
                _, current = self._load(session, pilot_id)
                if not self._active(current):
                    raise ValueError("Piloto encerrado antes da consulta reservada.")
            analysis = self._discover_analysis(pilot_id, reservation)
            error = None
            diagnostic = {}
            code = None
        except Exception as exc:
            analysis, error = None, type(exc).__name__
            diagnostic = http_diagnostics(exc)
            code = getattr(exc, "code", None)
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            query = next(q for q in value["queries"] if q["id"] == reservation["id"])
            query.update(state="SUCCEEDED" if analysis else "FAILED_OR_UNCERTAIN", error=error)
            from app.services.daily_queue import continuous, record_capture

            if continuous(value) and code == "RATE_LIMITED":
                query["state"] = "RATE_LIMITED"
            if diagnostic:
                query["http_diagnostics"] = diagnostic
            if analysis:
                # Separate durable discovery evidence from operational temporal
                # prices and the publication outbox. Preserve even a late response.
                comparison = json.loads(json.dumps(analysis, default=str))
                query["capture"] = dict(
                    capture_id=query["id"],
                    authorization_id=str(pilot_id),
                    mode=self.mode,
                    execution_environment="ISOLATED_MOCK"
                    if self.mode == "MOCK_ONLY" or getattr(self, "isolated_testing", False)
                    else "PRODUCTION",
                    source="shopee:official_api",
                    recorded_at=self._now().isoformat(),
                    comparison_sha256=fingerprint(comparison),
                    comparison=comparison,
                )
                record_capture(session, value, query, self._now())
            if analysis and self._active(value):
                query["proven_groups"] = len(analysis["proven_comparisons"])
                sends_exhausted = self._independent_budgets(value) and (
                    len(value["reservations"]) >= value["scope"]["plan"]["max_messages"]
                )
                for quote in analysis["candidates"]:
                    finding = dict(
                        identity=quote["identity"],
                        capture_id=query["id"],
                        name=quote["name"],
                        announced_price=quote["announced_price"],
                        currency=quote["currency"],
                        observed_at=quote["observed_at"],
                        canonical_url=quote.get("canonical_url"),
                        status="INCONCLUSIVE",
                        reason="NO_VERIFIED_COMMERCIAL_OFFER",
                    )
                    if quote["status"] == "REJECTED":
                        finding.update(status="REJECTED", reason=quote["reason"])
                    elif sends_exhausted:
                        finding["reason"] = "DAILY_MESSAGE_BUDGET_EXHAUSTED"
                    elif value["scope"]["plan"].get("commercial_mode") == ANNOUNCEMENT_ONLY:
                        quote = json.loads(json.dumps(quote, default=str))
                        try:
                            allowed = value["scope"]["plan"].get("announcement_identities")
                            if allowed is not None and quote["identity"] not in allowed:
                                raise ValueError("LISTING_OUTSIDE_ANNOUNCEMENT_SCOPE")
                            if self._already_queued(value, quote["identity"]):
                                raise ValueError("ALREADY_QUEUED_OR_ATTEMPTED")
                            self._append_announcement(session, value, pilot_id, query["id"], quote)
                            finding.update(
                                status="QUEUED", reason="ANNOUNCEMENT_ONLY_OFFICIAL_CAPTURE"
                            )
                        except (IntegrationError, ValueError) as exc:
                            finding["reason"] = (
                                str(exc) if isinstance(exc, ValueError) else exc.code
                            )
                    else:
                        ids = session.scalars(
                            select(Offer.id)
                            .join(Product, Product.id == Offer.product_id)
                            .where(
                                Product.platform == Platform.SHOPEE,
                                Product.external_id == quote["identity"],
                            )
                            .order_by(Offer.id.desc())
                            .limit(20)
                        ).all()
                        for offer_id in ids:
                            try:
                                bundle = self._eligible(session, offer_id, quote)
                            except (IntegrationError, ValueError) as exc:
                                finding["reason"] = getattr(
                                    exc, "code", "INCOMPLETE_IDENTITY_OR_VARIANT"
                                )
                                continue
                            if not self._append_verified_offer(value, bundle, quote):
                                finding.update(
                                    status="REJECTED", reason="ALREADY_QUEUED_OR_ATTEMPTED"
                                )
                            else:
                                finding.update(
                                    status="QUEUED", reason="EXISTING_VERIFIED_COMMERCIAL_EVIDENCE"
                                )
                            break
                    value["findings"].append(finding)
            if analysis:
                value["opportunities"] = pilot_opportunities(
                    [q["capture"] for q in value["queries"] if q.get("capture")],
                    now=self._now(),
                    commercial_queue=value["queue"],
                    signals_by_identity=load_ranking_signals(
                        session, [r["identity"] for r in analysis["candidates"]], now=self._now()
                    ),
                )
            self._save(row, value)
            session.commit()

    def _append_announcement(self, session, value, pilot_id, capture_id, quote):
        from app.services.daily_queue import continuous, queue_prefix, setting

        if continuous(value):
            row = setting(session, queue_prefix(value) + quote["identity"])
            if not row or row.value["state"] != "PENDING":
                raise ValueError("DAILY_CANDIDATE_ALREADY_HANDLED")
        with session.begin_nested():
            bundle = create_announcement(
                session,
                self.settings,
                value,
                pilot_id,
                capture_id,
                quote,
                self._now(),
            )
        value["queue"].append(
            dict(
                id=uuid4().hex,
                offer_id=bundle.offer.id,
                product_id=bundle.product.id,
                identity=quote["identity"],
                documented_product_key="listing:" + quote["identity"],
                identity_evidence_sha256=evidence_hash(bundle),
                shop_id=quote["shop_id"],
                destination_id=value["scope"]["plan"]["destination_id"],
                quote=quote,
                state="QUEUED",
                commercial_mode=ANNOUNCEMENT_ONLY,
            )
        )

    @staticmethod
    def _already_queued(value, identity):
        from app.services.daily_queue import continuous

        if continuous(value) and not any(r["identity"] == identity for r in value["reservations"]):
            return any(
                q["identity"] == identity and q["state"] not in {"EXPIRED", "REJECTED"}
                for q in value["queue"]
            )
        return any(q["identity"] == identity for q in value["queue"])

    def _append_verified_offer(self, value, bundle, quote):
        proof = DocumentedIdentity.model_validate(
            bundle.history.evidence_metadata["documented_identity"]
        )
        product_key = proof.duplicate_product_key()
        if any(
            (q["identity"] == quote["identity"] or q.get("documented_product_key") == product_key)
            and q["state"] not in {"REJECTED", "EXPIRED"}
            for q in value["queue"]
        ):
            return False
        value["queue"].append(
            dict(
                id=uuid4().hex,
                offer_id=bundle.offer.id,
                product_id=bundle.product.id,
                identity=quote["identity"],
                documented_product_key=product_key,
                identity_evidence_sha256=fingerprint(
                    bundle.history.evidence_metadata["documented_identity"]
                ),
                shop_id=quote["shop_id"],
                destination_id=value["scope"]["plan"]["destination_id"],
                quote=json.loads(json.dumps(quote, default=str)),
                state="QUEUED",
            )
        )
        return True

    def enqueue_documented_imports(self, pilot_id):
        """Consume current imported evidence locally, through the same queue/guards.

        This is not discovery and creates no observations or attestations. Search
        terms still limit relevance; incomplete imports cannot bypass eligibility.
        """
        self._safe(execution=True)
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            if not self._active(value):
                self._save(row, value)
                session.commit()
                return
            if value["scope"]["plan"].get("commercial_mode") == ANNOUNCEMENT_ONLY:
                return
            offers = session.execute(
                select(Offer, Product, ProductPriceHistory)
                .join(Product, Product.id == Offer.product_id)
                .join(ProductPriceHistory, ProductPriceHistory.id == Offer.price_history_id)
                .where(Product.platform == Platform.SHOPEE, Offer.expires_at > self._now())
                .order_by(Offer.id.desc())
                .limit(200)
            ).all()
            for offer, product, history in offers:
                if not (history.evidence_metadata or {}).get("official_export"):
                    continue
                if not any(
                    relevance(product.name, SearchTerm(query=term), [])[0] == "SELECTED"
                    for term in value["scope"]["plan"]["terms"]
                ):
                    continue
                shop_id, item_id = (product.external_id or ":").split(":", 1)
                quote = dict(
                    identity=product.external_id,
                    shop_id=shop_id,
                    item_id=item_id,
                    name=product.name,
                    announced_price=str(offer.current_price),
                    price_status="VALID_ANNOUNCED_PRICE",
                    status="CANDIDATE",
                    evidence_source="CONFIRMED_OFFICIAL_EXPORT",
                    source_sha256=history.evidence_metadata["official_export"]["content_sha256"],
                )
                try:
                    bundle = self._eligible(session, offer.id, quote)
                except (IntegrationError, ValueError):
                    continue
                if self._append_verified_offer(value, bundle, quote):
                    value["findings"].append(
                        dict(
                            identity=product.external_id,
                            status="QUEUED",
                            reason="EXISTING_VERIFIED_OFFICIAL_EXPORT",
                        )
                    )
            self._save(row, value)
            session.commit()

    def _reserve_message(self, pilot_id, queue_id):
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
            if not self._active(value) or len(value["reservations"]) >= plan.max_messages:
                self._save(row, value)
                session.commit()
                return None
            self._check_daily(session, value)
            recent = [
                r
                for r in value["reservations"]
                if datetime.fromisoformat(r["at"]) > self._now() - timedelta(seconds=600)
            ]
            if plan.messages_per_window is not None and len(recent) >= plan.messages_per_window:
                return None
            item = next(q for q in value["queue"] if q["id"] == queue_id)
            if item["state"] != "QUEUED":
                return None
            # Cross-pilot listing/destination cooldown; UNCERTAIN never expires.
            for other in session.scalars(
                select(AppSetting.value).where(AppSetting.key.startswith(PREFIX))
            ):
                for r in other["reservations"]:
                    if (
                        (
                            r["identity"] == item["identity"]
                            or r.get("documented_product_key") == item["documented_product_key"]
                        )
                        and r["destination_id"] == plan.destination_id
                        and (
                            r["state"] in {"CLAIMED", "UNCERTAIN"}
                            or datetime.fromisoformat(r["at"])
                            > self._now() - timedelta(seconds=plan.cooldown_seconds)
                        )
                    ):
                        item.update(state="REJECTED", reason="LISTING_DESTINATION_COOLDOWN")
                        self._save(row, value)
                        session.commit()
                        return None
            try:
                if item.get("commercial_mode") != plan.commercial_mode and (
                    item.get("commercial_mode") == ANNOUNCEMENT_ONLY
                    or plan.commercial_mode == ANNOUNCEMENT_ONLY
                ):
                    raise ValueError("Commercial publication mode mismatch")
                if (
                    plan.announcement_identities is not None
                    and item["identity"] not in plan.announcement_identities
                ):
                    raise ValueError("Listing outside authorized scope")
                bundle = self._eligible(
                    session, item["offer_id"], item["quote"], commercial_mode=plan.commercial_mode
                )
                if evidence_hash(bundle) != item["identity_evidence_sha256"]:
                    raise ValueError("Documento alterado depois da seleção.")
            except (IntegrationError, ValueError) as exc:
                item.update(
                    state="REJECTED", reason=getattr(exc, "code", "COMMERCIAL_EVIDENCE_INVALID")
                )
                self._save(row, value)
                session.commit()
                return None
            reservation = dict(
                id=uuid4().hex,
                queue_id=queue_id,
                state="CLAIMED",
                at=self._now().isoformat(),
                identity=item["identity"],
                documented_product_key=item["documented_product_key"],
                destination_id=plan.destination_id,
                http_started=False,
            )
            value["reservations"].append(reservation)
            item["state"] = "CLAIMED"
            self._save(row, value)
            session.commit()
            return reservation

    def _before_http(self, pilot_id, reservation_id):
        self._safe(execution=True)
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            plan = AutoPilotPlan.model_validate(value["scope"]["plan"])
            # The final reserved message may already exhaust the budget, but has
            # one permit. No other operation can claim that permit a second time.
            if value[
                "state"
            ] != self.active_state or not plan.not_before <= self._now() < self._deadline(value):
                raise ValueError("Piloto encerrado antes do HTTP.")
            r = next(r for r in value["reservations"] if r["id"] == reservation_id)
            if r["state"] != "CLAIMED" or r["http_started"]:
                raise ValueError("Permissão HTTP consumida.")
            r["http_started"] = True
            self._save(row, value)
            session.commit()

    def dispatch_batch(self, pilot_id):
        self._safe(execution=True)
        report = self.report(pilot_id)
        policy = report["scope"]["plan"].get("operating_policy") or {}
        if self._independent_budgets(report) and (
            len(report["reservations"]) >= report["scope"]["plan"]["max_messages"]
        ):
            return report
        destination_id = report["scope"]["plan"]["destination_id"]
        lease = "auto-pilot-destination:" + str(destination_id)
        owner = acquire_lease(self.factory, lease, 120, self.clock)
        if owner is None:
            return self.report(pilot_id)
        try:
            items = report["queue"]
            if report["scope"]["plan"].get("announcement_selection") == "RANKING_100_V1":
                ranks = {
                    r["identity"]: r["intelligent_rank"]
                    for r in report.get("opportunities", {}).get("analysis_queue", [])
                    if isinstance(r.get("intelligent_rank"), int)
                }
                items = sorted(items, key=lambda item: ranks.get(item["identity"], 1000000))
            for item in items:
                if item["state"] != "QUEUED":
                    continue
                with self.factory() as session:
                    if not renew_lease(session, lease, owner, 120, self.clock):
                        break
                    if self._independent_budgets(report):
                        try:
                            _PilotPublication.check_rate(
                                session,
                                session.get(PublicationDestination, destination_id),
                                self._now(),
                                policy,
                            )
                        except IntegrationError:
                            break
                    session.commit()
                reservation = self._reserve_message(pilot_id, item["id"])
                if reservation is None:
                    continue
                state, reason, publication_id = "UNCERTAIN", "INTERRUPTED_OR_UNKNOWN", None
                try:

                    def before_http(permit=reservation["id"]):
                        with self.factory() as session:
                            if not renew_lease(session, lease, owner, 120, self.clock):
                                raise ValueError("Reserva do worker expirou antes do HTTP.")
                            session.commit()
                        self._bind_transport_fence(lease, owner)
                        self._before_http(pilot_id, permit)

                    sender = _ReservedSender(self.publisher, before_http)
                    publication_type = (
                        _AnnouncementPublication
                        if report["scope"]["plan"].get("commercial_mode") == ANNOUNCEMENT_ONLY
                        else _PilotPublication
                    )
                    publication = publication_type(
                        self.publisher.settings, sender, self.clock, policy=policy
                    )
                    with self.factory() as session:
                        entry = publication.preview(
                            session,
                            item["offer_id"],
                            destination_id,
                            report["scope"]["plan"].get("announcement_template") == "PRO_V2",
                        )
                        session.commit()
                        publication_id, digest = entry.id, entry.preview_hash
                    publication.approve(
                        self.factory, publication_id, digest, "AUTO_PILOT_RULES:" + str(pilot_id)
                    )
                    with self.factory() as session:
                        row, value = self._load(session, pilot_id, lock=True)
                        value["automatic_approvals"] += 1
                        self._save(row, value)
                        session.commit()
                    outcome = publication.publish(self.factory, publication_id, digest)
                    state, reason = outcome.status.value, outcome.last_error_code
                except (IntegrationError, ValueError) as exc:
                    state, reason = (
                        "REJECTED",
                        getattr(exc, "code", "PILOT_OR_COMMERCIAL_SCOPE_INVALID"),
                    )
                with self.factory() as session:
                    row, value = self._load(session, pilot_id, lock=True)
                    r = next(r for r in value["reservations"] if r["id"] == reservation["id"])
                    r.update(state=state, reason=reason, publication_id=publication_id)
                    next(q for q in value["queue"] if q["id"] == item["id"]).update(
                        state=state, reason=reason
                    )
                    from app.services.daily_queue import record_dispatch

                    record_dispatch(session, value, item["identity"], state, reason)
                    self._after_outcome(value, state, reason)
                    self._active(value)
                    self._save(row, value)
                    session.commit()
        finally:
            release_lease(self.factory, lease, owner)
        return self.report(pilot_id)

    def collect_commercial_evidence(self, pilot_id):
        self._safe()
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            policy = value["scope"]["plan"].get("commercial_collection")
            if policy and value["state"] == self.active_state and self._active(value):
                value["commercial_collection"] = collect_documents(
                    session,
                    self.settings,
                    policy,
                    value.get("commercial_collection"),
                    now=self._now(),
                )
            self._save(row, value)
            session.commit()

    def _check_daily(self, session, value):
        pass

    def _enrich_daily(self, pilot_id):
        pass

    def tick(self, pilot_id):
        current = self.report(pilot_id)
        if current["state"] in TERMINAL:
            return current
        self.collect_commercial_evidence(pilot_id)
        self.enqueue_documented_imports(pilot_id)
        self._enrich_daily(pilot_id)
        self.dispatch_batch(pilot_id)
        # Do not spend query budget while the destination's rolling window is
        # full. At most two pages (different configured terms) fill one burst.
        for _ in range(2):
            current = self.report(pilot_id)
            plan = current["scope"]["plan"]
            recent = sum(
                datetime.fromisoformat(r["at"]) > self._now() - timedelta(seconds=600)
                for r in current["reservations"]
            )
            if (
                current["state"] != self.active_state
                or (
                    not self._independent_budgets(current) and recent >= plan["messages_per_window"]
                )
                or not self._query_due(current)
            ):
                break
            before = len(current["queue"])
            self.discover_once(pilot_id)
            self.collect_commercial_evidence(pilot_id)
            self.enqueue_documented_imports(pilot_id)
            self._enrich_daily(pilot_id)
            after = self.dispatch_batch(pilot_id)
            if (
                len(after["queue"]) == before
                or after["counts"]["published"] == current["counts"]["published"]
            ):
                break
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            captures = [q["capture"] for q in value["queries"] if q.get("capture")]
            value["opportunities"] = pilot_opportunities(
                captures,
                now=self._now(),
                commercial_queue=value["queue"],
                signals_by_identity=load_ranking_signals(
                    session,
                    [r["identity"] for c in captures for r in c["comparison"]["candidates"]],
                    now=self._now(),
                ),
            )
            value["discovery_state"] = (
                "QUERY_BUDGET_EXHAUSTED"
                if len(value["queries"]) >= value["scope"]["plan"]["max_queries"]
                else "READY"
            )
            pending = any(q["state"] in {"QUEUED", "CLAIMED"} for q in value["queue"])
            if (
                self._independent_budgets(value)
                and len(value["reservations"]) >= value["scope"]["plan"]["max_messages"]
            ):
                pending = False
            exhausted = len(value["queries"]) >= value["scope"]["plan"]["max_queries"]
            policy = self._operating_policy(value)
            value["activity_state"] = (
                "PROCESSING_PUBLICATION_QUEUE"
                if pending
                else "WAITING_NO_WORK_QUERY_BUDGET"
                if exhausted
                else "WAITING_NEXT_QUERY"
                if self._next_query_at(value) is not None
                else "WAITING_FOR_WINDOW_END"
            )
            if (
                value["state"] == self.active_state
                and exhausted
                and not pending
                and (policy is None or policy.on_no_work == "STOP_WHEN_DRAINED")
            ):
                value.update(
                    state="COMPLETED",
                    reason="QUERY_BUDGET" if policy is None else "NO_WORK_AFTER_QUERY_BUDGET",
                )
            self._save(row, value)
            session.commit()
        return self.report(pilot_id)

    def report(self, pilot_id):
        self._safe()
        with self.factory() as session:
            row, value = self._load(session, pilot_id, lock=True)
            if value["state"] == self.active_state or (
                value["state"] == "PAUSED"
                and value["scope"]["plan"].get("duration_seconds")
                and value.get("started_at")
            ):
                self._active(value)
                self._save(row, value)
                session.commit()
            reservations = value["reservations"]
            return dict(
                authorization_id=str(pilot_id),
                **value,
                counts=dict(
                    found=len(value["findings"]),
                    distinct_listings_found=len({f["identity"] for f in value["findings"]}),
                    queries_reserved=len(value["queries"]),
                    messages_reserved=len(reservations),
                    http_attempts=sum(r["http_started"] for r in reservations),
                    automatically_approved=value["automatic_approvals"],
                    published=sum(r["state"] == "SENT" for r in reservations),
                    analysis_candidates=len(
                        value.get("opportunities", {}).get("analysis_queue", [])
                    ),
                    proven_comparison_groups=len(
                        value.get("opportunities", {}).get("proven_comparisons", [])
                    ),
                    cheap_opportunities=sum(
                        r.get("opportunity", {}).get("kind") == "BELOW_PROVEN_GROUP_MEAN_AND_MEDIAN"
                        for r in value.get("opportunities", {}).get("analysis_queue", [])
                    ),
                    publishable_queue=sum(q["state"] == "QUEUED" for q in value["queue"]),
                    uncertain=sum(r["state"] == "UNCERTAIN" for r in reservations),
                    rejected=sum(q["state"] == "REJECTED" for q in value["queue"])
                    + sum(f["status"] == "REJECTED" for f in value["findings"]),
                    inconclusive=sum(f["status"] == "INCONCLUSIVE" for f in value["findings"]),
                ),
                real_activation_allowed=False,
                query_schedule=dict(
                    interval_seconds=self._query_interval(value),
                    next_query_at=self._next_query_at(value).isoformat()
                    if self._next_query_at(value)
                    else None,
                    remaining=max(0, value["scope"]["plan"]["max_queries"] - len(value["queries"])),
                    budget_exhausted=len(value["queries"]) >= value["scope"]["plan"]["max_queries"],
                    operating_policy="WINDOWED"
                    if self._operating_policy(value)
                    else "LEGACY_SIGNED_SCOPE",
                ),
                external_http_calls=0,
                authority="MOCK_ONLY_PILOT_RULES_NOT_HUMAN_MESSAGE_APPROVAL",
            )
