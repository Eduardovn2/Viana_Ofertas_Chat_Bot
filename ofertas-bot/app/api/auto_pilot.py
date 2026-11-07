"""AUTO_PILOT administration. Execution remains isolated and MockTransport-only."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.shopee_discovery import invoke
from app.core.security import require_admin
from app.services.auto_pilot import AutoPilotPlan, AutoPilotService

router = APIRouter(
    prefix="/api/v1/auto-pilots", tags=["AUTO_PILOT offline"], dependencies=[Depends(require_admin)]
)


def service(request):
    return getattr(request.app.state, "auto_pilot", None) or AutoPilotService(
        request.app.state.session_factory,
        request.app.state.settings,
    )


class StartOffline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.post("")
def prepare(data: AutoPilotPlan, request: Request):
    return invoke(service(request).prepare, data)


@router.get("/{pilot_id}")
def report(pilot_id: UUID, request: Request):
    return invoke(service(request).report, pilot_id)


@router.post("/{pilot_id}/start-offline")
def start(pilot_id: UUID, data: StartOffline, request: Request):
    return invoke(service(request).start_offline, pilot_id, data.scope_sha256)


@router.post("/{pilot_id}/tick-offline")
def tick(pilot_id: UUID, request: Request):
    return invoke(service(request).tick, pilot_id)


@router.post("/{pilot_id}/stop")
def stop(pilot_id: UUID, request: Request):
    return invoke(service(request).stop, pilot_id)
