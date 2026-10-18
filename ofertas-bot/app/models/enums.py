"""Persisted state values shared by the API and domain services."""

from enum import StrEnum


class Platform(StrEnum):
    MERCADO_LIVRE = "MERCADO_LIVRE"
    SHOPEE = "SHOPEE"


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class PriceAccess(StrEnum):
    PUBLIC = "PUBLIC"
    EXCLUSIVE = "EXCLUSIVE"


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


class DestinationChannel(StrEnum):
    TELEGRAM = "TELEGRAM"
    WHATSAPP = "WHATSAPP"


class PublicationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class MonitoringJobStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
