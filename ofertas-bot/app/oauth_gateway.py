"""Public callback-only ASGI app. Never expose the administrative application through a tunnel."""

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from app.api.oauth import callback_router
from app.core.config import Settings
from app.core.logging import configure_logging, protect_oauth_access_logs
from app.services.oauth_context import OAuthContext


def create_callback_app(settings: Settings | None = None, *, oauth_context=None) -> FastAPI:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.state.mercadolivre_oauth = oauth_context or OAuthContext(settings)
    configure_logging("WARNING")
    protect_oauth_access_logs()

    @application.middleware("http")
    async def configured_callback_host(request: Request, call_next):
        try:
            expected = urlsplit(
                application.state.mercadolivre_oauth.settings.mercadolivre_redirect_uri or ""
            ).hostname
        except ValidationError:
            expected = None
        if not expected or request.url.hostname != expected:
            return PlainTextResponse(
                "Not found", status_code=404, headers={"Cache-Control": "no-store"}
            )
        return await call_next(request)

    application.include_router(callback_router)
    return application


app = create_callback_app()
