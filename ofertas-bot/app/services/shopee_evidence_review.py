"""Administrative evidence drafts tied to saved observations. No HTTP or authorization dispatch."""

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError
from app.integrations.shopee import _positive_id
from app.models import AppSetting, IntegrationEvent, Platform
from app.services.comparison import aware_utc
from app.services.leases import conflict_insert
from app.services.shopee_classification import documented_title_conflicts
from app.services.shopee_multistore import ProductFacts, ProductTarget, ShopeeMultistoreAdapter
from app.services.shopee_replay import analyze_recorded_search

PREFIX = "shopee_evidence_review:"


class EvidencePatch(ProductTarget):
    brand: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    quantity: int | None = Field(default=None, strict=True, ge=1, le=10000)


class ReviewEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    expected_revision: int = Field(strict=True, ge=0)
    observation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    origin: Literal[
        "AFFILIATE_PORTAL_EXPORT", "OFFICIAL_MANUFACTURER_DOCUMENT", "MANUAL_LISTING_OBSERVATION"
    ]
    reference: str = Field(min_length=3, max_length=500)
    source_observed_at: datetime
    source_excerpt: str = Field(min_length=10, max_length=4000)
    binding_note: str = Field(min_length=10, max_length=1000)
    facts: EvidencePatch

    @field_validator("reference", "source_excerpt", "binding_note")
    @classmethod
    def meaningful(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError("Referência, trecho e vínculo devem conter texto válido.")
        return value.strip()

    @field_validator("source_observed_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)


class ReviewConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    expected_revision: int = Field(strict=True, ge=1)
    confirmed_listing_binding: StrictBool

    @field_validator("confirmed_listing_binding")
    @classmethod
    def explicit(cls, value):
        if not value:
            raise ValueError("Confirme o vínculo documental com este anúncio e opção.")
        return value


class ShopeeEvidenceReview:
    def __init__(self, collection):
        self.collection = collection
        self.factory, self.clock = collection.factory, collection.clock

    @staticmethod
    def key(authorization_id, shop_id, item_id):
        try:
            return (
                PREFIX
                + str(UUID(str(authorization_id)))
                + ":"
                + _positive_id(shop_id)
                + ":"
                + _positive_id(item_id)
            )
        except IntegrationError:
            raise ValueError("Identidade de anúncio inválida.") from None

    def _observation(self, authorization_id, shop_id, item_id):
        report = ShopeeMultistoreAdapter(self.collection).read(authorization_id)
        analysis = analyze_recorded_search(report)
        identity = f"{shop_id}:{item_id}"
        row = next((r for r in analysis["candidates"] if r["identity"] == identity), None)
        if row is None:
            raise LookupError("Anúncio não consta no resultado gravado.")
        return row, analysis["source_comparison_sha256"]

    def _source_still_current(self, session, authorization_id, comparison_hash):
        value = session.scalar(
            select(AppSetting.value)
            .where(AppSetting.key == ShopeeMultistoreAdapter.key(authorization_id))
            .with_for_update()
        )
        if (
            not value
            or value["state"] != "SUCCEEDED"
            or fingerprint(value["result"].get("comparison", {})) != comparison_hash
        ):
            raise ValueError("Resultado de origem mudou; recarregue a observação.")

    @staticmethod
    def _readiness(value, row):
        try:
            facts = ProductFacts.model_validate(value.get("facts", {}))
        except ValidationError as error:
            return {
                "ready": False,
                "missing_or_invalid_fields": sorted({str(e["loc"][0]) for e in error.errors()}),
            }
        issues = documented_title_conflicts(row["name"], facts)
        return {"ready": not issues, "review_issues": issues}

    def read(self, authorization_id, shop_id, item_id):
        key = self.key(authorization_id, shop_id, item_id)
        row, comparison_hash = self._observation(authorization_id, shop_id, item_id)
        with self.factory() as session:
            value = session.scalar(select(AppSetting.value).where(AppSetting.key == key))
        value = value or dict(state="NOT_STARTED", revision=0, facts={}, field_sources={})
        return dict(
            **value,
            identity=row["identity"],
            current_observation_fingerprint=row["observation_fingerprint"],
            source_current=(
                value.get("source_comparison_sha256", comparison_hash) == comparison_hash
                and value.get("observation_fingerprint", row["observation_fingerprint"])
                == row["observation_fingerprint"]
            ),
            readiness=self._readiness(value, row),
            publication_authorized=False,
            external_http_calls=0,
        )

    def _audit(self, session, key, status):
        session.add(
            IntegrationEvent(
                platform=Platform.SHOPEE,
                operation="multistore_evidence_review",
                status=status,
                source_reference=key,
                observed_at=self.clock(),
            )
        )

    def edit(self, authorization_id, shop_id, item_id, data):
        edit = ReviewEdit.model_validate(data)
        key = self.key(authorization_id, shop_id, item_id)
        row, comparison_hash = self._observation(authorization_id, shop_id, item_id)
        now = aware_utc(self.clock())
        if (
            row["observation_fingerprint"] != edit.observation_fingerprint
            or edit.source_observed_at > now
            or row["observed_at"] > now
        ):
            raise ValueError("Fingerprint divergente ou data futura.")
        for secret in (
            self.collection.settings.shopee_app_id,
            self.collection.settings.shopee_secret,
            self.collection.settings.admin_api_key,
        ):
            if (
                secret
                and secret.get_secret_value()
                and any(
                    secret.get_secret_value() in text
                    for text in (edit.reference, edit.source_excerpt, edit.binding_note)
                )
            ):
                raise ValueError("Não inclua credenciais nos documentos de revisão.")
        patch = edit.facts.model_dump(exclude_unset=True)
        if edit.origin == "OFFICIAL_MANUFACTURER_DOCUMENT" and any(
            patch.get(k) is not None for k in ("quantity", "condition", "price_conditions")
        ):
            raise ValueError(
                "Documento do fabricante não comprova quantidade vendida, condição ou termos da loja."
            )
        source = dict(
            origin=edit.origin,
            reference=edit.reference,
            source_observed_at=edit.source_observed_at.isoformat(),
            source_excerpt=edit.source_excerpt,
            binding_note=edit.binding_note,
            actor=edit.actor,
            source_fingerprint=hashlib.sha256(edit.source_excerpt.encode()).hexdigest(),
            fingerprint_kind="SHA256_PROVIDED_EXCERPT_NOT_FETCHED_DOCUMENT",
        )
        with self.factory() as session:
            self._source_still_current(session, authorization_id, comparison_hash)
            session.execute(
                conflict_insert(session, AppSetting)
                .values(
                    key=key,
                    value={"revision": 0, "state": "DRAFT", "facts": {}, "field_sources": {}},
                    description="Evidências documentais; não autoriza execução ou publicação.",
                )
                .on_conflict_do_nothing(index_elements=[AppSetting.key])
            )
            record = session.scalar(
                select(AppSetting)
                .where(AppSetting.key == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            old = record.value
            if old["revision"] != edit.expected_revision:
                raise ValueError("Conflito de revisão; recarregue antes de editar.")
            if (
                old.get("observation_fingerprint", edit.observation_fingerprint)
                != edit.observation_fingerprint
                or old.get("source_comparison_sha256", comparison_hash) != comparison_hash
            ):
                raise ValueError("Revisão vinculada a outra observação.")
            facts, sources = dict(old["facts"]), dict(old["field_sources"])
            for field, fact in patch.items():
                if fact is None:
                    facts.pop(field, None)
                    sources.pop(field, None)
                else:
                    facts[field], sources[field] = fact, source
            previous = list(old.get("previous_documentations", []))
            if old.get("state") == "DOCUMENTED":
                previous.append(dict(proof=old["proof"], evidence_sha256=old["evidence_sha256"]))
            record.value = dict(
                state="DRAFT",
                revision=old["revision"] + 1,
                facts=facts,
                field_sources=sources,
                observation_fingerprint=edit.observation_fingerprint,
                source_comparison_sha256=comparison_hash,
                updated_at=now.isoformat(),
                updated_by=edit.actor,
                previous_documentations=previous,
            )
            self._audit(session, key, "DRAFT")
            session.commit()
        return self.read(authorization_id, shop_id, item_id)

    def confirm(self, authorization_id, shop_id, item_id, data):
        confirmation = ReviewConfirm.model_validate(data)
        key = self.key(authorization_id, shop_id, item_id)
        row, comparison_hash = self._observation(authorization_id, shop_id, item_id)
        with self.factory() as session:
            self._source_still_current(session, authorization_id, comparison_hash)
            record = session.scalar(
                select(AppSetting)
                .where(AppSetting.key == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if record is None:
                raise LookupError("Revisão documental inexistente.")
            value = record.value
            if value["revision"] != confirmation.expected_revision:
                raise ValueError("Conflito de revisão; recarregue antes de confirmar.")
            if (
                value["observation_fingerprint"] != row["observation_fingerprint"]
                or value["source_comparison_sha256"] != comparison_hash
            ):
                raise ValueError("Revisão documental desatualizada.")
            if (
                not self._readiness(value, row)["ready"]
                or aware_utc(self.clock()) < row["observed_at"]
            ):
                raise ValueError(
                    "Faltam atributos ou há conflitos documentais; complete o rascunho."
                )
            if value["state"] != "DOCUMENTED":
                proof = dict(
                    identity=row["identity"],
                    actor=confirmation.actor,
                    confirmed_at=aware_utc(self.clock()).isoformat(),
                    observation_fingerprint=row["observation_fingerprint"],
                    source_comparison_sha256=comparison_hash,
                    facts=ProductFacts.model_validate(value["facts"]).model_dump(mode="json"),
                    field_sources=value["field_sources"],
                )
                record.value = {
                    **value,
                    "state": "DOCUMENTED",
                    "revision": value["revision"] + 1,
                    "proof": proof,
                    "evidence_sha256": fingerprint(proof),
                }
                self._audit(session, key, "DOCUMENTED")
                session.commit()
        return self.read(authorization_id, shop_id, item_id)

    def records(self, authorization_id):
        prefix = PREFIX + str(UUID(str(authorization_id))) + ":"
        with self.factory() as session:
            values = session.execute(
                select(AppSetting.key, AppSetting.value).where(AppSetting.key.startswith(prefix))
            ).all()
        return {key[len(prefix) :]: value for key, value in values}

    def bindings(self, authorization_id):
        return {
            v["proof"]["identity"]: v
            for v in self.records(authorization_id).values()
            if v.get("state") == "DOCUMENTED" and v.get("proof")
        }
