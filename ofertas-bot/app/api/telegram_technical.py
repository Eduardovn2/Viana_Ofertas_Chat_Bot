"""Local authenticated, explicit technical preview/approval/one-shot dispatch."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.manual_offers import ApprovalRequest, MessageRead, _raise
from app.api.products import Db
from app.api.schemas import InputModel
from app.api.telegram import DispatchRequest, _save
from app.core.security import require_admin
from app.integrations.errors import IntegrationError
from app.models import (
    TelegramPublicationStatus,
    TelegramTechnicalAudit,
    TelegramTechnicalPublication,
)

router = APIRouter(
    prefix="/api/v1/telegram/technical-publications",
    tags=["Telegram técnico"],
    dependencies=[Depends(require_admin)],
)
Identifier = Annotated[int, Field(gt=0, strict=True)]


class TechnicalPreview(InputModel):
    destination_id: Identifier
    text: str = Field(min_length=1, max_length=4096)
    actor: str = Field(min_length=1, max_length=200)


class TechnicalApproval(ApprovalRequest):
    destination_id: Identifier


class TechnicalDispatch(DispatchRequest):
    destination_id: Identifier


class TechnicalRead(BaseModel):
    kind: Literal["ADMINISTRATIVE_TECHNICAL"] = "ADMINISTRATIVE_TECHNICAL"
    id: int
    destination_id: int
    chat_id: str
    text: str
    content_hash: str
    preview_hash: str
    message: MessageRead
    status: TelegramPublicationStatus
    created_by: str
    approved_by: str | None
    approved_at: datetime | None
    attempt_number: int
    claimed_at: datetime | None
    sent_at: datetime | None
    message_id: str | None
    last_error_code: str | None


class AuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    publication_id: int
    event: str
    actor: str | None
    preview_hash: str
    error_code: str | None
    created_at: datetime


def _read(entry):
    fields = TechnicalRead.model_fields.keys() - {"message", "kind", "chat_id"}
    return TechnicalRead(
        **{key: getattr(entry, key) for key in fields},
        chat_id=entry.preview_payload["chat_id"],
        message=entry.preview_payload["message"],
    )


def _get(session, entry_id):
    entry = session.get(TelegramTechnicalPublication, entry_id)
    if entry is None:
        raise HTTPException(404, "Publicação técnica não encontrada.")
    return entry


@router.post("/preview", response_model=TechnicalRead, status_code=201)
def preview(data: TechnicalPreview, request: Request, session: Db):
    entry = _save(
        session, lambda: request.app.state.telegram_technical.preview(session, **data.model_dump())
    )
    return _read(entry)


@router.get("", response_model=list[TechnicalRead])
def list_technical(
    session: Db,
    destination_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = select(TelegramTechnicalPublication)
    if destination_id is not None:
        query = query.where(TelegramTechnicalPublication.destination_id == destination_id)
    return [
        _read(entry)
        for entry in session.scalars(
            query.order_by(TelegramTechnicalPublication.id.desc()).offset(offset).limit(limit)
        )
    ]


@router.get("/{entry_id}", response_model=TechnicalRead)
def get_technical(entry_id: int, session: Db):
    return _read(_get(session, entry_id))


@router.get("/{entry_id}/audit", response_model=list[AuditRead])
def audit(entry_id: int, session: Db):
    _get(session, entry_id)
    return session.scalars(
        select(TelegramTechnicalAudit)
        .where(TelegramTechnicalAudit.publication_id == entry_id)
        .order_by(TelegramTechnicalAudit.id)
    ).all()


@router.post("/{entry_id}/approve", response_model=TechnicalRead)
def approve(entry_id: int, data: TechnicalApproval, request: Request):
    try:
        return _read(
            request.app.state.telegram_technical.approve(
                request.app.state.session_factory,
                entry_id,
                data.destination_id,
                data.preview_hash,
                data.actor,
            )
        )
    except IntegrationError as error:
        _raise(error)


@router.post("/{entry_id}/dispatch", response_model=TechnicalRead)
def dispatch(entry_id: int, data: TechnicalDispatch, request: Request):
    try:
        return _read(
            request.app.state.telegram_technical.publish(
                request.app.state.session_factory, entry_id, data.destination_id, data.preview_hash
            )
        )
    except IntegrationError as error:
        _raise(error)


@router.post("/{entry_id}/recover", response_model=TechnicalRead)
def recover(entry_id: int, request: Request):
    try:
        return _read(
            request.app.state.telegram_technical.recover(
                request.app.state.session_factory, entry_id
            )
        )
    except IntegrationError as error:
        _raise(error)
