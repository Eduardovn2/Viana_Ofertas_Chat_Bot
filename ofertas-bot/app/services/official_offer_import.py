"""Local, confirmed official CSV documents into the existing manual-offer service.

No provider I/O, link generation, source fetching, product creation or publication.
Column meanings are supplied and explicitly confirmed, never inferred from headers.
"""

import csv
import hashlib
import io
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError
from app.models import AppSetting, Platform, Product, ProductPriceHistory
from app.services.comparison import aware_utc
from app.services.leases import conflict_insert
from app.services.manual_offers import create_manual_offer, read_manual_offer
from app.services.offers import has_own_affiliate_link
from app.services.product_documentation import DocumentedIdentity
from app.services.shopee_identity import expected_identity, url_identity, validate_target

PREFIX = "official_offer_export:"
CURRENT_PREFIX = "official_offer_current:"
FIELDS = {
    "shop_id",
    "item_id",
    "variant_id",
    "name",
    "original_url",
    "public_price",
    "availability",
    "price_access",
    "currency",
    "observed_at",
    "all_options_same_public_terms",
    "all_options_available",
}
REQUIRED = FIELDS - {"variant_id", "all_options_same_public_terms", "all_options_available"}


class BoundCommercialRowError(ValueError):
    def __init__(self, reason, product_id, observed_at):
        super().__init__(reason)
        self.product_id, self.observed_at = product_id, observed_at


def _record_current(session, data, product_id, observed_at, row, state):
    key = CURRENT_PREFIX + str(product_id)
    session.execute(
        conflict_insert(session, AppSetting)
        .values(key=key, value={})
        .on_conflict_do_nothing(index_elements=[AppSetting.key])
    )
    current = session.scalar(select(AppSetting).where(AppSetting.key == key).with_for_update())
    if current.value and datetime.fromisoformat(current.value["observed_at"]) > observed_at:
        return False
    current.value = dict(
        observed_at=observed_at.isoformat(),
        state=state,
        content_sha256=data.content_sha256,
        row_sha256=fingerprint(row),
    )
    return True


def validate_current_import(session, history):
    provenance = (history.evidence_metadata or {}).get("official_export")
    if not provenance:
        return
    current = session.scalar(
        select(AppSetting.value).where(AppSetting.key == CURRENT_PREFIX + str(history.product_id))
    )
    if (
        not current
        or current["state"] != "CURRENT"
        or current["content_sha256"] != provenance["content_sha256"]
        or current["row_sha256"] != provenance["row_sha256"]
    ):
        raise ValueError("OFFICIAL_EXPORT_SUPERSEDED_OR_INCONCLUSIVE")


class OfficialExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_kind: Literal["OFFICIAL_EXPORT", "OFFICIAL_PAGE_OBSERVATION"] = "OFFICIAL_EXPORT"
    observation_source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    csv_content: str = Field(min_length=1, max_length=1_000_000)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_url: str = Field(max_length=2048)
    source_reference: str = Field(min_length=3, max_length=500)
    account_reference: str = Field(min_length=3, max_length=200)
    column_semantics_reference: str = Field(min_length=3, max_length=500)
    exported_at: datetime
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    delimiter: Literal[",", ";", "\t"] = ","
    decimal_separator: Literal[".", ","] = "."
    columns: dict[str, str] = Field(min_length=1, max_length=len(FIELDS))
    available_value: str = Field(min_length=1, max_length=100)
    public_value: str = Field(min_length=1, max_length=100)
    all_options_true_value: str = Field(default="true", min_length=1, max_length=100)
    confirmed_official_document: StrictBool = False
    confirmed_column_semantics: StrictBool = False
    confirmed_commercial_import: StrictBool = False
    identity_documents: dict[str, DocumentedIdentity] = Field(default_factory=dict, max_length=200)
    confirmed_new_identity_documents: StrictBool = False

    @field_validator("source_url")
    @classmethod
    def official_origin(cls, value, info: ValidationInfo):
        parts = urlsplit(value)
        hosts = {"affiliate.shopee.com.br", "open-api.affiliate.shopee.com.br"}
        if info.data.get("document_kind") == "OFFICIAL_PAGE_OBSERVATION":
            hosts = {"shopee.com.br", "www.shopee.com.br"}
            url_identity(value)
        if (
            parts.scheme != "https"
            or parts.hostname not in hosts
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            raise ValueError("Origem oficial Shopee requerida; nenhum endereço será acessado.")
        return value

    @model_validator(mode="after")
    def observed_page_provenance(self):
        if self.document_kind == "OFFICIAL_PAGE_OBSERVATION" and not self.observation_source_sha256:
            raise ValueError(
                "Observação de página exige fingerprint da evidência original preservada."
            )
        return self

    @field_validator("exported_at")
    @classmethod
    def timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Data exige fuso horário.")
        return aware_utc(value)

    @field_validator("columns")
    @classmethod
    def column_names(cls, value):
        if set(value) - FIELDS or len(set(value.values())) != len(value):
            raise ValueError("Mapeamento desconhecido ou ambíguo.")
        if any(not v.strip() for v in value.values()):
            raise ValueError("Nome de coluna vazio.")
        return value

    @field_validator("available_value", "public_value", "all_options_true_value")
    @classmethod
    def explicit_known_status(cls, value):
        if value.strip().casefold() in {"unknown", "desconhecido", "null", "none", "n/a", "na"}:
            raise ValueError(
                "Estado desconhecido não pode significar condição comercial comprovada."
            )
        return value


def _document(data, now):
    raw = data.csv_content.encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != data.content_sha256:
        raise ValueError("Fingerprint do arquivo não corresponde aos bytes UTF-8 fornecidos.")
    if data.exported_at > now:
        raise ValueError("Exportação não pode estar no futuro.")
    reader = csv.DictReader(
        io.StringIO(data.csv_content.lstrip("\ufeff")), delimiter=data.delimiter, strict=True
    )
    try:
        headers = reader.fieldnames or []
    except csv.Error:
        raise ValueError("Cabeçalho CSV inválido.") from None
    if not headers or len(set(headers)) != len(headers) or any(not v.strip() for v in headers):
        raise ValueError("Cabeçalhos ausentes, vazios ou duplicados.")
    if set(data.columns.values()) - set(headers):
        raise ValueError("Colunas mapeadas não existem no documento.")
    try:
        rows = list(reader)
    except csv.Error:
        raise ValueError("CSV inválido ou aspas ambíguas.") from None
    if not rows or len(rows) > 200 or any(None in row or None in row.values() for row in rows):
        raise ValueError("Exportação exige 1 a 200 linhas com estrutura CSV inequívoca.")
    if data.document_kind == "OFFICIAL_PAGE_OBSERVATION" and len(rows) != 1:
        raise ValueError("Observação de página deve corresponder a exatamente um anúncio.")
    binding = data.model_dump(
        mode="json",
        exclude={
            "csv_content",
            "actor",
            "confirmed_official_document",
            "confirmed_column_semantics",
            "confirmed_new_identity_documents",
            "confirmed_commercial_import",
        },
    )
    # Preserve hashes of all previously reviewed export manifests.
    if data.document_kind == "OFFICIAL_EXPORT":
        binding.pop("document_kind", None)
        binding.pop("observation_source_sha256", None)
    return rows, fingerprint(binding)


def _proof(session, product, data, observed, price, row, now):
    supplied = data.identity_documents.get(str(product.id))
    if supplied is not None:
        if not data.confirmed_new_identity_documents:
            raise ValueError("NEW_IDENTITY_DOCUMENT_CONFIRMATION_REQUIRED")
        proof = supplied
    else:
        history = session.scalars(
            select(ProductPriceHistory)
            .where(
                ProductPriceHistory.product_id == product.id,
                ProductPriceHistory.is_simulated.is_(False),
            )
            .order_by(ProductPriceHistory.observed_at.desc(), ProductPriceHistory.id.desc())
        ).all()
        # Latest explicit identity document is authoritative; never fall back to an
        # older document when the current one has become invalid or contradictory.
        stored = next(
            (
                h.evidence_metadata.get("documented_identity")
                for h in history
                if (h.evidence_metadata or {}).get("documented_identity")
            ),
            None,
        )
        if stored is None:
            raise ValueError("DOCUMENTED_IDENTITY_REQUIRED")
        proof = DocumentedIdentity.model_validate(stored)
    proof.validate_product(product, now)
    if proof.identity_level == "LISTING_ALL_OPTIONS":
        if any(
            row.get(data.columns.get(field, "")) != data.all_options_true_value
            for field in ("all_options_same_public_terms", "all_options_available")
        ):
            raise ValueError("ALL_OPTIONS_COVERAGE_REQUIRED")
        # Only explicit columns of this confirmed document can refresh these
        # commercial statements. A newly exported file date alone never does so.
        proof = DocumentedIdentity.model_validate(
            proof.model_dump()
            | dict(
                source="OFFICIAL_EXPORT",
                source_sha256=data.content_sha256,
                actor=data.actor,
                reference=data.source_reference,
                verified_at=observed,
                all_options_public_price=price,
            )
        )
    return proof


def _row(session, data, row, now, *, require_link=True):
    missing = REQUIRED - set(data.columns)
    if missing:
        raise ValueError("MISSING_COLUMNS:" + ",".join(sorted(missing)))

    def get(name):
        return row.get(data.columns.get(name, ""), "")

    if not re.fullmatch(r"[1-9][0-9]{0,19}", get("shop_id")) or not re.fullmatch(
        r"[1-9][0-9]{0,19}", get("item_id")
    ):
        raise ValueError("INVALID_LISTING_IDENTITY")
    identity = get("shop_id") + ":" + get("item_id")
    products = session.scalars(
        select(Product)
        .where(
            Product.platform == Platform.SHOPEE,
            Product.external_id == identity,
            Product.variant_id == get("variant_id"),
        )
        .with_for_update()
    ).all()
    if len(products) != 1:
        raise ValueError("REGISTERED_PRODUCT_AND_OPTION_REQUIRED")
    product = products[0]
    if data.document_kind == "OFFICIAL_PAGE_OBSERVATION" and url_identity(
        data.source_url
    ) != expected_identity(product):
        raise ValueError("OBSERVED_PAGE_LISTING_MISMATCH")
    if url_identity(get("original_url")) != expected_identity(product):
        raise ValueError("LISTING_URL_BINDING_MISMATCH")
    selector_proven = validate_target(
        get("original_url"), expected_identity(product), product.variant_id
    )
    if not product.variant_id and not selector_proven:
        raise ValueError("WHOLE_LISTING_URL_REQUIRED")
    observed = datetime.fromisoformat(get("observed_at").replace("Z", "+00:00"))
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("OBSERVATION_TIMEZONE_REQUIRED")
    observed = aware_utc(observed)
    if not now - timedelta(seconds=600) < observed <= data.exported_at <= now:
        raise ValueError("STALE_OR_FUTURE_COMMERCIAL_OBSERVATION")
    try:
        if get("currency") != "BRL":
            raise ValueError("BRL_CURRENCY_REQUIRED")
        if not re.fullmatch(
            r"[0-9]+(?:" + re.escape(data.decimal_separator) + r"[0-9]{1,2})?", get("public_price")
        ):
            raise ValueError("INVALID_OR_AMBIGUOUS_PRICE")
        price = Decimal(get("public_price").replace(data.decimal_separator, "."))
        if not Decimal("0") < price <= Decimal("9999999999.99"):
            raise ValueError("INVALID_OR_AMBIGUOUS_PRICE")
        if get("availability") != data.available_value:
            raise ValueError("AVAILABLE_REQUIRED")
        if get("price_access") != data.public_value:
            raise ValueError("PUBLIC_PRICE_REQUIRED")
        if require_link and not has_own_affiliate_link(product, require_verified=True):
            raise ValueError("VERIFIED_OWN_AFFILIATE_LINK_REQUIRED")
        proof = _proof(session, product, data, observed, price, row, now)
        if not proof.accepts_observation_name(product.name, get("name")):
            raise ValueError("UNDOCUMENTED_PRODUCT_NAME")
        proof.validate_commercial(price, observed)
        return product, proof, price, observed
    except (ValueError, IntegrationError) as error:
        raise BoundCommercialRowError(
            getattr(error, "code", str(error)), product.id, observed
        ) from None


def process_export(session, settings, data, *, persist=False, now=None):
    """Caller owns transaction. Preview never writes; commit uses append-only evidence.

    Repeated files return existing IDs after revalidation, not new verifications.
    A changed mapping for an already imported file is rejected.
    """
    data = OfficialExportInput.model_validate(data)
    now = aware_utc(now or datetime.now(UTC))
    rows, binding = _document(data, now)
    confirmed = data.confirmed_official_document and data.confirmed_column_semantics
    if persist and (not confirmed or not data.confirmed_commercial_import):
        raise ValueError(
            "Confirme origem, semântica e autorização de cadastro comercial deste arquivo."
        )
    key = PREFIX + data.content_sha256
    previous = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if previous is not None:
        if previous.value["binding_sha256"] != binding:
            raise ValueError("Mesmo arquivo já importado com outro vínculo/mapeamento.")
        result = dict(previous.value["result"], replay=True)
        result["currently_valid_offer_ids"] = []
        for offer_id in result["created_offer_ids"]:
            try:
                read_manual_offer(session, offer_id, settings, now)
                result["currently_valid_offer_ids"].append(offer_id)
            except (IntegrationError, ValueError):
                pass
        return result
    if persist:
        # Serialize by file before appending any offers, including concurrent imports.
        session.execute(
            conflict_insert(session, AppSetting)
            .values(key=key, value={"binding_sha256": binding, "result": None})
            .on_conflict_do_nothing(index_elements=[AppSetting.key])
        )
        record = session.scalar(select(AppSetting).where(AppSetting.key == key).with_for_update())
        if record.value["result"] is not None:
            return process_export(session, settings, data, persist=True, now=now)
    result = dict(
        content_sha256=data.content_sha256,
        binding_sha256=binding,
        official_origin_is_responsible_attestation=True,
        confirmed=confirmed,
        replay=False,
        rows=[],
        created_offer_ids=[],
        external_http_calls=0,
    )
    identities = [
        (
            r.get(data.columns.get("shop_id", "")),
            r.get(data.columns.get("item_id", "")),
            r.get(data.columns.get("variant_id", ""), ""),
        )
        for r in rows
    ]
    for index, row in enumerate(rows, start=2):
        try:
            with session.begin_nested():
                product, proof, price, observed = _row(session, data, row, now)
                if identities.count(identities[index - 2]) > 1:
                    raise BoundCommercialRowError(
                        "DUPLICATE_OR_CONFLICTING_PRODUCT_ROW", product.id, observed
                    )
                outcome = dict(
                    csv_line=index,
                    product_id=product.id,
                    identity=product.external_id,
                    identity_level=proof.identity_level,
                    state="READY" if confirmed else "DOCUMENT_CONFIRMATION_REQUIRED",
                    observed_at=observed.isoformat(),
                    expires_at=(observed + timedelta(seconds=600)).isoformat(),
                )
                if persist:
                    if not _record_current(session, data, product.id, observed, row, "CURRENT"):
                        raise ValueError("OLDER_THAN_LATEST_CONFIRMED_EXPORT_OBSERVATION")
                    bundle = create_manual_offer(
                        session,
                        settings,
                        product_id=product.id,
                        current_price=price,
                        actor=data.actor,
                        verified=True,
                        availability="AVAILABLE",
                        price_access="PUBLIC",
                        verified_at=observed,
                        expires_at=observed + timedelta(seconds=600),
                        now=now,
                        documented_identity=proof.model_dump(mode="json"),
                    )
                    bundle.history.source_reference = data.source_reference
                    bundle.history.evidence_metadata = dict(
                        bundle.history.evidence_metadata or {},
                        official_export=dict(
                            content_sha256=data.content_sha256,
                            binding_sha256=binding,
                            row_sha256=fingerprint(row),
                            csv_line=index,
                            source_url=data.source_url,
                            account_reference=data.account_reference,
                            exported_at=data.exported_at.isoformat(),
                            column_semantics_reference=data.column_semantics_reference,
                            confirmed_by=data.actor,
                            document_kind=data.document_kind,
                            observation_source_sha256=data.observation_source_sha256,
                        ),
                    )
                    outcome.update(state="IMPORTED", offer_id=bundle.offer.id)
                    result["created_offer_ids"].append(bundle.offer.id)
                result["rows"].append(outcome)
        except (ValueError, IntegrationError, TypeError) as error:
            if persist and isinstance(error, BoundCommercialRowError):
                _record_current(session, data, error.product_id, error.observed_at, row, "BLOCKED")
            result["rows"].append(
                dict(csv_line=index, state="BLOCKED", reason=getattr(error, "code", str(error)))
            )
    result["ready_or_imported"] = sum(
        row["state"] in {"READY", "IMPORTED"} for row in result["rows"]
    )
    result["blocked"] = sum(row["state"] == "BLOCKED" for row in result["rows"])
    if persist:
        record.value = dict(binding_sha256=binding, result=result)
        session.flush()
    return result
