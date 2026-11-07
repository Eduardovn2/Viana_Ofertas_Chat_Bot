"""Normalized contracts. Unknown information is never replaced with invented data."""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.integrations.base import PriceSnapshot
from app.models import Platform


class CapabilityStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    REQUIRES_CREDENTIALS = "REQUIRES_CREDENTIALS"
    UNAVAILABLE_FOR_ACCOUNT = "UNAVAILABLE_FOR_ACCOUNT"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class Capability:
    name: str
    status: CapabilityStatus
    reason: str


@dataclass(frozen=True)
class ProductRef:
    platform: Platform
    external_item_id: str
    canonical_url: str
    variant_id: str = ""


class PriceContext(BaseModel):
    """Stable quote identity; amounts, timestamps and commissions cannot enter it."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    origin: str = Field(min_length=1, max_length=100)
    operation: str = Field(min_length=1, max_length=100)
    product_identity: str = Field(min_length=1, max_length=100)
    variant_identity: str = Field(default="UNKNOWN", min_length=1, max_length=100)
    price_kind: str = Field(min_length=1, max_length=100)
    payment: str = Field(default="UNKNOWN", min_length=1, max_length=100)
    coupon: str = Field(default="UNKNOWN", min_length=1, max_length=100)
    eligibility: str = Field(default="UNKNOWN", min_length=1, max_length=100)
    region: str = Field(default="UNKNOWN", min_length=1, max_length=100)

    @field_validator("*")
    @classmethod
    def clean_token(cls, value):
        if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Contexto de preço inválido.")
        return value

    @property
    def price_basis(self) -> str:
        return "v1:" + fingerprint(self.model_dump())


@dataclass(frozen=True, kw_only=True)
class ProductSnapshot(PriceSnapshot):
    platform: Platform
    external_item_id: str
    canonical_url: str
    name: str
    variant_id: str = ""
    category_external_id: str | None = None
    category_external_name: str | None = None
    image_url: str | None = None
    seller_id: str | None = None
    reported_price_min: Decimal | None = None
    reported_price_max: Decimal | None = None


@dataclass(frozen=True)
class DiscoveryPage:
    items: list[ProductSnapshot]
    page: int
    has_next: bool


@dataclass(frozen=True)
class AffiliateLinkResult:
    url: str
    source: str
    account_verified: bool
    # Identity is not evidence of affiliate ownership or conversion attribution.
    product_identity_verified: bool = False
    # Preserve exact provider tracking data internally, never in implicit logs.
    long_url: str | None = field(default=None, repr=False)


class MarketplaceConnector(Protocol):
    platform: Platform
    enabled: bool
    credentials_configured: bool

    def capabilities(self) -> list[Capability]: ...
    def fetch_product(self, ref: ProductRef) -> ProductSnapshot: ...
    def discover_offers(self, query: str, **kwargs) -> DiscoveryPage: ...
    def fetch_coupons(self, ref: ProductRef) -> list: ...
    def generate_affiliate_link(self, url: str) -> AffiliateLinkResult: ...


def fingerprint(data: dict) -> str:
    """Hash normalized public evidence, never authentication or full HTTP responses."""
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()
