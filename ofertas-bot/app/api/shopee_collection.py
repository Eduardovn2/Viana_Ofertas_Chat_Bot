"""Local authenticated controls; offline imports and automatic polling are not exposed."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.api.products import Db
from app.core.security import require_admin
from app.models import ShopeeCollectionCandidate, ShopeeCollectionRun
from app.services.shopee_collection import CollectionRequest, ObservationRequest
from app.services.shopee_recurring import PilotConfirmation, RecurringScope, ShopeeRecurringMonitor
from app.services.shopee_single_read import ReadApproval, ReadConfirmation, ShopeeSingleReadAdapter

router = APIRouter(
    prefix="/api/v1/shopee-collection",
    tags=["Coleta Shopee controlada"],
    dependencies=[Depends(require_admin)],
)


@router.post("/runs")
def run(data: CollectionRequest, request: Request):
    try:
        return request.app.state.shopee_collection.execute(data)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/runs")
def runs(session: Db):
    return [
        {
            "id": r.id,
            "mode": r.mode,
            "status": r.status,
            "actor": r.actor,
            "result": r.result,
            "error_code": r.error_code,
        }
        for r in session.scalars(
            select(ShopeeCollectionRun).order_by(ShopeeCollectionRun.created_at.desc()).limit(100)
        )
    ]


@router.post("/observations")
def observe(data: ObservationRequest, request: Request):
    """Validate the future scope; the operational backend cannot execute it yet."""
    try:
        return request.app.state.shopee_collection.observe(data)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/candidates")
def candidates(session: Db):
    return [
        {
            "id": r.id,
            "run_id": r.run_id,
            "product_id": r.product_id,
            "history_id": r.history_id,
            "status": r.status,
            "assessment": r.assessment,
        }
        for r in session.scalars(
            select(ShopeeCollectionCandidate)
            .order_by(ShopeeCollectionCandidate.id.desc())
            .limit(100)
        )
    ]


def single_read(request: Request):
    return ShopeeSingleReadAdapter(request.app.state.shopee_collection)


def recurring_pilot(request: Request):
    return ShopeeRecurringMonitor(request.app.state.shopee_collection)


@router.post("/recurring-pilots")
def prepare_recurring_pilot(data: RecurringScope, request: Request):
    try:
        return recurring_pilot(request).prepare_pilot(data)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/recurring-pilots/{authorization_id}")
def read_recurring_pilot(authorization_id: UUID, request: Request):
    try:
        return recurring_pilot(request).status(authorization_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@router.post("/recurring-pilots/{authorization_id}/approve")
def approve_recurring_pilot(authorization_id: UUID, data: PilotConfirmation, request: Request):
    try:
        return recurring_pilot(request).approve_pilot(authorization_id, data)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/recurring-pilots/{authorization_id}/cycles/{cycle_id}/execute")
def execute_recurring_pilot(
    authorization_id: UUID, cycle_id: UUID, data: PilotConfirmation, request: Request
):
    try:
        return recurring_pilot(request).execute_pilot(authorization_id, cycle_id, data)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/identity-reads/plan")
def plan_identity_read(data: ObservationRequest, request: Request):
    try:
        return single_read(request).plan(data)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/identity-reads")
def prepare_identity_read(data: ObservationRequest, request: Request):
    try:
        return single_read(request).prepare(data)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/identity-reads/{authorization_id}")
def read_identity_authorization(authorization_id: UUID, request: Request):
    try:
        return single_read(request).read(authorization_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@router.post("/identity-reads/{authorization_id}/approve")
def approve_identity_read(authorization_id: UUID, data: ReadApproval, request: Request):
    try:
        return single_read(request).approve(authorization_id, data)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/identity-reads/{authorization_id}/execute")
def execute_identity_read(authorization_id: UUID, data: ReadConfirmation, request: Request):
    try:
        return single_read(request).execute(authorization_id, data)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/candidates/{candidate_id}/revalidate")
def revalidate(candidate_id: int, request: Request):
    try:
        return request.app.state.shopee_collection.revalidate(candidate_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/schedule/prepare")
def prepare(request: Request):
    try:
        return request.app.state.shopee_collection.prepare_schedule(request.app.state.monitoring)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
