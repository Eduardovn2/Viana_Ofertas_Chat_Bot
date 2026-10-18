"""Normalized contracts. Unknown information is never replaced with invented data."""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

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
