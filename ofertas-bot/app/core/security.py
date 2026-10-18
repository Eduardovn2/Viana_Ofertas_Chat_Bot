import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_admin(request: Request, api_key: str | None = Security(api_key_header)) -> None:
    expected = request.app.state.settings.admin_api_key
    if expected is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Configure ADMIN_API_KEY com ao menos 32 caracteres para habilitar a API administrativa",
        )
    if not api_key or not secrets.compare_digest(
        api_key.encode("utf-8"), expected.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Chave administrativa ausente ou inválida"
        )
