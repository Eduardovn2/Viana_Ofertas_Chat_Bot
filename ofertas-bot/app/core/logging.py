import json
import logging
from datetime import UTC, datetime
from urllib.parse import unquote


class OAuthAccessLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and unquote(path.split("?", 1)[0]).startswith(
                "/oauth/mercadolivre"
            ):
                args = list(record.args)
                args[2] = "/oauth/mercadolivre/callback"
                record.args = tuple(args)
        return True


def protect_oauth_access_logs():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OAuthAccessLogFilter) for item in logger.filters):
        logger.addFilter(OAuthAccessLogFilter())


class JsonFormatter(logging.Formatter):
    """Somente campos controlados; não serializa corpo, headers, URLs ou exceções."""

    def format(self, record: logging.LogRecord) -> str:
        result = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key in ("product_id", "error_type", "status_code"):
            if hasattr(record, key):
                result[key] = getattr(record, key)
        if hasattr(record, "oauth_error"):
            reason = record.oauth_error
            result["oauth_error"] = (
                reason
                if reason
                in {
                    "invalid_client",
                    "invalid_grant",
                    "invalid_scope",
                    "invalid_request",
                    "unauthorized_client",
                    "unauthorized_application",
                    "unsupported_grant_type",
                    "forbidden",
                    "local_rate_limited",
                    "network_error",
                    "unclassified",
                }
                else "unclassified"
            )
        return json.dumps(result, ensure_ascii=False)


def configure_logging(level: str) -> None:
    logger = logging.getLogger("ofertas_bot")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
