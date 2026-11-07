"""Real pilot preparation/approval; no automatic worker launch or implicit approval."""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.api.auto_pilot import StartOffline
from app.api.shopee_discovery import invoke
from app.core.security import require_admin
from app.services.auto_pilot import AutoPilotPlan
from app.services.auto_pilot_real import PilotApproval, RealAutoPilotService
from app.services.auto_pilot_worker import worker_status
from app.services.opportunity_ranking import rank_rows
from app.services.ranking_signal_import import load_ranking_signals

router = APIRouter(
    prefix="/api/v1/auto-pilots-real",
    tags=["AUTO_PILOT real controlado"],
    dependencies=[Depends(require_admin)],
)


def controller(request: Request):
    injected = getattr(request.app.state, "auto_pilot_real", None)
    if injected is not None:
        yield injected
        return
    service = RealAutoPilotService(request.app.state.session_factory, request.app.state.settings)
    try:
        yield service
    finally:
        service.close()


@router.post("")
def prepare(data: AutoPilotPlan, service=Depends(controller)):
    return invoke(service.prepare, data)


@router.get("/{pilot_id}")
def report(pilot_id: UUID, service=Depends(controller)):
    return invoke(service.report, pilot_id)


@router.get("/{pilot_id}/ranking")
def ranking(
    pilot_id: UUID,
    sort_by: Literal["score", "price", "coverage", "opportunity"] = "score",
    service=Depends(controller),
):
    report = invoke(service.report, pilot_id)
    rows = report.get("opportunities", {}).get("analysis_queue", [])
    with service.factory() as session:
        signals = load_ranking_signals(
            session, [row["identity"] for row in rows], now=service._now()
        )
    return dict(
        authorization_id=str(pilot_id),
        state=report["state"],
        candidates=rank_rows(
            rows, now=service._now(), sort_by=sort_by, signals_by_identity=signals
        ),
        sort_by=sort_by,
        publication_authorized=False,
    )


class RankingEvidencePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    confirmed_documented_sources: StrictBool
    signals_by_identity: dict[str, dict[str, Any]] = Field(max_length=100)

    @field_validator("confirmed_documented_sources")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("Prévia enriquecida exige referências documentais confirmadas.")
        return value

    @field_validator("signals_by_identity")
    @classmethod
    def bounded(cls, value):
        for signals in value.values():
            trend = signals.get("trend", [])
            if (
                set(signals) - {"reviews", "popularity", "trend", "seller"}
                or not isinstance(trend, list)
                or len(trend) > 100
            ):
                raise ValueError("Dimensões ou tamanho da série inválidos.")
        return value


@router.post("/{pilot_id}/ranking/preview")
def ranking_preview(pilot_id: UUID, data: RankingEvidencePreview, service=Depends(controller)):
    report = invoke(service.report, pilot_id)
    return dict(
        candidates=rank_rows(
            report.get("opportunities", {}).get("analysis_queue", []),
            now=service._now(),
            signals_by_identity=data.signals_by_identity,
        ),
        actor=data.actor,
        persisted=False,
        publication_authorized=False,
    )


@router.post("/{pilot_id}/approve")
def approve(pilot_id: UUID, data: PilotApproval, service=Depends(controller)):
    return invoke(service.approve, pilot_id, data)


@router.post("/{pilot_id}/start")
def start(pilot_id: UUID, data: StartOffline, request: Request, service=Depends(controller)):
    if getattr(request.app.state, "execution_profile", None) == "ASSISTED_PREPARATION":
        raise HTTPException(
            409, "Modo assistido não inicia piloto real; utilize o worker autorizado."
        )
    return invoke(service.start_authorized, pilot_id, data.scope_sha256)


@router.post("/{pilot_id}/tick")
def tick(pilot_id: UUID, request: Request, service=Depends(controller)):
    if getattr(request.app.state, "execution_profile", None) == "ASSISTED_PREPARATION":
        raise HTTPException(409, "Modo assistido não executa piloto real.")
    return invoke(service.tick, pilot_id)


@router.post("/{pilot_id}/stop")
def stop(pilot_id: UUID, service=Depends(controller)):
    return invoke(service.stop, pilot_id)


@router.post("/{pilot_id}/pause")
def pause(pilot_id: UUID, service=Depends(controller)):
    return invoke(service.pause, pilot_id)


@router.post("/{pilot_id}/resume")
def resume(pilot_id: UUID, data: StartOffline, request: Request, service=Depends(controller)):
    return start(pilot_id, data, request, service)


@router.get("/{pilot_id}/worker")
def status(pilot_id: UUID, service=Depends(controller)):
    invoke(service.report, pilot_id)
    return worker_status(service.factory, pilot_id, service._now())
