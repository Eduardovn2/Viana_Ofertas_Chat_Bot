"""Explicit local administration; two independent flags guard every dispatch."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.manual_offers import PublicationRead, _publication_read, _raise
from app.api.products import Db, commit
from app.api.schemas import InputModel
from app.core.security import require_admin
from app.integrations.errors import IntegrationError
from app.models import (
    CategoryMode,
    DestinationCategory,
    DestinationChannel,
    PublicationDestination,
    PublicationLog,
    TelegramPublication,
)
from app.services.telegram_destinations import (
    configure_destination,
    destination_or_error,
    prepare_test_group,
)

router = APIRouter(
    prefix="/api/v1/telegram", tags=["Telegram"], dependencies=[Depends(require_admin)]
)


class DestinationCreate(InputModel):
    name: str = Field(min_length=1, max_length=200)
    external_id: str = Field(min_length=2, max_length=17)
    enabled: StrictBool = False
    manual_approval_required: StrictBool = True
    category_mode: CategoryMode = CategoryMode.UNCONFIGURED
    category_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(
        default_factory=list, max_length=100
    )
    min_interval_seconds: int = Field(default=60, ge=1, le=86400, strict=True)
    max_messages_per_hour: int = Field(default=20, ge=1, le=1000, strict=True)


class DestinationPatch(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: StrictBool | None = None
    manual_approval_required: StrictBool | None = None
    category_mode: CategoryMode | None = None
    category_ids: list[Annotated[int, Field(gt=0, strict=True)]] | None = Field(
        default=None, max_length=100
    )
    min_interval_seconds: int | None = Field(default=None, ge=1, le=86400, strict=True)
    max_messages_per_hour: int | None = Field(default=None, ge=1, le=1000, strict=True)

    @model_validator(mode="after")
    def no_nulls(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Campos enviados não podem ser nulos.")
        return self


class DestinationRead(BaseModel):
    id: int
    name: str
    external_id: str
    channel: DestinationChannel
    enabled: bool
    manual_approval_required: bool
    category_mode: CategoryMode
    category_ids: list[int]
    min_interval_seconds: int
    max_messages_per_hour: int
    chat_allowed: bool


class Confirmed(InputModel):
    confirmed: StrictBool

    @field_validator("confirmed")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("Confirmação explícita obrigatória.")
        return value


class DispatchRequest(Confirmed):
    preview_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResolutionRequest(Confirmed):
    outcome: Literal["SENT", "NOT_SENT"]
    actor: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=1, max_length=2000)
    message_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")


class LogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    attempt_number: int
    message_id: str | None
    message_text: str | None
    error_message: str | None
    sent_at: datetime | None
    created_at: datetime


def read_destination(session, destination, settings):
    fields = DestinationRead.model_fields.keys() - {"category_ids", "chat_allowed"}
    return DestinationRead(
        **{key: getattr(destination, key) for key in fields},
        category_ids=list(
            session.scalars(
                select(DestinationCategory.category_id)
                .where(DestinationCategory.destination_id == destination.id)
                .order_by(DestinationCategory.category_id)
            )
        ),
        chat_allowed=destination.external_id in settings.telegram_allowed_chat_ids,
    )


def _save(session, action):
    try:
        result = action()
        commit(session)
        return result
    except IntegrationError as error:
        session.rollback()
        if error.code == "DESTINATION_NOT_FOUND":
            raise HTTPException(404, error.message) from None
        _raise(error)
    except ValueError as error:
        session.rollback()
        raise HTTPException(422, str(error)) from None
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            409, "Grupo Telegram já cadastrado ou configuração em conflito."
        ) from None


@router.get("/status")
def status(request: Request):
    settings = request.app.state.settings
    return {
        "publisher_enabled": settings.telegram_publisher_enabled,
        "real_sends_authorized": settings.telegram_real_sends_authorized,
        "token_configured": settings.telegram_bot_token is not None,
        "automatic_publication": False,
    }


@router.post("/destinations", response_model=DestinationRead, status_code=201)
def create_destination(data: DestinationCreate, request: Request, session: Db):
    destination = _save(session, lambda: configure_destination(session, **data.model_dump()))
    return read_destination(session, destination, request.app.state.settings)


@router.post("/destinations/prepare-test-group", response_model=DestinationRead)
def prepare_group(request: Request, session: Db):
    destination = _save(session, lambda: prepare_test_group(session))
    return read_destination(session, destination, request.app.state.settings)


@router.get("/destinations", response_model=list[DestinationRead])
def list_destinations(
    request: Request,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return [
        read_destination(session, row, request.app.state.settings)
        for row in session.scalars(
            select(PublicationDestination)
            .where(PublicationDestination.channel == DestinationChannel.TELEGRAM)
            .order_by(PublicationDestination.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.get("/destinations/{destination_id}", response_model=DestinationRead)
def get_destination(destination_id: int, request: Request, session: Db):
    destination = _save(session, lambda: destination_or_error(session, destination_id))
    return read_destination(session, destination, request.app.state.settings)


@router.patch("/destinations/{destination_id}", response_model=DestinationRead)
def patch_destination(destination_id: int, data: DestinationPatch, request: Request, session: Db):
    destination = _save(
        session,
        lambda: configure_destination(
            session,
            destination=destination_or_error(session, destination_id),
            **data.model_dump(exclude_unset=True),
        ),
    )
    return read_destination(session, destination, request.app.state.settings)


@router.get("/publications/{entry_id}", response_model=PublicationRead)
def get_publication(entry_id: int, session: Db):
    entry = session.get(TelegramPublication, entry_id)
    if entry is None:
        raise HTTPException(404, "Publicação não encontrada.")
    return _publication_read(entry)


@router.get("/publications/{entry_id}/logs", response_model=list[LogRead])
def logs(entry_id: int, session: Db):
    entry = session.get(TelegramPublication, entry_id)
    if entry is None:
        raise HTTPException(404, "Publicação não encontrada.")
    return session.scalars(
        select(PublicationLog)
        .where(
            PublicationLog.offer_id == entry.offer_id,
            PublicationLog.destination_id == entry.destination_id,
            PublicationLog.attempt_number == entry.attempt_number,
        )
        .order_by(PublicationLog.id)
    ).all()


@router.post("/publications/{entry_id}/dispatch", response_model=PublicationRead)
def dispatch(entry_id: int, data: DispatchRequest, request: Request):
    try:
        return _publication_read(
            request.app.state.telegram_publications.publish(
                request.app.state.session_factory, entry_id, data.preview_hash
            )
        )
    except IntegrationError as error:
        _raise(error)


@router.post("/publications/{entry_id}/recover", response_model=PublicationRead)
def recover(entry_id: int, request: Request):
    try:
        return _publication_read(
            request.app.state.telegram_publications.recover(
                request.app.state.session_factory, entry_id
            )
        )
    except IntegrationError as error:
        _raise(error)


@router.post("/publications/{entry_id}/resolve", response_model=PublicationRead)
def resolve(entry_id: int, data: ResolutionRequest, request: Request):
    try:
        return _publication_read(
            request.app.state.telegram_publications.resolve(
                request.app.state.session_factory,
                entry_id,
                data.outcome,
                data.actor,
                data.evidence,
                data.message_id,
            )
        )
    except IntegrationError as error:
        _raise(error)
