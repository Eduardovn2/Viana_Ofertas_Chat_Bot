"""A predictable provider for local development; it performs no network calls."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from app.integrations.base import PriceSnapshot
from app.models import Availability, PriceAccess, Product


class SimulatedPriceProvider:
    """Use fixed configurable prices and an injectable observation clock."""

    def __init__(
        self,
        current_price: Decimal | None = Decimal("199.90"),
        previous_price: Decimal | None = None,
        availability: Availability = Availability.AVAILABLE,
        price_access: PriceAccess = PriceAccess.PUBLIC,
        currency: str = "BRL",
        installment_text: str | None = None,
        shipping_text: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.current_price = current_price
        self.previous_price = previous_price
        self.availability = availability
        self.price_access = price_access
        self.currency = currency
        self.installment_text = installment_text
        self.shipping_text = shipping_text
        self.clock = clock or (lambda: datetime.now(UTC))

    def fetch(self, product: Product) -> PriceSnapshot:
        return PriceSnapshot(
            current_price=self.current_price,
            previous_price=self.previous_price,
            availability=self.availability,
            price_access=self.price_access,
            source="simulated",
            observed_at=self.clock(),
            is_simulated=True,
            currency=self.currency,
            installment_text=self.installment_text,
            shipping_text=self.shipping_text,
        )
