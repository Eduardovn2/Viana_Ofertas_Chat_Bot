"""Administrative configuration, evidence and sealed offline exercises. No live switch."""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.api.products import Db
from app.core.security import require_admin
from app.integrations.errors import IntegrationError
from app.models import ShopeeDiscoveryFinding, ShopeeDiscoveryProduct, ShopeeDiscoveryRun
from app.services.discovery_relevance import DiscoveryPolicyInput, SearchTerm
from app.services.ranking_signal_import import RankingSignalImport, import_ranking_signals
from app.services.shopee_evidence_review import ReviewConfirm, ReviewEdit, ShopeeEvidenceReview
from app.services.shopee_evidence_sources import evidence_sources
from app.services.shopee_multistore import MultistoreSearchScope, ProductTarget
from app.services.shopee_single_read import ReadConfirmation

router = APIRouter(
    prefix="/api/v1/shopee-discovery",
    tags=["Descoberta Shopee offline"],
    dependencies=[Depends(require_admin)],
)


class Actor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=3, max_length=100)

    @field_validator("actor")
    @classmethod
    def valid_actor(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Responsável inválido.")
        return value.strip()


class Configure(Actor):
    policy: DiscoveryPolicyInput


class Exercise(Actor):
    request_id: UUID
    operation: Literal["DISCOVER", "MONITOR", "EVALUATE", "RECOVER"]


class Decision(Actor):
    decision: Literal["ACCEPT", "REJECT"]


class PreferencesEdit(Actor):
    expected_revision: int = Field(strict=True, ge=0)
    changes: dict[str, Any] = Field(min_length=1, max_length=40)


class MultistoreSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: SearchTerm
    target: ProductTarget | None = None
    evidence: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("exclusions")
    @classmethod
    def normalized_exclusions(cls, value):
        return DiscoveryPolicyInput.exclusions_normalized(value)


class DefinedProductSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: ProductTarget
    limit: int = Field(default=20, strict=True, ge=1, le=100)
    evidence: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("exclusions")
    @classmethod
    def normalized_exclusions(cls, value):
        return DiscoveryPolicyInput.exclusions_normalized(value)


class SavedSearchAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: ProductTarget | None = None


class OpportunityView(SavedSearchAnalysis):
    selection_limit: int = Field(default=2, strict=True, ge=0, le=20)


class CommercialPreview(SavedSearchAnalysis):
    offer_id: int | None = Field(default=None, strict=True, gt=0)


@router.post(
    "/multistore-searches/{authorization_id}/candidates/{shop_id}/{item_id}/commercial-preview"
)
def preview_multistore_offer(
    authorization_id: UUID, shop_id: str, item_id: str, data: CommercialPreview, request: Request
):
    service = request.app.state.shopee_discovery
    return service._json(
        invoke(
            lambda: service.multistore_commercial_preview(
                authorization_id,
                f"{shop_id}:{item_id}",
                target=data.target,
                offer_id=data.offer_id,
            )
        )
    )


@router.post("/multistore-searches/{authorization_id}/opportunities")
def multistore_opportunities(authorization_id: UUID, data: OpportunityView, request: Request):
    service = request.app.state.shopee_discovery
    return service._json(
        invoke(
            lambda: service.multistore_opportunities(
                authorization_id,
                target=data.target,
                selection_limit=data.selection_limit,
            )
        )
    )


@router.get("/evidence-sources")
def documentary_sources():
    return evidence_sources()


@router.post("/ranking-signals/preview")
def preview_ranking_signals(data: RankingSignalImport):
    from datetime import UTC, datetime

    return invoke(lambda: import_ranking_signals(None, data, now=datetime.now(UTC)))


@router.post("/ranking-signals/import")
def record_ranking_signals(data: RankingSignalImport, request: Request):
    from datetime import UTC, datetime

    with request.app.state.session_factory() as session:
        result = invoke(
            lambda: import_ranking_signals(session, data, now=datetime.now(UTC), persist=True)
        )
        session.commit()
        return result


@router.get("/multistore-searches/{authorization_id}/reviews/{shop_id}/{item_id}")
def read_evidence_review(authorization_id: UUID, shop_id: str, item_id: str, request: Request):
    return invoke(
        ShopeeEvidenceReview(request.app.state.shopee_collection).read,
        authorization_id,
        shop_id,
        item_id,
    )


@router.put("/multistore-searches/{authorization_id}/reviews/{shop_id}/{item_id}")
def edit_evidence_review(
    authorization_id: UUID, shop_id: str, item_id: str, data: ReviewEdit, request: Request
):
    return invoke(
        ShopeeEvidenceReview(request.app.state.shopee_collection).edit,
        authorization_id,
        shop_id,
        item_id,
        data,
    )


@router.post("/multistore-searches/{authorization_id}/reviews/{shop_id}/{item_id}/confirm")
def confirm_evidence_review(
    authorization_id: UUID, shop_id: str, item_id: str, data: ReviewConfirm, request: Request
):
    return invoke(
        ShopeeEvidenceReview(request.app.state.shopee_collection).confirm,
        authorization_id,
        shop_id,
        item_id,
        data,
    )


@router.post("/multistore-searches/{authorization_id}/analyze")
def analyze_multistore_search(authorization_id: UUID, data: SavedSearchAnalysis, request: Request):
    service = request.app.state.shopee_discovery
    return service._json(
        invoke(
            lambda: service.analyze_multistore_search(
                authorization_id,
                target=data.target,
            )
        )
    )


@router.post("/offline-product-search")
def search_defined_product(data: DefinedProductSearch, request: Request):
    service = request.app.state.shopee_discovery
    try:
        return service._json(
            invoke(
                lambda: service.search_defined_product(
                    data.product,
                    evidence=data.evidence,
                    exclusions=data.exclusions,
                    limit=data.limit,
                )
            )
        )
    except IntegrationError as error:
        raise HTTPException(502, {"code": error.code, "message": error.message}) from None


@router.post("/offline-multistore-search")
def search_multistore(data: MultistoreSearch, request: Request):
    service = request.app.state.shopee_discovery
    # Preserve Decimal amounts as strings in the HTTP representation.
    try:
        return service._json(
            invoke(
                lambda: service.search_multistore(
                    data.search.query,
                    intent=data.search.intent,
                    target=data.target,
                    evidence=data.evidence,
                    exclusions=data.exclusions,
                )
            )
        )
    except IntegrationError as error:
        raise HTTPException(502, {"code": error.code, "message": error.message}) from None


@router.get("/preferences")
def preferences(request: Request):
    return invoke(request.app.state.shopee_discovery.preferences)


@router.post("/multistore-searches")
def prepare_multistore_search(data: MultistoreSearchScope, request: Request):
    return invoke(request.app.state.shopee_discovery.prepare_multistore_search, data)


@router.get("/multistore-searches/{authorization_id}")
def multistore_search_status(authorization_id: UUID, request: Request):
    return invoke(request.app.state.shopee_discovery.multistore_search_status, authorization_id)


@router.post("/multistore-searches/{authorization_id}/approve")
def approve_multistore_search(authorization_id: UUID, data: ReadConfirmation, request: Request):
    return invoke(
        request.app.state.shopee_discovery.approve_multistore_search, authorization_id, data
    )


@router.post("/multistore-searches/{authorization_id}/execute")
def execute_multistore_search(authorization_id: UUID, data: ReadConfirmation, request: Request):
    return invoke(
        request.app.state.shopee_discovery.execute_multistore_search, authorization_id, data
    )


@router.patch("/preferences")
def edit_preferences(data: PreferencesEdit, request: Request):
    return invoke(
        request.app.state.shopee_discovery.update_preferences,
        data.changes,
        data.expected_revision,
        data.actor,
    )


@router.get("/destinations/{destination_id}/preferences")
def destination_preferences(destination_id: int, request: Request):
    return invoke(request.app.state.shopee_discovery.preferences, destination_id)


@router.patch("/destinations/{destination_id}/preferences")
def edit_destination_preferences(destination_id: int, data: PreferencesEdit, request: Request):
    return invoke(
        request.app.state.shopee_discovery.update_preferences,
        data.changes,
        data.expected_revision,
        data.actor,
        destination_id,
    )


def invoke(function, *args):
    try:
        return function(*args)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/status")
def status(request: Request):
    return request.app.state.shopee_discovery.status()


@router.put("/categories/{category_id}/policy")
def configure(category_id: int, data: Configure, request: Request):
    return invoke(
        request.app.state.shopee_discovery.configure, category_id, data.policy, data.actor
    )


@router.post("/offline-exercises")
def exercise(data: Exercise, request: Request):
    return invoke(
        request.app.state.shopee_discovery.execute, data.request_id, data.operation, data.actor
    )


@router.post("/schedule/prepare")
def prepare(request: Request):
    return invoke(request.app.state.shopee_discovery.prepare_schedule, request.app.state.monitoring)


@router.patch("/selections/{product_id}/decision")
def decide(product_id: int, data: Decision, request: Request):
    return invoke(
        request.app.state.shopee_discovery.manual_decision, product_id, data.decision, data.actor
    )


@router.get("/runs")
def runs(session: Db):
    return [
        {
            "id": r.id,
            "operation": r.operation,
            "mode": r.mode,
            "status": r.status,
            "requests": r.requests,
            "error_code": r.error_code,
            "result": r.result,
        }
        for r in session.scalars(
            select(ShopeeDiscoveryRun).order_by(ShopeeDiscoveryRun.created_at.desc()).limit(100)
        )
    ]


@router.get("/findings")
def findings(session: Db):
    return [
        {
            "id": r.id,
            "run_id": r.run_id,
            "page": r.page,
            "product_id": r.product_id,
            "history_id": r.history_id,
            "disposition": r.disposition,
            "score": str(r.score),
            "assessment": r.assessment,
        }
        for r in session.scalars(
            select(ShopeeDiscoveryFinding).order_by(ShopeeDiscoveryFinding.id.desc()).limit(100)
        )
    ]


@router.get("/selections")
def selections(session: Db):
    return [
        {
            "product_id": r.product_id,
            "category_id": r.category_id,
            "state": r.state,
            "priority": str(r.priority),
            "next_at": r.next_at,
            "candidate_id": r.candidate_id,
            "manual_decision": r.manual_decision,
            "plan": r.plan,
        }
        for r in session.scalars(
            select(ShopeeDiscoveryProduct)
            .order_by(ShopeeDiscoveryProduct.priority.desc())
            .limit(100)
        )
    ]
