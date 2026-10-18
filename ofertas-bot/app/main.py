import ipaddress
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.categories import router as categories_router
from app.api.monitoring import router as monitoring_router
from app.api.products import router as products_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db import build_engine, build_session_factory
from app.jobs.runtime import MonitoringRuntime

logger = logging.getLogger("ofertas_bot")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = build_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        logger.info("application_started_simulated_provider")
        try:
            application.state.monitoring.start()
            yield
        finally:
            application.state.monitoring.shutdown()
            engine.dispose()

    application = FastAPI(title="Ofertas Bot", version="0.2.1", lifespan=lifespan)
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = build_session_factory(engine)
    application.state.monitoring = MonitoringRuntime(application.state.session_factory, settings)
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"]
    )

    @application.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            local = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
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
                    "price_provider": "simulated",
                },
            )
        return {"status": "ok", "database": "connected", "price_provider": "simulated"}

    application.include_router(products_router)
    application.include_router(monitoring_router)
    application.include_router(categories_router)
    return application


app = create_app()
