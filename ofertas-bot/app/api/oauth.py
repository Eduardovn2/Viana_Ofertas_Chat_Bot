"""Only a local administrator can create a one-use OAuth launch link."""

import logging
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from app.core.security import require_admin
from app.integrations.errors import IntegrationError
from app.services.oauth_context import CALLBACK_PATH, digest
from app.services.oauth_store import OAuthStorageError

admin_router = APIRouter(
    prefix="/api/v1/oauth/mercadolivre",
    tags=["OAuth Mercado Livre"],
    dependencies=[Depends(require_admin)],
)
callback_router = APIRouter()
_COOKIE = "__Secure-viana-ml-oauth"
logger = logging.getLogger("ofertas_bot")
_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}


def _admin_error(error):
    if isinstance(error, IntegrationError):
        raise HTTPException(409, {"code": error.code, "message": error.message}) from None
    raise HTTPException(
        503, "OAuth indisponível; verifique a configuração local e a proteção Windows."
    ) from None


@admin_router.get("/status")
def status(request: Request):
    try:
        return JSONResponse(request.app.state.mercadolivre_oauth.status(), headers=_HEADERS)
    except (OAuthStorageError, ValidationError):
        raise HTTPException(503, "Configuração ou armazenamento OAuth indisponível.") from None


@admin_router.post("/authorize")
def authorize(request: Request):
    try:
        return JSONResponse(request.app.state.mercadolivre_oauth.create_launch(), headers=_HEADERS)
    except (IntegrationError, OAuthStorageError, ValidationError) as error:
        _admin_error(error)


@callback_router.get(CALLBACK_PATH, include_in_schema=False)
def callback(request: Request):
    """No code/state/token is ever returned to the browser or interpolated into diagnostics."""
    context = request.app.state.mercadolivre_oauth
    response = None
    clear_binding = False
    try:
        settings = context.settings
        expected_host = urlsplit(settings.mercadolivre_redirect_uri or "").hostname
        if request.url.hostname != expected_host:
            return PlainTextResponse(
                "Callback OAuth indisponível neste endereço.", 400, headers=_HEADERS
            )
        query = request.query_params
        if len(query.multi_items()) != len(query) or len(str(query)) > 8192:
            raise ValueError
        if "launch" in query:
            if set(query) != {"launch"}:
                raise ValueError
            url, binding, ttl = context.launch(query["launch"])
            response = RedirectResponse(url, status_code=303, headers=_HEADERS)
            response.set_cookie(
                _COOKIE,
                binding,
                max_age=ttl,
                secure=True,
                httponly=True,
                samesite="lax",
                path=CALLBACK_PATH,
            )
            return response
        if not query or set(query) - {"code", "state", "error", "error_description"}:
            raise ValueError
        binding = request.cookies.get(_COOKIE, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", binding):
            raise ValueError
        service = context.service(settings)
        try:
            service.callback(
                query.get("state"),
                query.get("code"),
                query.get("error"),
                binding_hash=digest(binding),
            )
        finally:
            service.close()
        response = PlainTextResponse(
            "OAuth concluído. Tokens protegidos no Windows. Integrações comerciais e publicações continuam desativadas.",
            headers=_HEADERS,
        )
        clear_binding = True
    except IntegrationError as error:
        logger.warning(
            "oauth_callback_failed",
            extra={
                "error_type": error.code,
                "status_code": getattr(error, "status_code", None),
                "oauth_error": getattr(error, "oauth_error", "unclassified"),
            },
        )
        clear_binding = error.code in {
            "OAUTH_DENIED",
            "OAUTH_INVALID_CODE",
            "OAUTH_TOKEN_EXCHANGE",
            "OAUTH_INVALID_TOKEN_RESPONSE",
        }
        response = PlainTextResponse(
            "Não foi possível concluir o OAuth. Reinicie a autorização pela API administrativa local.",
            400,
            headers=_HEADERS,
        )
    except (OAuthStorageError, ValidationError, ValueError, TypeError):
        logger.warning(
            "oauth_callback_rejected", extra={"error_type": "INVALID_REQUEST_OR_STORAGE"}
        )
        response = PlainTextResponse(
            "Não foi possível concluir o OAuth. Reinicie a autorização pela API administrativa local.",
            400,
            headers=_HEADERS,
        )
    if clear_binding:
        response.delete_cookie(
            _COOKIE, path=CALLBACK_PATH, secure=True, httponly=True, samesite="lax"
        )
    return response
