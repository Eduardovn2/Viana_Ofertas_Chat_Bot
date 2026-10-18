"""Persisted state values shared by the API and domain services."""

from enum import StrEnum


class Platform(StrEnum):
    MERCADO_LIVRE = "MERCADO_LIVRE"
    SHOPEE = "SHOPEE"


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class CategorySource(StrEnum):
    STORE = "store"
    RULE = "rule"
    MANUAL = "manual"


class CategoryMode(StrEnum):
    UNCONFIGURED = "unconfigured"
    ALL = "all"
    SELECTED = "selected"


class PriceAccess(StrEnum):
    PUBLIC = "PUBLIC"
    EXCLUSIVE = "EXCLUSIVE"
    CONDITIONAL = "CONDITIONAL"
    UNKNOWN = "UNKNOWN"


class SourceType(StrEnum):
    OFFICIAL_API = "OFFICIAL_API"
    MANUAL = "MANUAL"
    SIMULATED = "SIMULATED"
    UNKNOWN = "UNKNOWN"


class CouponEvidenceType(StrEnum):
    PERCENT = "PERCENT"
    FIXED = "FIXED"
    FREE_SHIPPING = "FREE_SHIPPING"
    UNKNOWN = "UNKNOWN"


class CouponDiscountType(StrEnum):
    PERCENTAGE = "PERCENTAGE"
    FIXED = "FIXED"


class CouponValidationState(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNVERIFIED = "UNVERIFIED"
    INVALID = "INVALID"


class OfferStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    READY = "READY"
    QUEUED = "QUEUED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING = "PENDING"
    FAILED = "FAILED"


class DestinationChannel(StrEnum):
    TELEGRAM = "TELEGRAM"
    WHATSAPP = "WHATSAPP"
    SIMULATED = "SIMULATED"


class QueueStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class PublicationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class MonitoringJobStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
