import ipaddress
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.auto_pilot import router as auto_pilot_router
from app.api.auto_pilot_real import router as auto_pilot_real_router
from app.api.categories import router as categories_router
from app.api.integrations import router as integrations_router
from app.api.manual_offers import router as manual_offers_router
from app.api.monitoring import router as monitoring_router
from app.api.oauth import admin_router as oauth_admin_router
from app.api.oauth import callback_router as oauth_callback_router
from app.api.products import router as products_router
from app.api.shopee_collection import router as shopee_collection_router
from app.api.shopee_discovery import router as shopee_discovery_router
from app.api.simulated_automation import router as simulated_automation_router
from app.api.telegram import router as telegram_router
from app.api.telegram_technical import router as telegram_technical_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, protect_oauth_access_logs
from app.db import build_engine, build_session_factory
from app.integrations.registry import ConnectorRegistry
from app.integrations.telegram import TelegramPublisher
from app.jobs.runtime import MonitoringRuntime
from app.jobs.simulation import SimulationMonitoringRuntime
from app.services.oauth_context import OAuthContext
from app.services.shopee_collection import ShopeeCollectionService
from app.services.shopee_discovery import ShopeeDiscoveryService
from app.services.simulated_automation import SimulatedAutomation
from app.services.telegram_publication import TelegramPublicationService
from app.services.telegram_technical import TelegramTechnicalService

logger = logging.getLogger("ofertas_bot")


def create_app(
    settings: Settings | None = None, *, background_monitoring_allowed: bool = True
) -> FastAPI:
    fixed_oauth_settings = settings
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    protect_oauth_access_logs()
    engine = build_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        logger.info("application_started")
        try:
            if application.state.background_monitoring_allowed:
                application.state.monitoring.start()
            yield
        finally:
            application.state.monitoring.shutdown()
            application.state.telegram_publisher.close()
            application.state.simulated_pipeline.close()
            engine.dispose()

    application = FastAPI(title="Ofertas Bot", version="0.3.0", lifespan=lifespan)
    application.state.settings = settings
    application.state.background_monitoring_allowed = background_monitoring_allowed
    application.state.mercadolivre_oauth = OAuthContext(fixed_oauth_settings)
    application.state.engine = engine
    application.state.session_factory = build_session_factory(engine)
    application.state.telegram_publisher = TelegramPublisher(settings)
    application.state.telegram_publications = TelegramPublicationService(
        settings, publisher=application.state.telegram_publisher
    )
    application.state.telegram_technical = TelegramTechnicalService(
        settings, publisher=application.state.telegram_publisher
    )
    application.state.integrations = ConnectorRegistry(
        settings, session_factory=application.state.session_factory
    )
    application.state.simulated_pipeline = SimulatedAutomation(
        application.state.session_factory, settings
    )
    application.state.shopee_collection = ShopeeCollectionService(
        application.state.session_factory, settings
    )
    application.state.shopee_discovery = ShopeeDiscoveryService(
        application.state.session_factory, settings
    )
    application.state.monitoring = MonitoringRuntime(
        application.state.session_factory,
        settings,
        connector_registry=application.state.integrations,
    )
    if settings.simulation_automation_enabled:
        application.state.monitoring = SimulationMonitoringRuntime(
            application.state.session_factory,
            settings,
            pipeline=application.state.simulated_pipeline,
        )
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"]
    )

    @application.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            local = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            )
            if not local and request.client is not None:
                peer = ipaddress.ip_address(request.client.host)
                local = any(
                    peer in ipaddress.ip_network(network)
                    for network in settings.admin_allowed_networks
                )
        except ValueError:
            local = False
        if not local:
            return JSONResponse(
                status_code=403, content={"detail": "Acesso permitido somente localmente no MVP"}
            )
        return await call_next(request)

    @application.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc: SQLAlchemyError):
        logger.error("database_error", extra={"error_type": type(exc).__name__})
        return JSONResponse(
            status_code=503, content={"detail": "Banco indisponível ou migrações pendentes"}
        )

    @application.get("/health", tags=["Sistema"])
    def health():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "database": "unavailable",
                    "price_provider": settings.price_provider,
                },
            )
        return {"status": "ok", "database": "connected", "price_provider": settings.price_provider}

    application.include_router(products_router)
    application.include_router(monitoring_router)
    application.include_router(categories_router)
    application.include_router(auto_pilot_router)
    application.include_router(auto_pilot_real_router)
    application.include_router(integrations_router)
    application.include_router(manual_offers_router)
    application.include_router(telegram_router)
    application.include_router(telegram_technical_router)
    application.include_router(simulated_automation_router)
    application.include_router(shopee_collection_router)
    application.include_router(shopee_discovery_router)
    application.include_router(oauth_admin_router)
    application.include_router(oauth_callback_router)
    return application


app = create_app()
