"""Simulated monitoring with durable scheduling, throttling and fenced leases."""

import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import RLock

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.base import PriceProvider
from app.integrations.errors import IntegrationError
from app.integrations.mock import SimulatedPriceProvider
from app.integrations.registry import ConnectorRegistry
from app.models import (
    IntegrationEvent,
    MonitoringJob,
    MonitoringJobStatus,
    Platform,
    Product,
    ProductPriceHistory,
)
from app.services.configuration import load_monitoring_settings
from app.services.leases import (
    Clock,
    SessionFactory,
    acquire_lease,
    conflict_insert,
    lease_expires_at,
    release_lease,
    renew_lease,
    utc_now,
)
from app.services.monitoring import collect_price

logger = logging.getLogger("ofertas_bot")


class MonitoringBusyError(RuntimeError):
    """Another execution owns the product, or this execution lost its lease."""


class ProviderRateLimitedError(RuntimeError):
    """The platform request slot is occupied; retry without sleeping in a worker."""

    def __init__(self, retry_at: datetime):
        super().__init__("Intervalo mínimo de consultas da plataforma ainda não decorrido")
        self.retry_at = retry_at


class MonitoringNotDueError(RuntimeError):
    """Internal control flow when a second scheduler observes an already-run job."""


def _threads_blocked(settings: Settings) -> bool:
    return settings.app_env == "test" or "pytest" in sys.modules


class MonitoringRuntime:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        provider_factory: Callable[[Product], PriceProvider] | None = None,
        clock: Clock | None = None,
        connector_registry: ConnectorRegistry | None = None,
    ) -> None:
        # No database connection or background thread is created here.
        self.session_factory = session_factory
        self.settings = settings
        self.provider_factory = provider_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.connectors = connector_registry or ConnectorRegistry(
            settings, session_factory=session_factory, clock=self.clock
        )
        self._scheduler: BackgroundScheduler | None = None
        self._paused = not settings.scheduler_enabled
        self._effective_enabled = settings.scheduler_enabled
        self._last_error: str | None = None
        self._tick_failures = 0
        self._next_tick_at: datetime | None = None
        self._state_lock = RLock()

    def _interval(self, product: Product, settings: Settings) -> int:
        return (
            settings.priority_interval_seconds
            if product.priority
            else settings.general_interval_seconds
        )

    def _is_due(self, job: MonitoringJob | None, interval: int, now: datetime) -> bool:
        if job is None:
            return True
        due_at = job.next_run_at
        if (
            job.interval_seconds != interval
            and job.failure_count == 0
            and job.last_run_at is not None
        ):
            due_at = job.last_run_at + timedelta(seconds=interval)
        return due_at is None or due_at <= now

    def _product(self, session: Session, product_id: int) -> Product:
        product = session.scalar(select(Product).where(Product.id == product_id).with_for_update())
        if product is None:
            raise LookupError("Produto não encontrado")
        if not product.monitoring_enabled:
            raise ValueError("O monitoramento deste produto está desativado")
        return product

    def _job(self, session: Session, product: Product, interval: int) -> MonitoringJob:
        session.execute(
            conflict_insert(session, MonitoringJob)
            .values(
                product_id=product.id,
                enabled=product.monitoring_enabled,
                interval_seconds=interval,
                status=MonitoringJobStatus.IDLE,
                failure_count=0,
            )
            .on_conflict_do_nothing(index_elements=[MonitoringJob.product_id])
        )
        return session.scalars(
            select(MonitoringJob).where(MonitoringJob.product_id == product.id).with_for_update()
        ).one()

    def _fence(self, session: Session, name: str, owner: str) -> None:
        if not renew_lease(
            session, name, owner, self.settings.monitoring_lease_seconds, self.clock
        ):
            raise MonitoringBusyError("A reserva de execução expirou ou mudou de proprietário")

    def _settings(self, session: Session) -> Settings:
        effective = load_monitoring_settings(session, self.settings)
        if effective.scheduler_enabled != self._effective_enabled:
            # Polling continues while paused, so another process can resume this instance.
            self._paused = not effective.scheduler_enabled
        self._effective_enabled = effective.scheduler_enabled
        return effective

    def _provider(
        self, product: Product, settings: Settings, *, real: bool = False, check: bool = False
    ) -> PriceProvider:
        if real or settings.price_provider == "marketplaces":
            return self.connectors.provider(product, allow_blocked=check)
        if self.provider_factory is not None:
            return self.provider_factory(product)
        return SimulatedPriceProvider(
            current_price=settings.mock_current_price,
            previous_price=settings.mock_previous_price,
            availability=settings.mock_availability,
            price_access=settings.mock_price_access,
            clock=self.clock,
        )

    def _preflight(self, product_id: int, manual: bool) -> tuple[Settings, str]:
        with self.session_factory() as session:
            effective = self._settings(session)
            product = self._product(session, product_id)
            if not manual and (self._paused or not effective.scheduler_enabled):
                raise MonitoringNotDueError("Monitoramento automático pausado")
            job = session.scalar(
                select(MonitoringJob).where(MonitoringJob.product_id == product_id)
            )
            if not manual and not self._is_due(
                job, self._interval(product, effective), utc_now(self.clock)
            ):
                raise MonitoringNotDueError("Monitoramento ainda não está previsto")
            return effective, product.platform.value

    def _defer(self, product_id: int, name: str, owner: str, retry_at: datetime) -> None:
        with self.session_factory() as session:
            product = self._product(session, product_id)
            settings = self._settings(session)
            interval = self._interval(product, settings)
            job = self._job(session, product, interval)
            job.enabled = True
            job.interval_seconds = interval
            job.status = MonitoringJobStatus.IDLE
            next_run = max(retry_at, utc_now(self.clock) + timedelta(seconds=1))
            job.next_run_at = max(next_run, job.next_run_at) if job.next_run_at else next_run
            job.last_error = ProviderRateLimitedError.__name__
            self._fence(session, name, owner)
            session.commit()

    def _mark_running(self, product_id: int, name: str, owner: str, manual: bool) -> None:
        with self.session_factory() as session:
            product = self._product(session, product_id)
            settings = self._settings(session)
            if not manual and (self._paused or not settings.scheduler_enabled):
                raise MonitoringNotDueError("Monitoramento automático pausado")
            interval = self._interval(product, settings)
            job = self._job(session, product, interval)
            now = utc_now(self.clock)
            # This second check is inside the product lease, immediately before claiming work.
            if not manual and not self._is_due(job, interval, now):
                raise MonitoringNotDueError("Outro processo já executou este monitoramento")
            job.enabled = True
            job.interval_seconds = interval
            job.status = MonitoringJobStatus.RUNNING
            job.last_run_at = now
            job.next_run_at = now + timedelta(seconds=interval)
            job.last_error = None
            self._fence(session, name, owner)
            session.commit()

    def _record_failure(self, product_id: int, name: str, owner: str, error: Exception) -> None:
        try:
            with self.session_factory() as session:
                product = session.scalar(
                    select(Product).where(Product.id == product_id).with_for_update()
                )
                if product is None:
                    return
                settings = self._settings(session)
                interval = self._interval(product, settings)
                job = self._job(session, product, interval)
                job.failure_count += 1
                job.status = MonitoringJobStatus.FAILED
                job.last_error = type(error).__name__[:100]
                if isinstance(error, IntegrationError):
                    session.add(
                        IntegrationEvent(
                            product_id=product.id,
                            platform=product.platform,
                            operation="fetch_product",
                            status="FAILED",
                            error_code=error.code,
                            observed_at=utc_now(self.clock),
                        )
                    )
                job.enabled = product.monitoring_enabled
                delay = min(
                    settings.monitoring_backoff_max_seconds,
                    interval * (2 ** min(job.failure_count - 1, 30)),
                )
                job.next_run_at = utc_now(self.clock) + timedelta(seconds=delay)
                self._fence(session, name, owner)
                session.commit()
        except (MonitoringBusyError, SQLAlchemyError):
            logger.warning(
                "monitoring_failure_record_unavailable", extra={"product_id": product_id}
            )

    def run_product(
        self, product_id: int, *, manual: bool = True, real: bool = False, check: bool = False
    ) -> ProductPriceHistory:
        name = f"monitor:product:{product_id}"
        owner = acquire_lease(
            self.session_factory, name, self.settings.monitoring_lease_seconds, self.clock
        )
        if owner is None:
            raise MonitoringBusyError("Já existe monitoramento em execução para este produto")
        started = False
        try:
            effective, platform = self._preflight(product_id, manual)
            minimum_interval = float(effective.provider_min_interval_seconds)
            if minimum_interval > 0:
                platform_name = f"provider:{platform}"
                platform_owner = acquire_lease(
                    self.session_factory, platform_name, minimum_interval, self.clock
                )
                # The platform slot remains occupied until its interval expires, even on error.
                if platform_owner is None:
                    retry_at = lease_expires_at(self.session_factory, platform_name)
                    retry_at = retry_at or (
                        utc_now(self.clock) + timedelta(seconds=minimum_interval)
                    )
                    self._defer(product_id, name, owner, retry_at)
                    raise ProviderRateLimitedError(retry_at)
            self._mark_running(product_id, name, owner, manual)
            started = True
            with self.session_factory() as session:
                session.expire_on_commit = False
                effective = self._settings(session)
                product = self._product(session, product_id)
                job = session.scalars(
                    select(MonitoringJob).where(MonitoringJob.product_id == product_id)
                ).one()
                history = collect_price(
                    session,
                    product,
                    self._provider(product, effective, real=real, check=check),
                    now=utc_now(self.clock),
                )
                if not history.is_simulated:
                    partial = (
                        history.evidence_metadata.get("price_access_reason")
                        == "price_permission_denied"
                    )
                    session.add(
                        IntegrationEvent(
                            product_id=product.id,
                            platform=product.platform,
                            operation="fetch_product",
                            status="PARTIAL" if partial else "SUCCEEDED",
                            error_code="PRICE_PERMISSION_DENIED" if partial else None,
                            observed_at=utc_now(self.clock),
                            source_reference=history.source_reference,
                        )
                    )
                # Delayed import keeps construction independent from the offer implementation.
                from app.services.offers import OfferEngine

                OfferEngine(effective, clock=self.clock).process(session, product, history)
                job.status = MonitoringJobStatus.SUCCEEDED
                job.failure_count = 0
                job.last_error = None
                job.interval_seconds = self._interval(product, effective)
                job.next_run_at = utc_now(self.clock) + timedelta(seconds=job.interval_seconds)
                # A provider that outlived its lease must roll back history, offers and job state.
                self._fence(session, name, owner)
                session.commit()
                session.expunge(history)
                logger.info("monitoring_succeeded", extra={"product_id": product_id})
                return history
        except Exception as exc:
            if isinstance(exc, IntegrationError) and exc.code == "AUTHORIZATION_DENIED":
                with self.session_factory() as session:
                    platform = session.scalar(
                        select(Product.platform).where(Product.id == product_id)
                    )
                if platform is not None:
                    self.connectors.block(platform)
            if started:
                self._record_failure(product_id, name, owner, exc)
            raise
        finally:
            try:
                release_lease(self.session_factory, name, owner)
            except SQLAlchemyError:
                logger.warning(
                    "monitoring_lease_release_unavailable", extra={"product_id": product_id}
                )

    def run_due(self) -> dict[str, int]:
        result = {"attempted": 0, "succeeded": 0, "deferred": 0, "failed": 0}
        with self.session_factory() as session:
            effective = self._settings(session)
            if self._paused or not effective.scheduler_enabled:
                return result
            now = utc_now(self.clock)
            rows = session.execute(
                select(Product, MonitoringJob)
                .outerjoin(MonitoringJob, MonitoringJob.product_id == Product.id)
                .where(Product.monitoring_enabled.is_(True))
                .order_by(Product.priority.desc(), Product.id)
            ).all()
            product_ids = [
                product.id
                for product, job in rows
                if self._is_due(job, self._interval(product, effective), now)
                and (
                    effective.price_provider == "simulated"
                    or self.connectors.enabled(product.platform)
                )
            ]
        for product_id in product_ids:
            result["attempted"] += 1
            try:
                self.run_product(product_id, manual=False)
                result["succeeded"] += 1
            except (MonitoringBusyError, ProviderRateLimitedError, MonitoringNotDueError):
                result["deferred"] += 1
            except SQLAlchemyError:
                raise
            except Exception as exc:
                result["failed"] += 1
                logger.warning(
                    "monitoring_product_failed",
                    extra={"product_id": product_id, "error_type": type(exc).__name__},
                )
        self._discover_due(effective)
        return result

    def _discover_due(self, settings: Settings):
        if not (
            settings.price_provider == "marketplaces"
            and settings.shopee_discovery_enabled
            and settings.shopee_discovery_query
            and self.connectors.enabled(Platform.SHOPEE)
        ):
            return
        # Keep the slot until the next interval, including failures/restarts. No catch-up loop.
        owner = acquire_lease(
            self.session_factory, "discovery:SHOPEE", settings.general_interval_seconds, self.clock
        )
        if owner is None:
            return
        try:
            page = self.connectors.get(Platform.SHOPEE).discover_offers(
                settings.shopee_discovery_query, page=1, limit=10, category=None
            )
            from app.services.marketplaces import ingest_discovery

            with self.session_factory() as session:
                ingest_discovery(session, page, Platform.SHOPEE, settings, clock=self.clock)
                session.commit()
        except IntegrationError as error:
            if error.code == "AUTHORIZATION_DENIED":
                self.connectors.block(Platform.SHOPEE)
            with self.session_factory() as session:
                session.add(
                    IntegrationEvent(
                        platform=Platform.SHOPEE,
                        operation="discover_offers",
                        status="FAILED",
                        error_code=error.code,
                        observed_at=utc_now(self.clock),
                    )
                )
                session.commit()

    def _tick(self) -> None:
        now = utc_now(self.clock)
        if self._next_tick_at is not None and now < self._next_tick_at:
            return
        try:
            self.run_due()
        except Exception as exc:
            self._tick_failures += 1
            self._last_error = type(exc).__name__[:100]
            delay = min(
                self.settings.monitoring_backoff_max_seconds,
                2 ** min(self._tick_failures - 1, 30),
            )
            self._next_tick_at = now + timedelta(seconds=delay)
            logger.warning("monitoring_tick_failed", extra={"error_type": self._last_error})
        else:
            self._tick_failures = 0
            self._last_error = None
            self._next_tick_at = now + timedelta(seconds=self.settings.scheduler_tick_seconds)

    def start(self) -> dict:
        with self._state_lock:
            if _threads_blocked(self.settings):
                return self.status()
            if self._scheduler is None:
                self._scheduler = BackgroundScheduler(timezone="UTC")
                self._scheduler.add_job(
                    self._tick,
                    "interval",
                    seconds=self.settings.scheduler_tick_seconds,
                    id="monitoring_tick",
                    max_instances=1,
                    coalesce=True,
                    replace_existing=True,
                )
                self._scheduler.start()
            return self.status()

    def pause(self) -> dict:
        with self._state_lock:
            self._paused = True
            self._effective_enabled = False
            return self.status()

    def resume(self) -> dict:
        with self._state_lock:
            self._paused = False
            self._effective_enabled = True
            self._next_tick_at = None
            if _threads_blocked(self.settings):
                return self.status()
            if self._scheduler is None:
                return self.start()
            self._scheduler.resume()
            return self.status()

    def shutdown(self, wait: bool = True) -> dict:
        with self._state_lock:
            if self._scheduler is not None:
                self._scheduler.shutdown(wait=wait)
                self._scheduler = None
            self.connectors.close()
            return self.status()

    def status(self) -> dict:
        return {
            "running": bool(
                self._scheduler is not None and self._scheduler.running and not self._paused
            ),
            "paused": self._paused or not self._effective_enabled,
            "enabled": self._effective_enabled,
            "threads_blocked": _threads_blocked(self.settings),
            "last_error": self._last_error,
            "next_tick_at": self._next_tick_at,
        }
