"""Administrative monitoring controls and simulated opportunities."""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from app.api.products import Db, commit, get_product
from app.core.security import require_admin
from app.models import (
    CategoryMode,
    DestinationChannel,
    MonitoringJob,
    Offer,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationQueueItem,
    QueueStatus,
)
from app.services.configuration import (
    MonitoringConfiguration,
    load_monitoring_settings,
    read_monitoring_configuration,
    save_monitoring_configuration,
)

router = APIRouter(
    prefix="/api/v1", tags=["Monitoramento e ofertas"], dependencies=[Depends(require_admin)]
)


class ConfigurationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduler_enabled: bool | None = None
    priority_interval_seconds: int | None = None
    general_interval_seconds: int | None = None
    provider_min_interval_seconds: Decimal | None = None
    price_max_age_seconds: int | None = None
    score_price_drop: Decimal | None = None
    score_confirmed_coupon: Decimal | None = None
    score_priority: Decimal | None = None
    score_discount_percent: Decimal | None = None
    score_availability: Decimal | None = None
    mock_current_price: Decimal | None = None
    mock_previous_price: Decimal | None = None
    mock_availability: str | None = None
    mock_price_access: str | None = None

    @model_validator(mode="after")
    def no_nulls(self):
        if any(
            getattr(self, key) is None for key in self.model_fields_set - {"mock_previous_price"}
        ):
            raise ValueError("Somente mock_previous_price pode ser nulo")
        return self


class OfferRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    price_history_id: int
    coupon_id: int | None
    current_price: Decimal
    previous_price: Decimal | None
    historical_price: Decimal | None
    discount_amount: Decimal | None
    discount_percentage: Decimal | None
    final_price: Decimal | None
    score: Decimal
    status: str
    expires_at: datetime | None
    source_type: str = "UNKNOWN"
    source_reference: str | None = None
    observed_at: datetime | None = None
    is_simulated: bool = True
    variant_id: str = ""
    affiliate_pending: bool = True
    blocked_reasons: list[str] = Field(default_factory=list)
    publication_mode: str = "dry-run"


class DestinationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    external_id: str = Field(min_length=1, max_length=200)
    manual_approval_required: bool = False
    category_mode: CategoryMode = CategoryMode.UNCONFIGURED


class DestinationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    manual_approval_required: bool | None = None

    @model_validator(mode="after")
    def no_nulls(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Campos enviados não podem ser nulos")
        return self


class DestinationRead(DestinationCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    channel: DestinationChannel
    enabled: bool


def encoded(value):
    return JSONResponse(jsonable_encoder(value, custom_encoder={Decimal: str}))


@router.get("/monitoring/status")
def monitoring_status(request: Request, session: Db):
    configuration = read_monitoring_configuration(session, request.app.state.settings)
    runtime_status = request.app.state.monitoring.status()
    return {
        **runtime_status,
        "enabled": configuration.scheduler_enabled,
        "paused": not configuration.scheduler_enabled,
        "running": runtime_status["running"] and configuration.scheduler_enabled,
        "configuration": configuration,
    }


@router.get("/monitoring/config", response_model=MonitoringConfiguration)
def monitoring_configuration(request: Request, session: Db):
    return read_monitoring_configuration(session, request.app.state.settings)


@router.patch("/monitoring/config", response_model=MonitoringConfiguration)
def patch_configuration(data: ConfigurationPatch, request: Request, session: Db):
    try:
        configuration = save_monitoring_configuration(
            session, request.app.state.settings, data.model_dump(exclude_unset=True)
        )
    except ValidationError as exc:
        raise HTTPException(
            422, "Configuração inválida: confira intervalos, pontuações e preços"
        ) from exc
    commit(session)
    if configuration.scheduler_enabled:
        request.app.state.monitoring.resume()
    else:
        request.app.state.monitoring.pause()
    return configuration


@router.post("/monitoring/pause", response_model=MonitoringConfiguration)
def pause_monitoring(request: Request, session: Db):
    return patch_configuration(ConfigurationPatch(scheduler_enabled=False), request, session)


@router.post("/monitoring/resume", response_model=MonitoringConfiguration)
def resume_monitoring(request: Request, session: Db):
    return patch_configuration(ConfigurationPatch(scheduler_enabled=True), request, session)


@router.get("/monitoring/jobs")
def list_monitoring_jobs(session: Db, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    return encoded(
        session.scalars(select(MonitoringJob).order_by(MonitoringJob.product_id).limit(limit)).all()
    )


@router.get("/offers", response_model=list[OfferRead])
def list_offers(
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from app.services.offers import has_own_affiliate_link

    rows = session.execute(
        select(Offer, ProductPriceHistory, Product)
        .join(ProductPriceHistory, ProductPriceHistory.id == Offer.price_history_id)
        .join(Product, Product.id == Offer.product_id)
        .order_by(Offer.score.desc(), Offer.id.desc())
        .offset(offset)
        .limit(limit)
    )
    result = []
    for offer, history, product in rows:
        pending = not has_own_affiliate_link(product, require_verified=not history.is_simulated)
        result.append(
            {
                **{column.name: getattr(offer, column.name) for column in Offer.__table__.columns},
                "source_type": history.source_type,
                "source_reference": history.source_reference,
                "observed_at": history.observed_at,
                "is_simulated": history.is_simulated,
                "variant_id": product.variant_id,
                "affiliate_pending": pending,
                "blocked_reasons": ["AFFILIATE_PENDING"] if pending else [],
            }
        )
    return result


@router.get("/queue")
def list_queue(
    session: Db,
    state: QueueStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = (
        select(PublicationQueueItem, Offer.score, ProductPriceHistory, Product.variant_id)
        .join(Offer, Offer.id == PublicationQueueItem.offer_id)
        .join(ProductPriceHistory, ProductPriceHistory.id == Offer.price_history_id)
        .join(Product, Product.id == PublicationQueueItem.product_id)
    )
    if state is not None:
        query = query.where(PublicationQueueItem.status == state)
    rows = session.execute(
        query.order_by(Offer.score.desc(), PublicationQueueItem.created_at, PublicationQueueItem.id)
        .offset(offset)
        .limit(limit)
    )
    return encoded(
        [
            {
                **{
                    column.name: getattr(item, column.name)
                    for column in PublicationQueueItem.__table__.columns
                },
                "score": score,
                "source_type": history.source_type,
                "source_reference": history.source_reference,
                "source_observed_at": history.observed_at,
                "source_is_simulated": history.is_simulated,
                "variant_id": variant_id,
                "publication_mode": "dry-run",
            }
            for item, score, history, variant_id in rows
        ]
    )


@router.post("/queue/{item_id}/simulate")
def simulate_publication(item_id: int, request: Request, session: Db):
    from app.services.leases import acquire_lease, release_lease, renew_lease
    from app.services.publication_queue import SimulatedQueueService

    item = session.get(PublicationQueueItem, item_id)
    if item is None:
        raise HTTPException(404, "Item de fila não encontrado")
    settings = load_monitoring_settings(session, request.app.state.settings)
    factory = request.app.state.session_factory
    lease_name = f"monitor:product:{item.product_id}"
    owner = acquire_lease(factory, lease_name, settings.monitoring_lease_seconds)
    if owner is None:
        raise HTTPException(409, "Produto já está sendo monitorado ou validado")
    try:
        result = SimulatedQueueService(settings).publish(session, item_id)
        if not renew_lease(session, lease_name, owner, settings.monitoring_lease_seconds):
            session.rollback()
            raise HTTPException(409, "Reserva de validação expirada; tente novamente")
        commit(session)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        session.rollback()
        raise
    finally:
        release_lease(factory, lease_name, owner)
    return encoded(
        {
            column.name: getattr(result, column.name)
            for column in PublicationQueueItem.__table__.columns
        }
    )


@router.get("/destinations", response_model=list[DestinationRead])
def list_destinations(session: Db):
    return session.scalars(
        select(PublicationDestination)
        .where(PublicationDestination.channel == DestinationChannel.SIMULATED)
        .order_by(PublicationDestination.id)
    ).all()


@router.post("/destinations", response_model=DestinationRead, status_code=201)
def create_destination(data: DestinationCreate, session: Db):
    destination = PublicationDestination(
        **data.model_dump(), channel=DestinationChannel.SIMULATED, enabled=True
    )
    session.add(destination)
    commit(session)
    return destination


@router.patch("/destinations/{destination_id}", response_model=DestinationRead)
def update_destination(destination_id: int, data: DestinationUpdate, session: Db):
    from app.services.category_routing import invalidate_ineligible_queue

    destination = session.get(PublicationDestination, destination_id)
    if destination is None:
        raise HTTPException(404, "Destino não encontrado")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(destination, key, value)
    session.flush()
    invalidate_ineligible_queue(session, destination_id=destination.id)
    commit(session)
    return destination


@router.get("/products/{product_id}/variations")
def product_variations(
    product_id: int, request: Request, session: Db, limit: Annotated[int, Query(ge=1, le=100)] = 50
):
    from app.models import ProductPriceHistory
    from app.services.comparison import PriceComparisonService

    get_product(session, product_id)
    settings = load_monitoring_settings(session, request.app.state.settings)
    observations = session.scalars(
        select(ProductPriceHistory)
        .where(ProductPriceHistory.product_id == product_id)
        .order_by(ProductPriceHistory.observed_at.desc(), ProductPriceHistory.id.desc())
        .limit(limit)
    ).all()
    return encoded(
        [
            {
                "history_id": row.id,
                "observed_at": row.observed_at,
                "store_previous_price": row.previous_price,
                **asdict(
                    PriceComparisonService().compare(
                        session,
                        row,
                        now=row.observed_at,
                        max_age_seconds=settings.price_max_age_seconds,
                    )
                ),
            }
            for row in observations
        ]
    )
