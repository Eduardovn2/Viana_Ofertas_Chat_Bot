import json
import logging
from datetime import UTC, datetime


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
        return json.dumps(result, ensure_ascii=False)


def configure_logging(level: str) -> None:
    logger = logging.getLogger("ofertas_bot")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
