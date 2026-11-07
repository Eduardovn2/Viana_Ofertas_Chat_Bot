"""The existing APScheduler/leases/backoff runtime, restricted to enrolled fakes."""

from decimal import Decimal

from sqlalchemy import func, select

from app.integrations.mock import SimulatedPriceProvider
from app.jobs.runtime import MonitoringRuntime
from app.models import ProductPriceHistory, SimulationProduct
from app.services.simulated_automation import SimulatedAutomation
from app.services.simulation_safety import SimulationEnrollmentInput, require_simulation_scope


class SimulationMonitoringRuntime(MonitoringRuntime):
    def __init__(self, factory, settings, clock=None, pipeline=None):
        super().__init__(factory, settings, clock=clock)
        self.pipeline = pipeline or SimulatedAutomation(factory, settings, self.clock)
        self.last_simulation = None

    def _settings(self, session):
        require_simulation_scope(session, self.settings)
        return super()._settings(session)

    def _product(self, session, product_id):
        product = super()._product(session, product_id)
        if (
            session.get(SimulationProduct, product_id) is None
            or not product.name.startswith("[SIMULATED] ")
            or (product.classification_metadata or {}).get("simulation_only") is not True
        ):
            raise ValueError("Produto não inscrito como fictício.")
        return product

    def _provider(self, product, settings, *, real=False, check=False):
        if real or check:
            raise ValueError("Consultas reais proibidas no runtime simulado.")
        with self.session_factory() as session:
            require_simulation_scope(session, settings)
            profile = session.get(SimulationProduct, product.id)
            if profile is None:
                raise ValueError("Produto não inscrito.")
            prices = SimulationEnrollmentInput(prices=profile.prices).prices
            count = session.scalar(
                select(func.count())
                .select_from(ProductPriceHistory)
                .where(
                    ProductPriceHistory.product_id == product.id,
                    ProductPriceHistory.is_simulated.is_(True),
                    ProductPriceHistory.source == "simulated",
                )
            )
        price = prices[min(count, len(prices) - 1)]
        return SimulatedPriceProvider(
            current_price=Decimal(price) if price is not None else None,
            previous_price=None,
            clock=self.clock,
        )

    def run_due(self):
        if not self.settings.simulation_automation_enabled:
            return {"attempted": 0, "succeeded": 0, "deferred": 0, "failed": 0}
        with self.session_factory() as session:
            effective = self._settings(session)
            if self._paused or not effective.scheduler_enabled:
                return {"attempted": 0, "succeeded": 0, "deferred": 0, "failed": 0}
        result = super().run_due()
        self.last_simulation = self.pipeline.run_pending()
        return result

    def start(self):
        with self.session_factory() as session:
            require_simulation_scope(session, self.settings)
        if not self.settings.simulation_automation_enabled:
            return self.status()
        return super().start()

    def shutdown(self, wait=True):
        result = super().shutdown(wait=wait)
        self.pipeline.close()
        return result
