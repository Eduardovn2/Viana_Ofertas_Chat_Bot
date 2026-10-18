import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.schemas import (
    AffiliateLinkRead,
    AffiliateLinkWrite,
    MonitoringUpdate,
    PriceHistoryRead,
    ProductCreate,
    ProductRead,
    ProductUpdate,
)
from app.core.security import require_admin
from app.db import get_session
from app.integrations.mock import SimulatedPriceProvider
from app.models import AffiliateLink, Product, ProductPriceHistory
from app.services.monitoring import collect_price
from app.services.products import normalize_product_identity, validate_affiliate_url

router = APIRouter(
    prefix="/api/v1/products", tags=["Produtos"], dependencies=[Depends(require_admin)]
)
Db = Annotated[Session, Depends(get_session)]
logger = logging.getLogger("ofertas_bot")


def get_product(session: Session, product_id: int) -> Product:
    product = session.scalar(
        select(Product)
        .where(Product.id == product_id)
        .options(selectinload(Product.affiliate_link))
    )
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Produto não encontrado")
    return product


def read_product(product: Product) -> ProductRead:
    return ProductRead(
        id=product.id,
        platform=product.platform,
        external_id=product.external_id,
        name=product.name,
        original_url=product.original_url,
        monitoring_enabled=product.monitoring_enabled,
        priority=product.priority,
        affiliate_pending=product.affiliate_link is None,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Registro duplicado ou conflito de integridade"
        ) from exc


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(data: ProductCreate, session: Db):
    try:
        external_id = normalize_product_identity(data.platform, data.original_url, data.external_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    product = Product(**{**data.model_dump(), "external_id": external_id})
    session.add(product)
    commit(session)
    logger.info("product_created", extra={"product_id": product.id})
    return read_product(get_product(session, product.id))


@router.get("", response_model=list[ProductRead])
def list_products(
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    products = session.scalars(
        select(Product)
        .options(selectinload(Product.affiliate_link))
        .order_by(Product.id)
        .offset(offset)
        .limit(limit)
    )
    return [read_product(product) for product in products]


@router.get("/{product_id}", response_model=ProductRead)
def retrieve_product(product_id: int, session: Db):
    return read_product(get_product(session, product_id))


@router.patch("/{product_id}", response_model=ProductRead)
def update_product(product_id: int, data: ProductUpdate, session: Db):
    product = get_product(session, product_id)
    changes = data.model_dump(exclude_unset=True)
    if "original_url" in changes:
        try:
            new_identity = normalize_product_identity(
                product.platform, changes["original_url"], product.external_id
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        if new_identity != product.external_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Para alterar a identidade do anúncio, cadastre outro produto",
            )
    for key, value in changes.items():
        setattr(product, key, value)
    commit(session)
    return read_product(product)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(product_id: int, session: Db):
    session.delete(get_product(session, product_id))
    commit(session)
    logger.info("product_deleted", extra={"product_id": product_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/{product_id}/monitoring", response_model=ProductRead)
def update_monitoring(product_id: int, data: MonitoringUpdate, session: Db):
    product = get_product(session, product_id)
    product.monitoring_enabled = data.enabled
    commit(session)
    return read_product(product)


@router.put("/{product_id}/affiliate-link", response_model=AffiliateLinkRead)
def put_affiliate_link(product_id: int, data: AffiliateLinkWrite, session: Db):
    product = get_product(session, product_id)
    try:
        validate_affiliate_url(product.platform, data.url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    link = product.affiliate_link
    if link is None:
        link = AffiliateLink(product_id=product.id, url=data.url, source="manual_official")
        session.add(link)
    else:
        link.url = data.url
    commit(session)
    logger.info("affiliate_link_saved", extra={"product_id": product_id})
    return link


@router.get("/{product_id}/affiliate-link", response_model=AffiliateLinkRead)
def retrieve_affiliate_link(product_id: int, session: Db):
    link = get_product(session, product_id).affiliate_link
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link de afiliado ainda não cadastrado")
    return link


@router.delete("/{product_id}/affiliate-link", status_code=status.HTTP_204_NO_CONTENT)
def delete_affiliate_link(product_id: int, session: Db):
    link = get_product(session, product_id).affiliate_link
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link de afiliado ainda não cadastrado")
    session.delete(link)
    commit(session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{product_id}/price-history", response_model=list[PriceHistoryRead])
def price_history(
    product_id: int,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    get_product(session, product_id)
    return session.scalars(
        select(ProductPriceHistory)
        .where(ProductPriceHistory.product_id == product_id)
        .order_by(ProductPriceHistory.observed_at.desc(), ProductPriceHistory.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()


@router.post(
    "/{product_id}/monitor", response_model=PriceHistoryRead, status_code=status.HTTP_201_CREATED
)
def monitor_product(product_id: int, request: Request, session: Db):
    product = get_product(session, product_id)
    if not product.monitoring_enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "O monitoramento deste produto está desativado"
        )
    provider = SimulatedPriceProvider(current_price=request.app.state.settings.mock_current_price)
    try:
        observation = collect_price(session, product, provider)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    commit(session)
    logger.info("simulated_price_recorded", extra={"product_id": product_id})
    return observation
