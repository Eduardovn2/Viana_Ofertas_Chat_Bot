"""Local administrative manual-offer workflow, separate from explicit dispatch."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select

from app.api.products import Db, commit
from app.api.schemas import InputModel
from app.core.security import require_admin
from app.integrations.errors import IntegrationError
from app.models import (
    Availability,
    ManualOfferDetails,
    Offer,
    OfferStatus,
    PriceAccess,
    ProductCategory,
    TelegramPublication,
    TelegramPublicationStatus,
)
from app.services.manual_offers import create_manual_offer
from app.services.official_offer_import import OfficialExportInput, process_export
from app.services.product_documentation import DocumentedIdentity
from app.services.shopee_commercial_handoff import CandidateContext
from app.services.telegram_publication import TelegramPublicationService

router = APIRouter(
    prefix="/api/v1", tags=["Ofertas manuais"], dependencies=[Depends(require_admin)]
)
Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Identifier = Annotated[int, Field(gt=0, strict=True)]


class ManualOfferCreate(InputModel):
    product_id: Identifier
    current_price: Money
    actor: str = Field(min_length=1, max_length=200)
    verified: StrictBool
    availability: Availability
    price_access: PriceAccess
    previous_price: Money | None = None
    coupon_id: Identifier | None = None
    installment_text: str | None = Field(default=None, min_length=1, max_length=500)
    image_url: str | None = Field(default=None, min_length=1, max_length=2048)
    verified_at: datetime | None = None
    expires_at: datetime | None = None
    multistore_context: CandidateContext | None = None
    documented_identity: DocumentedIdentity | None = None


class ManualOfferRead(BaseModel):
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
    status: OfferStatus
    expires_at: datetime | None
    verified_by: str
    verified_at: datetime
    image_url: str | None
    category_ids: list[int]


@router.post("/manual-offers/official-exports/preview")
def preview_official_export(data: OfficialExportInput, session: Db, request: Request):
    try:
        return process_export(session, request.app.state.settings, data)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/manual-offers/official-exports/import")
def import_official_export(data: OfficialExportInput, session: Db, request: Request):
    try:
        result = process_export(session, request.app.state.settings, data, persist=True)
        commit(session)
        return result
    except ValueError as error:
        session.rollback()
        raise HTTPException(422, str(error)) from None


class PreviewRequest(InputModel):
    destination_id: Identifier
    include_image: StrictBool = True


class ApprovalRequest(InputModel):
    preview_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=200)
    confirmed: StrictBool

    @field_validator("confirmed")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("A aprovação exige confirmação explícita da prévia por destino.")
        return value


class MessageRead(BaseModel):
    text: str
    photo_url: str | None = None
    parse_mode: str = "HTML"
    image_omitted: bool = False


class PublicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    offer_id: int
    product_id: int
    destination_id: int
    status: TelegramPublicationStatus
    preview_hash: str
    message: MessageRead
    approved_by: str | None
    approved_at: datetime | None
    last_error_code: str | None
    active_slot: int | None
    attempt_number: int
    message_id: str | None
    claimed_at: datetime | None
    retry_at: datetime | None
    sent_at: datetime | None
    resolved_by: str | None
    resolved_at: datetime | None
    resolution: str | None
    resolution_evidence: str | None


def _raise(error):
    if error.code in {"MANUAL_OFFER_NOT_FOUND", "MANUAL_PRODUCT_NOT_FOUND", "OUTBOX_NOT_FOUND"}:
        status = 404
    elif error.code in {"MANUAL_INPUT_INVALID", "OUTBOX_ACTOR_INVALID"}:
        status = 422
    else:
        status = 409
    raise HTTPException(status, {"code": error.code, "message": error.message}) from None


def _offer_read(session, offer, details):
    fields = ManualOfferRead.model_fields.keys() - {
        "verified_by",
        "verified_at",
        "image_url",
        "category_ids",
    }
    return ManualOfferRead(
        **{field: getattr(offer, field) for field in fields},
        verified_by=details.verified_by,
        verified_at=details.verified_at,
        image_url=details.image_url,
        category_ids=list(
            session.scalars(
                select(ProductCategory.category_id)
                .where(
                    ProductCategory.product_id == offer.product_id,
                )
                .order_by(ProductCategory.category_id)
            )
        ),
    )


def _publication_read(entry):
    fields = PublicationRead.model_fields.keys() - {"message"}
    return PublicationRead(
        **{field: getattr(entry, field) for field in fields},
        message=entry.preview_payload["message"],
    )


@router.post("/manual-offers", response_model=ManualOfferRead, status_code=201)
def register_manual_offer(data: ManualOfferCreate, request: Request, session: Db):
    try:
        bundle = create_manual_offer(session, request.app.state.settings, **data.model_dump())
        commit(session)
    except IntegrationError as error:
        session.rollback()
        _raise(error)
    return _offer_read(session, bundle.offer, bundle.details)


@router.get("/manual-offers", response_model=list[ManualOfferRead])
def list_manual_offers(
    session: Db,
    product_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = select(Offer, ManualOfferDetails).join(
        ManualOfferDetails, ManualOfferDetails.offer_id == Offer.id
    )
    if product_id is not None:
        query = query.where(Offer.product_id == product_id)
    return [
        _offer_read(session, offer, details)
        for offer, details in session.execute(
            query.order_by(Offer.id.desc()).offset(offset).limit(limit),
        )
    ]


@router.get("/manual-offers/{offer_id}", response_model=ManualOfferRead)
def retrieve_manual_offer(offer_id: int, session: Db):
    row = session.execute(
        select(Offer, ManualOfferDetails)
        .join(
            ManualOfferDetails,
            ManualOfferDetails.offer_id == Offer.id,
        )
        .where(Offer.id == offer_id)
    ).first()
    if row is None:
        raise HTTPException(404, "Oferta manual não encontrada")
    return _offer_read(session, *row)


@router.post("/manual-offers/{offer_id}/review-message")
def review_manual_offer_message(offer_id: int, data: PreviewRequest, request: Request, session: Db):
    try:
        return TelegramPublicationService(request.app.state.settings).review_message(
            session,
            offer_id,
            data.destination_id,
            data.include_image,
        )
    except IntegrationError as error:
        _raise(error)


@router.post("/manual-offers/{offer_id}/preview", response_model=PublicationRead)
def preview_manual_offer(offer_id: int, data: PreviewRequest, request: Request, session: Db):
    try:
        entry = TelegramPublicationService(request.app.state.settings).preview(
            session,
            offer_id,
            data.destination_id,
            data.include_image,
        )
        commit(session)
    except IntegrationError as error:
        session.rollback()
        _raise(error)
    return _publication_read(entry)


@router.get("/telegram/publications", response_model=list[PublicationRead])
def list_publications(
    session: Db,
    destination_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = select(TelegramPublication)
    if destination_id is not None:
        query = query.where(TelegramPublication.destination_id == destination_id)
    return [
        _publication_read(entry)
        for entry in session.scalars(
            query.order_by(TelegramPublication.id.desc()).offset(offset).limit(limit),
        )
    ]


@router.post("/telegram/publications/{publication_id}/approve", response_model=PublicationRead)
def approve_manual_offer(publication_id: int, data: ApprovalRequest, request: Request):
    try:
        entry = TelegramPublicationService(request.app.state.settings).approve(
            request.app.state.session_factory,
            publication_id,
            data.preview_hash,
            data.actor,
        )
    except IntegrationError as error:
        _raise(error)
    return _publication_read(entry)
