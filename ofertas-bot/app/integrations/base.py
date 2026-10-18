"""The provider boundary does not assume a particular external API."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.models import Availability, PriceAccess, Product


@dataclass(frozen=True)
class PriceSnapshot:
    current_price: Decimal | None
    previous_price: Decimal | None
    availability: Availability
    price_access: PriceAccess
    source: str
    observed_at: datetime
    is_simulated: bool
    currency: str = "BRL"
    installment_text: str | None = None
    shipping_text: str | None = None


class PriceProvider(Protocol):
    def fetch(self, product: Product) -> PriceSnapshot:
        """Fetch one observation, preserving unknown and restricted prices."""
        ...
