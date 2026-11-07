"""Append-only evidence and proposed trust policy; never grants publication authority.

AppSetting holds versioned documents; IntegrationEvent holds their audit references.
No observation, AffiliateLink.source or account_verified flag is promoted here.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.models import (
    AppSetting,
    Availability,
    IntegrationEvent,
    Platform,
    PriceAccess,
    ProductPriceHistory,
)
from app.services.comparison import aware_utc
from app.services.shopee_identity import expected_identity, validate_target

PREFIX = "shopee_evidence:"
POLICY_PREFIX = "shopee_trust_proposal:"
ACCOUNT_PREFIX = "shopee_account_evidence:"


class AccountEvidenceInput(BaseModel):
    """One explicit documentary proof, reusable for the bound credential."""

    model_config = ConfigDict(extra="forbid")
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    account_reference: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{3,120}$")
    documented_app_id: SecretStr
    reference: str = Field(pattern=r"^document:[a-zA-Z0-9_.:-]+$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verified_at: datetime
    expires_at: datetime
    confirmed_account_ownership: StrictBool
    confirmed_credential_binding: StrictBool
    # Only when this same official account document explicitly covers old links.
    covered_legacy_link_sha256: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("verified_at", "expires_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Data documental exige fuso horário.")
        return aware_utc(value)

    @field_validator("confirmed_account_ownership", "confirmed_credential_binding")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("A conta e o vínculo da credencial exigem comprovação explícita.")
        return value

    @field_validator("covered_legacy_link_sha256")
    @classmethod
    def hashes(cls, values):
        import re

        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in values):
            raise ValueError("Fingerprint de link inválido.")
        return values


def credential_binding(settings):
    if not settings.shopee_app_id or not settings.shopee_secret:
        raise ValueError("Credencial Shopee não configurada.")
    return fingerprint(
        dict(
            app_id=settings.shopee_app_id.get_secret_value(),
            secret=settings.shopee_secret.get_secret_value(),
        )
    )


def record_account_evidence(session, settings, data, *, now=None):
    now = aware_utc(now or datetime.now(UTC))
    data = AccountEvidenceInput.model_validate(data)
    if not settings.shopee_app_id or not hmac.compare_digest(
        data.documented_app_id.get_secret_value(), settings.shopee_app_id.get_secret_value()
    ):
        raise ValueError("O documento não corresponde à credencial configurada.")
    if (
        not data.verified_at <= now < data.expires_at
        or data.expires_at - data.verified_at > timedelta(days=365)
    ):
        raise ValueError("Validade documental inválida, expirada ou maior que um ano.")
    value = dict(
        **data.model_dump(mode="json", exclude={"documented_app_id"}),
        credential_fingerprint=credential_binding(settings),
        version=1,
        publication_authorized=False,
        source="OFFICIAL_ACCOUNT_DOCUMENT_REVIEW",
    )
    key = ACCOUNT_PREFIX + fingerprint(value)
    value["integrity_signature"] = hmac.new(
        settings.shopee_secret.get_secret_value().encode(),
        ("account-evidence:" + fingerprint(value)).encode(),
        hashlib.sha256,
    ).hexdigest()
    from app.services.leases import conflict_insert

    session.execute(
        conflict_insert(session, AppSetting)
        .values(key=key, value=value)
        .on_conflict_do_nothing(index_elements=[AppSetting.key])
    )
    return dict(id=key, **{k: v for k, v in value.items() if k != "integrity_signature"})


def account_evidence(session, settings, *, evidence_id=None, now=None):
    now = aware_utc(now or datetime.now(UTC))
    query = (
        select(AppSetting)
        .where(AppSetting.key.startswith(ACCOUNT_PREFIX))
        .order_by(AppSetting.id.desc())
    )
    if evidence_id is not None:
        query = query.where(AppSetting.key == evidence_id)
    for row in session.scalars(query):
        value = dict(row.value)
        signature = value.pop("integrity_signature", "")
        if value.get("credential_fingerprint") != credential_binding(settings):
            continue
        expected = hmac.new(
            settings.shopee_secret.get_secret_value().encode(),
            ("account-evidence:" + fingerprint(value)).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected) or row.key != ACCOUNT_PREFIX + fingerprint(
            value
        ):
            continue
        if (
            not datetime.fromisoformat(value["verified_at"])
            <= now
            < datetime.fromisoformat(value["expires_at"])
        ):
            continue
        return dict(id=row.key, **value)
    return None


def preserved_link_account_readiness(
    session, settings, product, generation_reference, *, evidence_id=None, now=None
):
    """No link generation/association and no retroactive credential attestation."""
    proof = account_evidence(session, settings, evidence_id=evidence_id, now=now)
    if proof is None:
        return dict(
            account_documented=False,
            ownership_reusable=False,
            blocker="ONE_REUSABLE_ACCOUNT_CREDENTIAL_DOCUMENT_REQUIRED",
            publication_authorized=False,
        )
    generation = session.scalar(
        select(AppSetting.value).where(AppSetting.key == generation_reference)
    )
    if not generation_reference.startswith("shopee_shortlink_once:") or not generation:
        raise ValueError("Registro de geração preservado inexistente.")
    result = generation.get("result", {})
    if (
        generation.get("status") != "SUCCEEDED"
        or result.get("http_status") != 200
        or result.get("real_posts") != 1
    ):
        raise ValueError("Geração não confirmada ou operação incerta; não reutilizar.")
    short = result.get("shortLink")
    if not short or not validate_target(result.get("longLink"), expected_identity(product)):
        raise ValueError("Registro não corresponde à identidade do anúncio.")
    bound = generation.get("credential_fingerprint") == proof["credential_fingerprint"]
    covered = link_fingerprint(short) in proof["covered_legacy_link_sha256"]
    return dict(
        account_documented=True,
        account_evidence_id=proof["id"],
        ownership_reusable=bound or covered,
        short_link=short,
        generation_credential_bound=bound,
        legacy_link_explicitly_documented=covered,
        blocker=None if bound or covered else "LEGACY_GENERATION_CREDENTIAL_BINDING_MISSING",
        variant_binding_required=True,
        operationally_verified=False,
        publication_authorized=False,
    )


class EvidenceKind(StrEnum):
    AUTHENTICATED_GENERATION = "AUTHENTICATED_GENERATION"
    PRODUCT_IDENTITY = "PRODUCT_IDENTITY"
    CREDENTIAL_PROVENANCE = "CREDENTIAL_PROVENANCE"
    OWNERSHIP_ATTESTATION = "OWNERSHIP_ATTESTATION"
    CONVERSION_RECORDED = "CONVERSION_RECORDED"
    COMMISSION_VALIDATED = "COMMISSION_VALIDATED"
    COMMERCIAL_ATTESTATION = "COMMERCIAL_ATTESTATION"


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str = Field(min_length=1, max_length=80, pattern=r"^[\w.@-]+$")
    reference: str = Field(
        min_length=1, max_length=180, pattern=r"^(audit|document|report):[a-zA-Z0-9_.:-]+$"
    )
    verified_at: datetime

    @field_validator("verified_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)


class LinkEvidenceInput(EvidenceInput):
    kind: EvidenceKind
    link_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    account_reference: str | None = Field(
        default=None, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )
    credential_reference: str | None = Field(
        default=None, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )


class CommercialEvidenceInput(EvidenceInput):
    history_id: int = Field(gt=0)
    applicable_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    variant_id: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_.:-]*$")
    availability: Availability = Availability.UNKNOWN
    price_access: PriceAccess = PriceAccess.UNKNOWN
    conditions_confirmed: bool = False
    conditions: str = Field(default="", max_length=500)


class TrustPolicyProposal(EvidenceInput):
    require_authenticated_generation: bool = True
    require_ownership_attestation: bool = True
    require_conversion: bool = False
    require_commission: bool = False


def link_fingerprint(url):
    return hashlib.sha256(url.encode()).hexdigest()


def _persist(session, product, value, *, now, policy=False):
    if product.platform != Platform.SHOPEE:
        raise ValueError("Evidência restrita a produtos Shopee.")
    verified = aware_utc(datetime.fromisoformat(value["verified_at"]))
    if verified > now:
        raise ValueError("Verificação não pode estar no futuro.")
    key = (POLICY_PREFIX if policy else PREFIX) + uuid4().hex
    value = {
        **value,
        "version": 1,
        "product_id": product.id,
        "external_id": product.external_id,
        "variant_id_at_recording": product.variant_id,
        "origin_sha256": link_fingerprint(product.original_url),
        "created_at": now.isoformat(),
        "publication_authorized": False,
    }
    session.add(AppSetting(key=key, value=value, description="Shopee evidence; no activation."))
    session.add(
        IntegrationEvent(
            product_id=product.id,
            platform=Platform.SHOPEE,
            operation="trust_policy_proposed" if policy else "evidence_recorded",
            status="PROPOSED" if policy else "RECORDED",
            observed_at=now,
            source_reference=key,
        )
    )
    session.flush()
    return {"id": key, **value}


def record_manual_link_evidence(session, product, data: LinkEvidenceInput, *, now=None):
    now = aware_utc(now or datetime.now(UTC))
    link = product.affiliate_link
    if link is None or link_fingerprint(link.url) != data.link_sha256:
        raise ValueError("Evidência não corresponde ao link atual.")
    if data.kind in {EvidenceKind.AUTHENTICATED_GENERATION, EvidenceKind.COMMERCIAL_ATTESTATION}:
        raise ValueError("Atestado manual não comprova geração autenticada ou preço.")
    if data.kind != EvidenceKind.PRODUCT_IDENTITY and not data.account_reference:
        raise ValueError("Atestado exige referência administrativa não secreta da conta.")
    if data.kind == EvidenceKind.CREDENTIAL_PROVENANCE and not data.credential_reference:
        raise ValueError("Proveniência exige referência não secreta da credencial.")
    if data.kind == EvidenceKind.PRODUCT_IDENTITY and not validate_target(
        link.url, expected_identity(product), product.variant_id
    ):
        raise ValueError("Identidade ou variante não comprovada pelo link.")
    return _persist(
        session,
        product,
        {
            **data.model_dump(mode="json"),
            "basis": "MANUAL_ATTESTATION",
            "account_verified": False,
        },
        now=now,
    )


def record_commercial_evidence(session, product, data: CommercialEvidenceInput, *, now=None):
    now = aware_utc(now or datetime.now(UTC))
    history = session.get(ProductPriceHistory, data.history_id)
    if (
        history is None
        or history.product_id != product.id
        or history.is_simulated
        or history.current_price != data.applicable_price
        or data.variant_id != product.variant_id
        or data.verified_at < history.observed_at
    ):
        raise ValueError(
            "Atestado deve referenciar preço, variante e observação reais compatíveis."
        )
    return _persist(
        session,
        product,
        {
            **data.model_dump(mode="json"),
            "kind": EvidenceKind.COMMERCIAL_ATTESTATION,
            "basis": "MANUAL_ATTESTATION",
            "observation_source": history.source,
            "observation_source_type": history.source_type,
            "observation_observed_at": history.observed_at.isoformat(),
            "observation_fingerprint": history.raw_fingerprint,
            "observation_availability": history.availability,
            "observation_price_access": history.price_access,
        },
        now=now,
    )


def propose_trust_policy(session, product, data: TrustPolicyProposal, *, now=None):
    return _persist(
        session,
        product,
        {
            **data.model_dump(mode="json"),
            "status": "PROPOSED",
            "identity_required": True,
            "credential_provenance_required": True,
            "account_verified": False,
        },
        now=aware_utc(now or datetime.now(UTC)),
        policy=True,
    )


def evidence_readiness(session, product, policy_id, *, now=None, max_age_seconds=600):
    """Diagnostic only. Proposals cannot activate trust or approve an offer."""
    now = aware_utc(now or datetime.now(UTC))
    policy = session.scalar(select(AppSetting.value).where(AppSetting.key == policy_id))
    if (
        not policy_id.startswith(POLICY_PREFIX)
        or policy is None
        or policy.get("product_id") != product.id
    ):
        raise ValueError("Proposta de confiança inexistente para este produto.")
    fingerprint = link_fingerprint(product.affiliate_link.url) if product.affiliate_link else None
    rows = session.scalars(select(AppSetting.value).where(AppSetting.key.startswith(PREFIX)))
    rows = [
        r
        for r in rows
        if r.get("product_id") == product.id
        and r.get("external_id") == product.external_id
        and r.get("variant_id_at_recording") == product.variant_id
        and r.get("origin_sha256") == link_fingerprint(product.original_url)
    ]
    kinds = {
        r["kind"]
        for r in rows
        if r.get("link_sha256") == fingerprint
        and fingerprint
        and r.get("basis") == "MANUAL_ATTESTATION"
        and r["kind"] != EvidenceKind.AUTHENTICATED_GENERATION
    }
    provenance_accounts = {
        r.get("account_reference")
        for r in rows
        if r.get("link_sha256") == fingerprint
        and fingerprint
        and r["kind"] == EvidenceKind.CREDENTIAL_PROVENANCE
        and r.get("credential_reference")
        and r.get("account_reference")
    }
    for kind in (
        EvidenceKind.OWNERSHIP_ATTESTATION,
        EvidenceKind.CONVERSION_RECORDED,
        EvidenceKind.COMMISSION_VALIDATED,
    ):
        if not any(
            r["kind"] == kind
            and r.get("link_sha256") == fingerprint
            and r.get("account_reference") in provenance_accounts
            for r in rows
        ):
            kinds.discard(kind)
    # A document claim cannot be relabeled as an authenticated API result.
    required = {EvidenceKind.PRODUCT_IDENTITY, EvidenceKind.CREDENTIAL_PROVENANCE}
    for option, kind in (
        ("require_authenticated_generation", EvidenceKind.AUTHENTICATED_GENERATION),
        ("require_ownership_attestation", EvidenceKind.OWNERSHIP_ATTESTATION),
        ("require_conversion", EvidenceKind.CONVERSION_RECORDED),
        ("require_commission", EvidenceKind.COMMISSION_VALIDATED),
    ):
        if policy[option]:
            required.add(kind)
    commercial = False
    for row in rows:
        if row["kind"] != EvidenceKind.COMMERCIAL_ATTESTATION:
            continue
        verified = datetime.fromisoformat(row["verified_at"])
        history = session.get(ProductPriceHistory, row["history_id"])
        commercial |= bool(
            now - timedelta(seconds=max_age_seconds) <= verified <= now
            and row["availability"] == Availability.AVAILABLE
            and row["price_access"] == PriceAccess.PUBLIC
            and row["conditions_confirmed"]
            and row["conditions"].strip()
            and row["variant_id"] == product.variant_id
            and history
            and not history.is_simulated
            and str(history.current_price) == row["applicable_price"]
            and history.source == row["observation_source"]
            and history.source_type == row["observation_source_type"]
            and history.product_id == product.id
            and history.currency == "BRL"
            and history.observed_at.isoformat() == row["observation_observed_at"]
            and history.raw_fingerprint == row["observation_fingerprint"]
            and history.availability == row["observation_availability"]
            and history.price_access == row["observation_price_access"]
        )
    return {
        "policy_status": "PROPOSED",
        "missing_link_evidence": sorted(required - kinds),
        "link_criteria_met": not (required - kinds),
        "commercial_evidence_sufficient": commercial,
        "usable": False,
        "account_verified": False,
        "publication_authorized": False,
        "blockers": ["POLICY_REQUIRES_EXPLICIT_APPROVAL", "REAL_PIPELINE_DISABLED"],
    }
