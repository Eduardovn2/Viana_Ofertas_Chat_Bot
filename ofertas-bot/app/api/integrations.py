"""Local administrative capability checks; external requests always require explicit flags."""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, field_validator
from sqlalchemy import select

from app.api.products import Db, commit, get_product
from app.api.schemas import InputModel, PriceHistoryRead
from app.core.security import require_admin
from app.integrations.contracts import CapabilityStatus
from app.integrations.errors import IntegrationError
from app.models import (
    AffiliateLink,
    CouponEvidence,
    CouponEvidenceType,
    CouponValidationState,
    IntegrationEvent,
    Platform,
    PriceAccess,
)
from app.services.coupon_evidence import save_manual_evidence

router = APIRouter(
    prefix="/api/v1", tags=["Integrações oficiais"], dependencies=[Depends(require_admin)]
)


class ProductCheck(InputModel):
    product_id: int = Field(gt=0)


class DiscoverWrite(InputModel):
    query: str = Field(min_length=1, max_length=100)
    page: int = Field(default=1, ge=1, le=100)
    limit: int = Field(default=20, ge=1, le=50)
    category: str | None = Field(default=None, max_length=100)


class CouponEvidenceWrite(InputModel):
    code: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )
    type: CouponEvidenceType = CouponEvidenceType.UNKNOWN
    value: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    minimum_purchase: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    max_discount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    validation_state: CouponValidationState = CouponValidationState.UNVERIFIED
    price_access: PriceAccess = PriceAccess.UNKNOWN
    verified_at: datetime | None = None
    expires_at: datetime | None = None
    source_reference: str | None = Field(default=None, max_length=500)
    product_eligibility_confirmed: bool = False
    conditions_complete: bool = False

    @field_validator("verified_at", "expires_at")
    @classmethod
    def aware(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Data deve incluir fuso horário.")
        return value

    @field_validator("source_reference")
    @classmethod
    def reference(cls, value):
        if value is not None:
            from app.services.products import _AFFILIATE_HOSTS, _PRODUCT_HOSTS, _validate_url

            allowed = frozenset().union(*_PRODUCT_HOSTS.values(), *_AFFILIATE_HOSTS.values())
            parsed = _validate_url(value, allowed)
            if parsed.query or parsed.fragment:
                raise ValueError("Referência deve omitir parâmetros e fragmentos.")
        return value


def _failure(error: IntegrationError):
    status = {
        "CONNECTOR_DISABLED": 409,
        "UNSUPPORTED_CAPABILITY": 409,
        "AUTHORIZATION_DENIED": 403,
        "RATE_LIMITED": 429,
        "TIMEOUT": 504,
    }.get(error.code, 502)
    raise HTTPException(status, {"code": error.code, "message": error.message}) from None


def _capabilities(registry, platform):
    capabilities = [asdict(item) for item in registry.get(platform).capabilities()]
    if registry.is_blocked(platform):
        for item in capabilities:
            if item["name"] in {
                "fetch_product",
                "public_price",
                "price_conditions",
                "discover_offers",
            }:
                item.update(
                    status=CapabilityStatus.UNAVAILABLE_FOR_ACCOUNT,
                    reason="A API recusou a autorização; nova checagem explícita necessária.",
                )
    return capabilities


@router.get("/integrations/status")
def integration_status(request: Request, session: Db):
    registry = request.app.state.integrations
    result = []
    for platform in Platform:
        event = session.scalar(
            select(IntegrationEvent)
            .where(IntegrationEvent.platform == platform)
            .order_by(IntegrationEvent.id.desc())
            .limit(1)
        )
        result.append(
            {
                "platform": platform,
                "enabled": registry.enabled(platform),
                "state": "BLOCKED"
                if registry.is_blocked(platform)
                else "ENABLED"
                if registry.enabled(platform)
                else "DISABLED",
                "credentials_configured": registry.configured(platform),
                "last_query": None
                if event is None
                else {
                    "observed_at": event.observed_at,
                    "status": event.status,
                    "error_code": event.error_code,
                },
                "capabilities": _capabilities(registry, platform),
            }
        )
    return result


@router.get("/integrations/{platform}/capabilities")
def capabilities(platform: Platform, request: Request):
    return {
        "platform": platform,
        "capabilities": _capabilities(request.app.state.integrations, platform),
    }


def _refresh(product_id, request, session, *, check=False):
    from app.jobs.runtime import ProviderRateLimitedError

    product = get_product(session, product_id)
    if not product.monitoring_enabled:
        raise HTTPException(409, "Monitoramento desativado para este produto.")
    try:
        return request.app.state.monitoring.run_product(product_id, real=True, check=check)
    except IntegrationError as error:
        _failure(error)
    except ProviderRateLimitedError:
        raise HTTPException(429, "Intervalo da plataforma ainda não decorrido.") from None
    except (LookupError, ValueError) as error:
        raise HTTPException(422, str(error)) from None
    except RuntimeError:
        raise HTTPException(409, "Consulta ocupada ou limitada; tente mais tarde.") from None


@router.post("/products/{product_id}/refresh-real", response_model=PriceHistoryRead)
def refresh_real(product_id: int, request: Request, session: Db):
    return _refresh(product_id, request, session)


@router.post("/integrations/{platform}/check", response_model=PriceHistoryRead)
def check(platform: Platform, data: ProductCheck, request: Request, session: Db):
    product = get_product(session, data.product_id)
    if product.platform != platform:
        raise HTTPException(422, "Produto pertence a outra plataforma.")
    connector = request.app.state.integrations.get(platform)
    if hasattr(connector, "transport"):
        connector.transport.clear_cache()
    result = _refresh(product.id, request, session, check=True)
    request.app.state.integrations.unblock(platform)
    return result


@router.post("/integrations/{platform}/discover")
def discover(platform: Platform, data: DiscoverWrite, request: Request, session: Db):
    from app.services.marketplaces import ingest_discovery

    registry = request.app.state.integrations
    if registry.is_blocked(platform):
        raise HTTPException(403, "Conector pausado após recusa de autorização.")
    try:
        page = registry.get(platform).discover_offers(
            data.query, page=data.page, limit=data.limit, category=data.category
        )
    except IntegrationError as error:
        session.add(
            IntegrationEvent(
                platform=platform,
                operation="discover_offers",
                status="FAILED",
                error_code=error.code,
            )
        )
        commit(session)
        if error.code == "AUTHORIZATION_DENIED":
            registry.block(platform)
        _failure(error)
    from app.services.configuration import load_monitoring_settings

    try:
        ids = ingest_discovery(
            session, page, platform, load_monitoring_settings(session, request.app.state.settings)
        )
    except ValueError as error:
        session.rollback()
        raise HTTPException(422, str(error)) from None
    commit(session)
    return {
        "product_ids": ids,
        "page": page.page,
        "has_next": page.has_next,
        "is_simulated": False,
        "publication_mode": "dry-run",
    }


@router.post("/products/{product_id}/affiliate-link/generate")
def generate_affiliate_link(product_id: int, request: Request, session: Db):
    product = get_product(session, product_id)
    if product.affiliate_link is not None:
        raise HTTPException(
            409, "O produto já possui link cadastrado; preserve a origem existente."
        )
    registry = request.app.state.integrations
    try:
        if registry.is_blocked(product.platform):
            raise HTTPException(403, "Conector pausado após recusa de autorização.")
        result = registry.get(product.platform).generate_affiliate_link(product.original_url)
    except IntegrationError as error:
        _failure(error)
    if not result.account_verified:
        raise HTTPException(409, "Associação do link com a conta não comprovada.")
    link = AffiliateLink(product_id=product.id, url=result.url, source="SHOPEE_OFFICIAL_API")
    session.add(link)
    commit(session)
    return {"url": link.url, "source": link.source, "account_verified": True}


@router.post("/products/{product_id}/coupon-evidence", status_code=201)
def create_coupon_evidence(product_id: int, data: CouponEvidenceWrite, session: Db):
    try:
        evidence = save_manual_evidence(session, get_product(session, product_id), data)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    commit(session)
    return {
        "id": evidence.id,
        "validation_state": evidence.validation_state,
        "coupon_id": evidence.coupon_id,
        "source_type": evidence.source_type,
        "conditions": evidence.conditions,
    }


@router.get("/products/{product_id}/coupon-evidence")
def list_coupon_evidence(
    product_id: int,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    get_product(session, product_id)
    from app.api.monitoring import encoded

    return encoded(
        session.scalars(
            select(CouponEvidence)
            .where(CouponEvidence.product_id == product_id)
            .order_by(CouponEvidence.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
