"""Read-only discovery preview and binding into the existing manual-offer workflow."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.models import AppSetting, Offer, Platform, Product, ProductPriceHistory
from app.services.comparison import aware_utc
from app.services.shopee_evidence_review import PREFIX as REVIEW_PREFIX
from app.services.shopee_multistore import ProductTarget, ShopeeMultistoreAdapter
from app.services.shopee_replay import analyze_recorded_search


class CandidateContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: UUID
    identity: str = Field(pattern=r"^[1-9][0-9]*:[1-9][0-9]*$", max_length=100)
    observation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target: ProductTarget | None = None


def saved_analysis(session, authorization_id, target=None):
    """Read current source and reviews in the caller's transaction, without credentials/HTTP."""
    identity = str(UUID(str(authorization_id)))
    source = session.scalar(
        select(AppSetting).where(AppSetting.key == ShopeeMultistoreAdapter.key(identity))
    )
    if source is None:
        raise LookupError("Busca multiloja não encontrada.")
    value = source.value
    if fingerprint(value["scope"]) != value["scope_sha256"]:
        raise ValueError("Escopo multiloja divergente.")
    prefix = REVIEW_PREFIX + identity + ":"
    records = {
        key[len(prefix) :]: record
        for key, record in session.execute(
            select(AppSetting.key, AppSetting.value).where(AppSetting.key.startswith(prefix))
        )
    }
    return analyze_recorded_search(
        dict(value, status=value["state"], authorization_id=identity),
        target=target,
        documented_reviews=records,
    )


def candidate_context(analysis, identity):
    row = next((r for r in analysis["candidates"] if r["identity"] == identity), None)
    if row is None:
        raise LookupError("Anúncio não encontrado nesta busca.")
    decision = next(
        d for d in analysis["opportunity_view"]["candidate_decisions"] if d["identity"] == identity
    )
    selected = any(
        c["identity"] == identity
        for c in analysis["opportunity_view"]["commercial_handoff"]["candidates"]
    )
    seal = dict(
        source_scope_sha256=analysis.get("source_scope_sha256"),
        observation_mode=analysis.get("observation_mode"),
        source_comparison_sha256=analysis.get("source_comparison_sha256"),
        candidate=row,
        decision=decision,
        selected=selected,
        target=analysis.get("requested_product"),
    )
    return row, decision, selected, fingerprint(seal)


def validate_binding(
    session, context, product, current_price, verified_at, *, allow_simulated=False
):
    """Called before creation and again by existing offer preview/approval/send validation."""
    context = CandidateContext.model_validate(context)
    analysis = saved_analysis(session, context.authorization_id, context.target)
    row, decision, selected, digest = candidate_context(analysis, context.identity)
    if (
        not selected
        or (analysis.get("observation_mode") != "REAL_MULTISTORE_SEARCH" and not allow_simulated)
        or decision["action"] != "AUTO_COMPARED"
        or context.context_sha256 != digest
        or context.observation_fingerprint != row.get("observation_fingerprint")
        or product.platform != Platform.SHOPEE
        or product.external_id != context.identity
        or Decimal(str(row["announced_price"])) != current_price
        or aware_utc(verified_at) < aware_utc(row["observed_at"])
    ):
        raise ValueError("Candidato, evidências ou condições divergentes da prévia multiloja.")
    # The explicit manual verification applies to the registered product's selected
    # variant. Neither title hints nor a textual variant name is treated as a SKU.
    return context.model_dump(mode="json")


def commercial_preview(session, settings, analysis, identity, *, now, offer_id=None):
    from app.integrations.errors import IntegrationError
    from app.services.manual_offers import read_manual_offer

    row, decision, selected, digest = candidate_context(analysis, identity)
    blockers = []
    if analysis.get("observation_mode") != "REAL_MULTISTORE_SEARCH":
        blockers.append("SIMULATED_SOURCE_NOT_COMMERCIAL_EVIDENCE")
    if not selected or decision["action"] != "AUTO_COMPARED":
        blockers.append("CANDIDATE_NOT_SELECTED_WITH_PROVEN_EQUIVALENCE")
    if offer_id is None:
        # Match the sealed observation, never just a title or a low price. Multiple
        # matching variants remain an explicit administrative choice.
        possible = session.execute(
            select(Offer.id, ProductPriceHistory.evidence_metadata)
            .join(Product, Product.id == Offer.product_id)
            .join(ProductPriceHistory, ProductPriceHistory.id == Offer.price_history_id)
            .where(Product.platform == Platform.SHOPEE, Product.external_id == identity)
            .order_by(Offer.id.desc())
            .limit(21)
        ).all()
        matching = [
            offer
            for offer, metadata in possible
            if isinstance(binding := (metadata or {}).get("multistore_context"), dict)
            and binding.get("context_sha256") == digest
        ]
        valid = []
        for candidate_id in matching:
            try:
                read_manual_offer(session, candidate_id, settings, now)
                valid.append(candidate_id)
            except IntegrationError:
                pass
        if len(valid) == 1 and len(possible) <= 20:
            offer_id = valid[0]
        elif len(valid) > 1 or len(possible) > 20:
            blockers.append("MULTIPLE_OR_TRUNCATED_EXISTING_OFFERS_SELECT_EXPLICIT_ID")
    commercial = None
    if offer_id is not None:
        try:
            bundle = read_manual_offer(session, offer_id, settings, now)
            binding = (bundle.history.evidence_metadata or {}).get("multistore_context")
            if binding is None:
                raise ValueError("Oferta sem vínculo documental com esta prévia.")
            expected = CandidateContext.model_validate(binding)
            if (
                str(expected.authorization_id) != analysis.get("source_authorization_id")
                or expected.identity != identity
                or expected.context_sha256 != digest
            ):
                raise ValueError("Oferta vinculada a outra prévia.")
            commercial = dict(
                offer_id=bundle.offer.id,
                product_id=bundle.product.id,
                verified_by=bundle.details.verified_by,
                verified_at=bundle.details.verified_at,
                price_history_id=bundle.history.id,
                applicable_public_price=bundle.offer.current_price,
                availability=bundle.history.availability.value,
                price_access=bundle.history.price_access.value,
                coupon_adjusted_price=bundle.offer.final_price,
                existing_affiliate_link_verified=True,
                evidence_reused=True,
            )
        except IntegrationError as error:
            blockers.append(error.code)
        except (LookupError, ValueError):
            blockers.append("MULTISTORE_OFFER_BINDING_INVALID_OR_STALE")
    if commercial is None:
        blockers.extend(
            [
                "EXPLICIT_CURRENT_PUBLIC_PRICE_AND_AVAILABILITY_VERIFICATION_REQUIRED",
                "EXISTING_VERIFIED_AFFILIATE_LINK_REQUIRED",
                "SELECTED_VARIANT_AND_COMMERCIAL_TERMS_CONFIRMATION_REQUIRED",
            ]
        )
    blockers.extend(
        [
            "DESTINATION_CATEGORY_AND_COOLDOWN_REVALIDATION_REQUIRED",
            "EXPLICIT_DESTINATION_PREVIEW_APPROVAL_REQUIRED",
            "PUBLICATION_AUTHORIZATION_REQUIRED",
        ]
    )
    context = dict(
        authorization_id=analysis.get("source_authorization_id"),
        identity=identity,
        observation_fingerprint=row.get("observation_fingerprint"),
        context_sha256=digest,
        target=analysis.get("requested_product"),
    )
    return dict(
        version="MULTISTORE_ADMIN_OFFER_PREVIEW_V1",
        identity=identity,
        shop_id=row["shop_id"],
        item_id=row["item_id"],
        name=row["name"],
        seller=row["seller"],
        announced_price=row["announced_price"],
        currency=row["currency"],
        observed_at=row["observed_at"],
        observation_mode=analysis.get("observation_mode"),
        attributes=row["attributes"],
        attribute_states=decision["attribute_states"],
        documentary_sources=row.get("documentary_sources", {}),
        inferred_attributes=row.get("inferred_attributes", {}),
        missing_identity_evidence=decision["missing_information"],
        commercial_handoff_selected=selected,
        commercial_evidence=commercial,
        blockers=blockers,
        final_delivered_price=None,
        shipping=None,
        historical_discount=None,
        cross_store_spread_is_historical_discount=False,
        can_create_offer=False,
        can_approve_offer=False,
        publication_authorized=False,
        history_writes=0,
        external_http_calls=0,
        next_step=dict(
            method="POST",
            endpoint=f"/api/v1/manual-offers/{offer_id}/preview"
            if commercial
            else "/api/v1/manual-offers",
            multistore_context=context,
            explicit_commercial_verification_required=commercial is None,
            automatic_execution=False,
        ),
        preview_sha256=fingerprint(dict(context=context, commercial=commercial, blockers=blockers)),
        preview_hash_is_approval=False,
    )
