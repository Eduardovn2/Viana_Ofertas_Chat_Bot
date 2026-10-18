"""Public model and enum exports; importing this module registers all tables."""

from app.models.base import Base, UTCDateTime, utcnow
from app.models.entities import (
    AffiliateLink,
    AppSetting,
    Coupon,
    MonitoringJob,
    Offer,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationLog,
)
from app.models.enums import (
    Availability,
    CouponDiscountType,
    CouponValidationState,
    DestinationChannel,
    MonitoringJobStatus,
    OfferStatus,
    Platform,
    PriceAccess,
    PublicationStatus,
)

__all__ = [
    "AffiliateLink",
    "AppSetting",
    "Availability",
    "Base",
    "Coupon",
    "CouponDiscountType",
    "CouponValidationState",
    "DestinationChannel",
    "MonitoringJob",
    "MonitoringJobStatus",
    "Offer",
    "OfferStatus",
    "Platform",
    "PriceAccess",
    "Product",
    "ProductPriceHistory",
    "PublicationDestination",
    "PublicationLog",
    "PublicationStatus",
    "UTCDateTime",
    "utcnow",
]
