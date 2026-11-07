"""Explicit identity evidence for the existing manual-offer path; no inference or I/O."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.integrations.contracts import fingerprint
from app.models import Platform
from app.services.categories import normalize_text
from app.services.comparison import aware_utc
from app.services.shopee_classification import documented_title_conflicts
from app.services.shopee_multistore import ProductFacts, ProductTarget

CORE_CONTENT_NOTICE = "Volante, pedais, câmbio e demais equipamentos: composição não confirmada. Confira os itens incluídos no anúncio."


class DocumentedIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: str
    identity_level: Literal["VARIANT", "LISTING_ALL_OPTIONS"] = "VARIANT"
    variant_id: str = ""
    original_url_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: Literal["MANUAL_DOCUMENTED", "OFFICIAL_EXPORT"]
    actor: str = Field(min_length=3)
    reference: str = Field(min_length=3)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verified_at: datetime
    confirmed_listing_binding: Literal[True]
    facts: ProductFacts | None = None
    documented_observation_names: tuple[str, ...] = Field(default=(), max_length=10)
    confirmed_all_options_same_public_terms: Literal[True] | None = None
    all_options_public_price: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    confirmed_all_options_available: Literal[True] | None = None
    publication_name_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    message_scope: Literal["FULL_DOCUMENTED", "CORE_ATTRIBUTES_ONLY"] = "FULL_DOCUMENTED"
    core_attributes: ProductTarget | None = None

    @field_validator(
        "confirmed_all_options_same_public_terms", "confirmed_all_options_available", mode="before"
    )
    @classmethod
    def explicit_optional_confirmation(cls, value):
        if value is not None and (type(value) is not bool or value is not True):
            raise ValueError("Cobertura de todas as opções exige comprovação explícita.")
        return value

    @field_validator("verified_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Data documental exige fuso horário.")
        return value

    @field_validator("documented_observation_names")
    @classmethod
    def valid_names(cls, values):
        if any(
            not normalize_text(v) or len(v) > 500 or any(ord(c) < 32 for c in v) for v in values
        ):
            raise ValueError("Nome documental inválido.")
        return values

    @model_validator(mode="after")
    def scoped_evidence(self):
        if self.message_scope == "CORE_ATTRIBUTES_ONLY" and (
            self.publication_name_sha256 is None
            or self.facts is not None
            or self.core_attributes is not None
            and self.core_attributes.kit is not None
        ):
            raise ValueError(
                "Descrição limitada exige vínculo ao nome e não pode declarar kit desconhecido."
            )
        if self.identity_level == "VARIANT":
            attributes = (
                self.core_attributes if self.message_scope == "CORE_ATTRIBUTES_ONLY" else self.facts
            )
            if not self.variant_id.strip() or attributes is None:
                raise ValueError("Modalidade variante exige identificador e fatos documentados.")
        elif (
            self.variant_id
            or self.facts is not None
            or self.confirmed_all_options_same_public_terms is not True
            or self.confirmed_all_options_available is not True
            or self.all_options_public_price is None
            or self.publication_name_sha256 is None
        ):
            raise ValueError(
                "Anúncio sem SKU exige preço e disponibilidade comprovados para todas as opções."
            )
        return self

    @field_validator("confirmed_listing_binding", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Confirmação documental explícita obrigatória.")
        return value

    def validate_product(self, product, now):
        if self.message_scope == "CORE_ATTRIBUTES_ONLY":
            import re

            if self.publication_name_sha256 != fingerprint({"name": product.name}) or re.search(
                r"\b(volante|pedais|pedal|cambio|kit|completo|completa|inclui|incluido|acompanha)\b",
                normalize_text(product.name),
            ):
                raise ValueError(
                    "Nome da mensagem limitada contém afirmação não comprovada de composição."
                )
        if (
            product.platform != Platform.SHOPEE
            or self.identity != product.external_id
            or self.variant_id != product.variant_id
            or self.original_url_sha256 != fingerprint({"original_url": product.original_url})
            or aware_utc(self.verified_at) > aware_utc(now)
            or (self.facts is not None and documented_title_conflicts(product.name, self.facts))
            or (
                self.core_attributes is not None
                and documented_title_conflicts(product.name, self.core_attributes)
            )
            or (
                self.identity_level == "LISTING_ALL_OPTIONS"
                and self.publication_name_sha256 != fingerprint({"name": product.name})
            )
        ):
            raise ValueError("Documento não corresponde ao produto/variante atual.")
        return self

    def validate_commercial(self, price, verified_at, *, coupon_id=None, previous_price=None):
        if self.message_scope == "CORE_ATTRIBUTES_ONLY" and previous_price is not None:
            raise ValueError(
                "Descrição limitada não autoriza desconto histórico de composição desconhecida."
            )
        if self.identity_level == "LISTING_ALL_OPTIONS" and (
            Decimal(str(price)) != self.all_options_public_price
            or aware_utc(verified_at) != aware_utc(self.verified_at)
            or coupon_id is not None
            or previous_price is not None
        ):
            raise ValueError("Condições do anúncio não cobrem esta mensagem comercial.")
        return self

    def accepts_observation_name(self, product_name, observed_name):
        allowed = (product_name, *self.documented_observation_names)
        return normalize_text(observed_name) in {normalize_text(v) for v in allowed} and (
            self.facts is None or not documented_title_conflicts(observed_name, self.facts)
        )

    def duplicate_product_key(self):
        if self.message_scope == "CORE_ATTRIBUTES_ONLY":
            return fingerprint({"listing_identity": self.identity, "variant_id": self.variant_id})
        if self.facts is not None:
            return fingerprint(self.facts.model_dump(mode="json", exclude={"price_conditions"}))
        # Unknown physical equivalence never merges different sellers/products.
        return fingerprint(
            {"listing_identity": self.identity, "identity_level": self.identity_level}
        )
