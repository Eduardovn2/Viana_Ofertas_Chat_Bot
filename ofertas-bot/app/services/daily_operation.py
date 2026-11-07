"""Recurring authority and one durable AUTO_PILOT per Brasília day."""

import hashlib
import hmac
from datetime import date, datetime, time, timedelta
from typing import Literal
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.models import AppSetting, DestinationChannel, PublicationDestination
from app.services.leases import conflict_insert

ZONE = ZoneInfo("America/Sao_Paulo")
GRANT = "daily_grant:"
DAY = "daily_run:"
CONTROL = "daily_operation_control"


class DailyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: UUID
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    destination_id: int = Field(gt=0)
    start_date: date
    end_date: date
    start_time: time = time(8)
    end_time: time = time(23)
    days: list[StrictInt] = Field(
        default_factory=lambda: list(range(7)), min_length=1, max_length=7
    )
    commercial_mode: Literal["ANNOUNCEMENT_ONLY", "VERIFIED_OFFER"] = "ANNOUNCEMENT_ONLY"
    terms: list[str] = Field(min_length=1, max_length=20)
    max_queries: int | None = Field(default=None, ge=1, strict=True)
    max_links: int | None = Field(default=None, ge=0, strict=True)
    max_messages: int | None = Field(default=None, ge=1, strict=True)
    messages_per_window: int | None = Field(default=None, ge=1, strict=True)
    query_interval_seconds: int | None = Field(default=None, ge=60, le=7200, strict=True)
    operation_version: Literal["DAILY_V2", "DAILY_V3"] | None = None
    telegram_per_minute: int = Field(default=20, ge=1, le=20, strict=True)
    administrative_messages_per_hour: int | None = Field(default=None, ge=1, strict=True)
    shopee_posts_per_minute: int | None = Field(default=None, ge=1, strict=True)
    cooldown_seconds: int = Field(default=10800, ge=10800, le=86400, strict=True)
    quota_reference: str | None = Field(default=None, min_length=3, max_length=300)
    shopee_daily_quota: int | None = Field(default=None, gt=0, strict=True)
    telegram_daily_quota: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def limits(self):
        from app.services.auto_pilot import AutoPilotPlan

        if (
            self.end_date < self.start_date
            or self.start_time.tzinfo
            or self.end_time.tzinfo
            or self.end_time <= self.start_time
            or len(set(self.days)) != len(self.days)
            or any(type(d) is not int or not 0 <= d <= 6 for d in self.days)
        ):
            raise ValueError("Datas, horários ou dias inválidos.")
        if self.max_links and self.commercial_mode != "ANNOUNCEMENT_ONLY":
            raise ValueError("Geração diária de links exige modo informativo.")
        if self.operation_version != "DAILY_V3" and (
            not self.max_messages
            or self.max_messages > 100
            or not self.messages_per_window
            or self.messages_per_window > 5
            or (self.max_queries or 0) > 1440
            or (self.max_links or 0) > 4320
        ):
            raise ValueError("Limites do formato diário legado excedidos.")
        if self.operation_version == "DAILY_V3" and self.commercial_mode != "ANNOUNCEMENT_ONLY":
            raise ValueError("A fila contínua requer ANNOUNCEMENT_ONLY.")
        if self.quota_reference or self.shopee_daily_quota or self.telegram_daily_quota:
            if (
                not self.quota_reference
                or (self.shopee_daily_quota or 0) < (self.max_queries or 0) + (self.max_links or 0)
                or (
                    self.operation_version != "DAILY_V3"
                    and (self.telegram_daily_quota or 0) < self.max_messages
                )
            ):
                raise ValueError("Quotas documentadas insuficientes para os limites.")
        AutoPilotPlan.distinct_terms(self.terms)
        if self.budgets_defined():
            AutoPilotPlan.model_validate(self.child(self.start_date))
        return self

    def budgets_defined(self):
        if self.operation_version == "DAILY_V3":
            return all(
                v is not None
                for v in (
                    self.max_queries,
                    self.max_links,
                    self.shopee_posts_per_minute,
                    self.shopee_daily_quota,
                )
            ) and bool(self.quota_reference)
        return all(
            v is not None for v in (self.max_queries, self.max_links, self.query_interval_seconds)
        )

    def effective_message_limit(self):
        if self.operation_version != "DAILY_V3":
            return self.max_messages
        start, end = self.window(self.start_date)
        seconds = int((end - start).total_seconds())
        limits = [((seconds + 59) // 60) * self.telegram_per_minute]
        if self.max_messages is not None:
            limits.append(self.max_messages)
        if self.telegram_daily_quota is not None:
            limits.append(self.telegram_daily_quota)
        return min(limits)

    def window(self, day):
        return datetime.combine(day, self.start_time, ZONE), datetime.combine(
            day, self.end_time, ZONE
        )

    def child(self, day):
        if not self.budgets_defined():
            raise ValueError("Defina orçamento de consultas, links e intervalo de busca.")
        start, end = self.window(day)
        policy = {"query_interval_seconds": self.query_interval_seconds}
        if self.operation_version in {"DAILY_V2", "DAILY_V3"}:
            policy["continue_discovery_after_message_budget"] = True
        if self.operation_version == "DAILY_V3":
            policy.update(
                telegram_per_minute=self.telegram_per_minute,
                administrative_messages_per_hour=self.administrative_messages_per_hour,
                shopee_posts_per_minute=self.shopee_posts_per_minute,
            )
        return dict(
            authorization_id=str(uuid5(self.authorization_id, day.isoformat())),
            daily_grant_id=str(self.authorization_id),
            actor=self.actor,
            destination_id=self.destination_id,
            terms=self.terms,
            not_before=start,
            expires_at=end,
            max_queries=self.max_queries,
            max_messages=self.effective_message_limit(),
            messages_per_window=self.messages_per_window,
            cooldown_seconds=self.cooldown_seconds,
            commercial_mode=self.commercial_mode,
            announcement_template="PRO_V2" if self.commercial_mode == "ANNOUNCEMENT_ONLY" else None,
            announcement_selection="RANKING_100_V1"
            if self.commercial_mode == "ANNOUNCEMENT_ONLY"
            else None,
            operating_policy=policy,
        )


def _signature(settings, kind, value):
    key = hashlib.sha256(
        (
            settings.shopee_app_id.get_secret_value()
            + "\0"
            + settings.shopee_secret.get_secret_value()
            + "\0"
            + settings.telegram_bot_token.get_secret_value()
        ).encode()
    ).digest()
    return hmac.new(
        key, ("daily-v1:" + kind + ":" + fingerprint(value)).encode(), hashlib.sha256
    ).hexdigest()


def setting(session, key, *, lock=False):
    query = select(AppSetting).where(AppSetting.key == key)
    return session.scalar(query.with_for_update() if lock else query)


def ensure_setting(session, key, value):
    session.execute(
        conflict_insert(session, AppSetting)
        .values(key=key, value=value)
        .on_conflict_do_nothing(index_elements=[AppSetting.key])
    )
    return setting(session, key, lock=True)


def grant(session, settings, identifier, *, approved=True, lock=False):
    row = setting(session, GRANT + str(identifier), lock=lock)
    if row is None:
        raise ValueError("Autorização diária inexistente.")
    value = row.value
    scope = value["scope"]
    approval = value.get("approval")
    if fingerprint(scope) != value["scope_sha256"] or not hmac.compare_digest(
        value["signature"], _signature(settings, "scope", scope)
    ):
        raise ValueError("Autorização diária ou credenciais divergentes.")
    if approved and (
        not approval
        or approval["scope_sha256"] != value["scope_sha256"]
        or not hmac.compare_digest(
            value.get("approval_signature", ""), _signature(settings, "approval", approval)
        )
    ):
        raise ValueError("Confirmação da autorização diária ausente.")
    return row, DailyPlan.model_validate(scope["plan"])


def prepare(session, settings, data):
    plan = DailyPlan.model_validate(data)
    if plan.operation_version is None:
        plan = plan.model_copy(update={"operation_version": "DAILY_V2"})
    destination = session.get(PublicationDestination, plan.destination_id)
    if (
        destination is None
        or destination.channel != DestinationChannel.TELEGRAM
        or destination.external_id not in settings.telegram_allowed_chat_ids
    ):
        raise ValueError("Destino diário fora da configuração autorizável.")
    scope = dict(
        plan=plan.model_dump(mode="json"),
        timezone=ZONE.key,
        chat_id=destination.external_id,
        template="PRO_V2" if plan.commercial_mode == "ANNOUNCEMENT_ONLY" else "VERIFIED_OFFER",
        operations=["productOfferV2", "Telegram"]
        + (["generateShortLink"] if plan.max_links != 0 else []),
        max_external_posts=(
            plan.max_queries + plan.max_links + plan.effective_message_limit()
            if plan.budgets_defined()
            else None
        ),
        window_seconds=600,
        freshness_seconds=600,
        retry=False,
        fallback=False,
        pagination=False,
        ranking="OPPORTUNITY_100_V1",
    )
    if plan.operation_version == "DAILY_V3":
        scope.update(
            effective_message_limit=plan.effective_message_limit(),
            telegram_reference="https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this",
            telegram_minimum_spacing_seconds=1,
            revalidate_pending_by_identity=True,
            close_at_window_end=True,
        )
    value = dict(
        scope=scope,
        scope_sha256=fingerprint(scope),
        signature=_signature(settings, "scope", scope),
        approval=None,
        state="PREPARED",
    )
    session.add(AppSetting(key=GRANT + str(plan.authorization_id), value=value))
    session.commit()
    return public_grant(value)


def public_grant(value):
    return {k: v for k, v in value.items() if k not in {"signature", "approval_signature"}}


def approve(
    session, settings, identifier, digest, actor, *, confirmed, acknowledge_unconfirmed_quotas, now
):
    row, plan = grant(session, settings, identifier, approved=False, lock=True)
    value = row.value
    if (
        confirmed is not True
        or digest != value["scope_sha256"]
        or value["state"] != "PREPARED"
        or plan.window(plan.end_date)[1] <= now
        or actor != plan.actor
    ):
        raise ValueError("Confirmação diária exige ator, escopo e validade exatos.")
    if not plan.budgets_defined():
        raise ValueError("Prepare outro escopo com os orçamentos Shopee e o intervalo definidos.")
    start, end = plan.window(plan.start_date)
    bounded_trial = (
        plan.start_date == plan.end_date
        and end - start <= timedelta(minutes=30)
        and max(plan.max_queries, plan.max_links, plan.effective_message_limit()) <= 3
    )
    if not plan.quota_reference and not (bounded_trial and acknowledge_unconfirmed_quotas is True):
        raise ValueError("Operação recorrente exige quotas confirmadas e referência documental.")
    approval = dict(
        scope_sha256=digest,
        actor=actor,
        approved_at=now.isoformat(),
        confirmed=True,
        acknowledge_unconfirmed_quotas=acknowledge_unconfirmed_quotas,
    )
    row.value = dict(
        value,
        state="APPROVED",
        approval=approval,
        approval_signature=_signature(settings, "approval", approval),
    )
    session.commit()
    return public_grant(row.value)


def require_child(session, settings, plan, now, *, scope_hash=None, running=False):
    """Match the signed parent, calendar and durable day claim before I/O."""
    from app.services.auto_pilot import AutoPilotPlan

    child = AutoPilotPlan.model_validate(plan)
    row, policy = grant(session, settings, child.daily_grant_id)
    day = child.not_before.astimezone(ZONE).date()
    expected = AutoPilotPlan.model_validate(policy.child(day))
    ledger = setting(session, DAY + day.isoformat())
    if (
        child != expected
        or not policy.start_date <= day <= policy.end_date
        or day.weekday() not in policy.days
        or not ledger
        or ledger.value["pilot_id"] != str(child.authorization_id)
        or ledger.value["grant_id"] != str(policy.authorization_id)
        or (scope_hash is not None and scope_hash != row.value["scope_sha256"])
    ):
        raise ValueError("Piloto diário fora do escopo autorizado.")
    destination = session.get(PublicationDestination, policy.destination_id)
    if (
        destination is None
        or destination.external_id != row.value["scope"]["chat_id"]
        or destination.external_id not in settings.telegram_allowed_chat_ids
    ):
        raise ValueError("Destino diário alterado.")
    if running:
        control = setting(session, CONTROL, lock=True)
        if (
            not child.not_before <= now < child.expires_at
            or not control
            or control.value.get("grant_id") != str(policy.authorization_id)
            or control.value.get("state") != "RUNNING"
            or control.value.get("stopped_on") == day.isoformat()
        ):
            raise ValueError("Operação diária pausada, parada ou fora do horário.")
    return row.value["scope_sha256"], policy


def control(session, settings, action, now, identifier=None):
    row = ensure_setting(session, CONTROL, {"state": "STOPPED"})
    value = dict(row.value)
    today = now.astimezone(ZONE).date().isoformat()
    if action == "start":
        if value.get("state") in {"RUNNING", "PAUSED"} and value.get("grant_id") != str(identifier):
            raise ValueError("Pare o escopo corrente antes de selecionar outro.")
        _, plan = grant(session, settings, identifier)
        if plan.window(plan.end_date)[1] <= now:
            raise ValueError("Autorização diária vencida.")
        if value.get("stopped_on") == today:
            raise ValueError("STOP vigente até o fim do dia.")
        if value.get("state") == "PAUSED":
            raise ValueError("Use RETOMAR para a operação pausada.")
        value.update(grant_id=str(identifier), state="RUNNING")
    elif action == "pause" and value.get("state") == "RUNNING":
        value["state"] = "PAUSED"
    elif action == "resume" and value.get("state") == "PAUSED":
        grant(session, settings, value["grant_id"])
        value["state"] = "RUNNING"
    elif action == "stop":
        value.update(state="STOPPED", stopped_on=today)
    else:
        raise ValueError("Ação incompatível com o estado diário.")
    row.value = value
    session.commit()
    return value
