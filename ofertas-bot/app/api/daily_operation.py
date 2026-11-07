"""Loopback panel and authenticated daily controls."""

import hashlib
import hmac
import secrets
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import func, select

from app.core.security import require_admin
from app.daily_worker import HEALTH
from app.models import (
    AppSetting,
    Offer,
    Product,
    PublicationDestination,
    TelegramPublication,
    TelegramPublicationStatus,
)
from app.services.auto_pilot import PREFIX, AutoPilotService
from app.services.daily_operation import (
    CONTROL,
    DAY,
    GRANT,
    ZONE,
    DailyPlan,
    approve,
    control,
    prepare,
    public_grant,
    setting,
)
from app.services.daily_queue import QUEUE

router = APIRouter()
COOKIE = "vianinha_daily_session"
LOGIN = "daily_panel_login"
PANEL = Path(__file__).resolve().parents[1] / "static" / "daily.html"


def loopback(request: Request):
    hosts = {"127.0.0.1:8000", "localhost:8000", "127.0.0.1", "localhost"}
    host = request.headers.get("host")
    if (
        request.client is None
        or request.client.host not in {"127.0.0.1", "::1"}
        or host not in hosts
    ):
        raise HTTPException(403, "Painel disponível apenas no computador local.")
    if request.method == "POST" and request.headers.get("origin") not in {None, "http://" + host}:
        raise HTTPException(403, "Origem inválida.")


def _session_signature(request, value):
    key = request.app.state.settings.admin_api_key
    if key is None:
        raise HTTPException(503, "Administração local não configurada.")
    return hmac.new(
        key.get_secret_value().encode(), ("daily-panel:" + value).encode(), hashlib.sha256
    ).hexdigest()


def admin(request: Request):
    loopback(request)
    if key := request.headers.get("x-api-key"):
        require_admin(request, key)
        return
    if request.method == "POST" and (
        not request.headers.get("origin") or request.headers.get("x-daily-panel") != "1"
    ):
        raise HTTPException(403, "Comando fora do painel local.")
    token = request.cookies.get(COOKIE, "")
    expires, _, digest = token.partition(".")
    if (
        not expires.isascii()
        or not expires.isdecimal()
        or len(expires) > 12
        or int(expires) <= datetime.now(UTC).timestamp()
        or not hmac.compare_digest(digest, _session_signature(request, expires))
    ):
        raise HTTPException(401, "Sessão administrativa necessária.")


class Login(BaseModel):
    code: str = Field(min_length=16, max_length=100)


@router.post("/api/v1/daily/session")
def login(data: Login, request: Request, response: Response):
    loopback(request)
    if not request.headers.get("origin") or request.headers.get("x-daily-panel") != "1":
        raise HTTPException(403, "Abra o painel local para entrar.")
    now = datetime.now(UTC)
    with request.app.state.session_factory() as session:
        row = setting(session, LOGIN, lock=True)
        if (
            not row
            or row.value.get("used")
            or datetime.fromisoformat(row.value["expires_at"]) <= now
            or not secrets.compare_digest(
                row.value["digest"], hashlib.sha256(data.code.encode()).hexdigest()
            )
        ):
            raise HTTPException(401, "Código inválido ou vencido.")
        row.value = dict(row.value, used=True)
        session.commit()
    expires = str(int((now + timedelta(hours=2)).timestamp()))
    response.set_cookie(
        COOKIE,
        expires + "." + _session_signature(request, expires),
        max_age=7200,
        httponly=True,
        samesite="strict",
    )
    return {"authenticated": True}


@router.get("/daily", response_class=HTMLResponse)
def panel(request: Request):
    loopback(request)
    return HTMLResponse(
        PANEL.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        },
    )


@router.get("/api/v1/daily", dependencies=[Depends(admin)])
def status(request: Request):
    now = datetime.now(UTC)
    local = now.astimezone(ZONE)
    with request.app.state.session_factory() as session:
        stored = setting(session, CONTROL)
        current = stored.value if stored else {"state": "STOPPED"}
        health_row = setting(session, HEALTH)
        health = health_row.value if health_row else {}
        healthy = bool(
            health and (now - datetime.fromisoformat(health["at"])).total_seconds() < 120
        )
        day_row = setting(session, DAY + local.date().isoformat())
        day = day_row.value if day_row else {}
        pilot_row = setting(session, PREFIX + day["pilot_id"]) if day else None
        pilot = pilot_row.value if pilot_row else {}
        grants = [
            public_grant(row.value)
            for row in session.scalars(
                select(AppSetting)
                .where(AppSetting.key.startswith(GRANT))
                .order_by(AppSetting.id.desc())
                .limit(30)
            )
        ]
        active = next(
            (
                g
                for g in grants
                if g["scope"]["plan"]["authorization_id"] == current.get("grant_id")
            ),
            None,
        )
        plan = DailyPlan.model_validate(active["scope"]["plan"]) if active else None
        displayed = active or (grants[0] if grants else None)
        next_at = None
        if plan:
            for offset in range(8):
                day_date = local.date() + timedelta(days=offset)
                start, _ = plan.window(day_date)
                if (
                    plan.start_date <= day_date <= plan.end_date
                    and day_date.weekday() in plan.days
                    and start > local
                ):
                    next_at = start.isoformat()
                    break
        state = (
            current["state"]
            if current["state"] != "RUNNING"
            else health.get("state", "WAITING")
            if healthy
            else "ERROR"
        )
        used = dict(
            queries=len(pilot.get("queries", [])),
            links=len(pilot.get("daily_links", [])),
            messages=len(pilot.get("reservations", [])),
        )
        remaining = (
            {
                key: max(
                    0,
                    (
                        plan.effective_message_limit()
                        if key == "messages"
                        else getattr(plan, "max_" + key)
                    )
                    - count,
                )
                for key, count in used.items()
                if key == "messages" or getattr(plan, "max_" + key) is not None
            }
            if plan
            else {}
        )
        publication_limit = (
            plan.effective_message_limit()
            if plan
            else displayed["scope"].get(
                "effective_message_limit", displayed["scope"]["plan"]["max_messages"]
            )
            if displayed
            else None
        )
        exhausted = publication_limit is not None and used["messages"] >= publication_limit
        queued = {
            item["offer_id"]: item
            for item in pilot.get("queue", [])
            if item["state"] == "QUEUED" and not exhausted
        }
        live_offers = (
            session.scalars(
                select(Offer.id).where(Offer.id.in_(queued), Offer.expires_at > now)
            ).all()
            if queued
            else []
        )
        waiting = [
            dict(
                identity=queued[offer_id]["identity"],
                name=queued[offer_id]["quote"].get("name"),
                announced_price=queued[offer_id]["quote"].get("announced_price"),
            )
            for offer_id in live_offers
        ]
        backlog = None
        if displayed and displayed["scope"]["plan"].get("operation_version") == "DAILY_V3":
            prefix = QUEUE + displayed["scope"]["plan"]["authorization_id"] + ":"
            bucket = AppSetting.value["state"].as_string()
            backlog = dict(
                session.execute(
                    select(bucket, func.count())
                    .where(AppSetting.key.startswith(prefix))
                    .group_by(bucket)
                ).all()
            )
            waiting = [
                dict(
                    identity=row.value["identity"],
                    name=row.value["name"],
                    needs_revalidation=datetime.fromisoformat(row.value["observed_at"])
                    <= now - timedelta(seconds=600),
                )
                for row in session.scalars(
                    select(AppSetting)
                    .where(AppSetting.key.startswith(prefix), bucket == "PENDING")
                    .order_by(AppSetting.id)
                    .limit(50)
                )
            ]
        queries = pilot.get("queries", [])
        messages = pilot.get("reservations", [])
        found = {r["identity"] for r in pilot.get("findings", [])}
        query_at = None
        if state == "RUNNING" and pilot:
            planner = AutoPilotService(
                request.app.state.session_factory, request.app.state.settings
            )
            due = planner._next_query_at(pilot)
            query_at = max(now, due).astimezone(ZONE).isoformat() if due else None
        published_today = session.scalar(
            select(func.count())
            .select_from(TelegramPublication)
            .where(
                TelegramPublication.status == TelegramPublicationStatus.SENT,
                TelegramPublication.sent_at >= datetime.combine(local.date(), time.min, ZONE),
                TelegramPublication.sent_at
                < datetime.combine(local.date() + timedelta(days=1), time.min, ZONE),
            )
        )
        return dict(
            state=state,
            now=local.isoformat(),
            next_at=next_at,
            next_query_at=query_at,
            control=current,
            grants=grants,
            active=active,
            worker=dict(healthy=healthy, **health),
            database="OK",
            shopee=queries[-1]["state"] if queries else "SEM_TESTE_NESTE_DIA",
            telegram=messages[-1]["state"] if messages else "SEM_ENVIO_NESTE_DIA",
            used=used,
            remaining=remaining,
            products_found=len(found),
            classified=len(pilot.get("opportunities", {}).get("analysis_queue", [])),
            published=sum(r["state"] == "SENT" for r in messages),
            published_today=published_today,
            publication_limit=publication_limit,
            message_budget_exhausted=exhausted,
            waiting_to_send=waiting,
            backlog=backlog,
            sent_last_hour=session.scalar(
                select(func.count())
                .select_from(TelegramPublication)
                .where(
                    TelegramPublication.status == TelegramPublicationStatus.SENT,
                    TelegramPublication.sent_at > now - timedelta(hours=1),
                )
            ),
            recent_blocks=[
                dict(identity=r["identity"], reason=r.get("reason"))
                for r in pilot.get("findings", [])
                if r["status"] != "QUEUED"
            ][-10:],
            link_attempts=[
                dict(identity=r["identity"], state=r["state"], reason=r.get("reason"))
                for r in pilot.get("daily_links", [])
            ][-10:],
        )


@router.get("/api/v1/daily/history", dependencies=[Depends(admin)])
def history(request: Request, day: date | None = None, product: str = "", state: str = ""):
    query = (
        select(TelegramPublication, Product, PublicationDestination)
        .join(Product, Product.id == TelegramPublication.product_id)
        .join(
            PublicationDestination, PublicationDestination.id == TelegramPublication.destination_id
        )
    )
    when = TelegramPublication.sent_at
    if day:
        when = func.coalesce(when, TelegramPublication.created_at)
        query = query.where(
            when >= datetime.combine(day, time.min, ZONE),
            when < datetime.combine(day + timedelta(days=1), time.min, ZONE),
        )
    if product:
        query = query.where(Product.name.ilike("%" + product[:100] + "%"))
    if state:
        try:
            query = query.where(TelegramPublication.status == TelegramPublicationStatus(state))
        except ValueError:
            raise HTTPException(422, "Estado de publicação inválido.") from None
    with request.app.state.session_factory() as session:
        rows = session.execute(query.order_by(TelegramPublication.id.desc()).limit(100))
        return [
            dict(
                product=p.name,
                identity=p.external_id,
                destination=d.name,
                state=r.status.value,
                message_id=r.message_id,
                at=(r.sent_at or r.created_at).astimezone(ZONE).isoformat(),
                error=r.last_error_code,
            )
            for r, p, d in rows
        ]


class Approval(BaseModel):
    identifier: UUID
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str
    confirmed: StrictBool
    acknowledge_unconfirmed_quotas: StrictBool = False


class Command(BaseModel):
    action: Literal["start", "pause", "resume", "stop"]
    identifier: UUID | None = None


@router.post("/api/v1/daily/prepare", dependencies=[Depends(admin)])
def prepare_route(data: DailyPlan, request: Request):
    with request.app.state.session_factory() as session:
        try:
            return prepare(session, request.app.state.settings, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None


@router.post("/api/v1/daily/approve", dependencies=[Depends(admin)])
def approve_route(data: Approval, request: Request):
    with request.app.state.session_factory() as session:
        try:
            return approve(
                session,
                request.app.state.settings,
                data.identifier,
                data.scope_sha256,
                data.actor,
                confirmed=data.confirmed,
                acknowledge_unconfirmed_quotas=data.acknowledge_unconfirmed_quotas,
                now=datetime.now(UTC),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None


@router.post("/api/v1/daily/control", dependencies=[Depends(admin)])
def control_route(data: Command, request: Request):
    with request.app.state.session_factory() as session:
        try:
            return control(
                session, request.app.state.settings, data.action, datetime.now(UTC), data.identifier
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
