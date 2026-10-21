"""Administrative controls that cannot operate outside an isolated simulation DB."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.api.manual_offers import ApprovalRequest
from app.api.products import Db
from app.core.security import require_admin
from app.models import PublicationDestination, SimulationAudit, TelegramSimulation
from app.services.simulation_safety import (
    SimulationEnrollmentInput,
    SimulationPolicyInput,
    configure_policy,
    enroll_product,
    require_simulation_scope,
)
from app.services.telegram_publication import _row

router = APIRouter(
    prefix="/api/v1/simulated-automation",
    tags=["Automação exclusivamente simulada"],
    dependencies=[Depends(require_admin)],
)


def _scope(session, request):
    try:
        require_simulation_scope(session, request.app.state.settings)
    except ValueError:
        raise HTTPException(
            409,
            "Automação disponível somente em banco/schema isolado de simulação, com controles reais desativados.",
        ) from None


@router.get("/status")
def status(request: Request, session: Db):
    _scope(session, request)
    return {
        "automatic_execution_enabled": request.app.state.settings.simulation_automation_enabled,
        **request.app.state.simulated_pipeline.metrics(),
    }


@router.put("/products/{product_id}")
def enroll(product_id: int, data: SimulationEnrollmentInput, request: Request, session: Db):
    _scope(session, request)
    try:
        profile = enroll_product(session, request.app.state.settings, product_id, data.model_dump())
        session.commit()
        return _row(profile)
    except ValueError:
        raise HTTPException(422, "Use produto fictício marcado e preços válidos.") from None


@router.put("/destinations/{destination_id}/policy")
def policy(destination_id: int, data: SimulationPolicyInput, request: Request, session: Db):
    _scope(session, request)
    destination = session.get(PublicationDestination, destination_id)
    if destination is None:
        raise HTTPException(404, "Destino inexistente.")
    try:
        value = configure_policy(
            session, request.app.state.settings, destination, data.model_dump()
        )
        session.commit()
        return _row(value)
    except ValueError:
        raise HTTPException(422, "Política exige destino SIMULATED e produtos inscritos.") from None


@router.get("/publications")
def publications(request: Request, session: Db):
    _scope(session, request)
    return [
        _row(row)
        for row in session.scalars(
            select(TelegramSimulation).order_by(TelegramSimulation.id.desc()).limit(100)
        )
    ]


@router.get("/publications/{entry_id}/audit")
def audit(entry_id: int, request: Request, session: Db):
    _scope(session, request)
    return [
        _row(row)
        for row in session.scalars(
            select(SimulationAudit)
            .where(SimulationAudit.publication_id == entry_id)
            .order_by(SimulationAudit.id)
            .limit(100)
        )
    ]


@router.post("/publications/{entry_id}/approve")
def approve(entry_id: int, data: ApprovalRequest, request: Request, session: Db):
    _scope(session, request)
    try:
        request.app.state.simulated_pipeline.approve(entry_id, data.preview_hash, data.actor)
    except ValueError:
        raise HTTPException(409, "Aprovação simulada inválida ou prévia alterada.") from None
    return {"mode": "SIMULATED", "approved": True}


@router.post("/run")
def run(request: Request, session: Db):
    _scope(session, request)
    session.rollback()
    return request.app.state.simulated_pipeline.run_pending()
