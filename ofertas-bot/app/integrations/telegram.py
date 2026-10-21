"""One explicit Telegram POST, with no retries or constructor network activity.

The publication service must durably claim its outbox item before calling send.
Official contract: https://core.telegram.org/bots/api#sendmessage and #sendphoto.
"""

import logging
import re
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from app.services.message_templates import (
    MessageTemplateError,
    RenderedMessage,
    telegram_text_length,
    validate_public_url,
)

_ORIGIN = "https://api.telegram.org"
_TOKEN = re.compile(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,256}\Z")
_LOG_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,256}")
_CHAT = re.compile(r"-[1-9][0-9]*\Z")
_MESSAGES = {
    "PUBLISHER_DISABLED": "Publicador Telegram desativado.",
    "SEND_NOT_AUTHORIZED": "Envios reais Telegram não autorizados.",
    "MISSING_BOT_TOKEN": "Token Telegram ausente ou inválido.",
    "CHAT_NOT_ALLOWED": "Grupo Telegram não autorizado.",
    "INVALID_MESSAGE": "Prévia Telegram inválida.",
    "TELEGRAM_BAD_REQUEST": "Telegram recusou os dados do envio.",
    "TELEGRAM_UNAUTHORIZED": "Telegram recusou a credencial do bot.",
    "TELEGRAM_FORBIDDEN": "Telegram recusou acesso ao grupo.",
    "TELEGRAM_RATE_LIMITED": "Telegram solicitou aguardar antes de outro envio.",
    "DELIVERY_UNCERTAIN": "Resultado do envio Telegram incerto; não repetir automaticamente.",
}
_KNOWN_ERRORS = {
    400: "TELEGRAM_BAD_REQUEST",
    401: "TELEGRAM_UNAUTHORIZED",
    403: "TELEGRAM_FORBIDDEN",
    429: "TELEGRAM_RATE_LIMITED",
}


class TelegramPublishError(Exception):
    def __init__(self, code, *, status=None, retry_after=None, uncertain=False):
        self.code = code if code in _MESSAGES else "DELIVERY_UNCERTAIN"
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.retry_after = retry_after if type(retry_after) is int and retry_after >= 0 else None
        self.uncertain = uncertain or self.code == "DELIVERY_UNCERTAIN"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True)
class TelegramSendResult:
    message_id: int
    chat_id: str


class _TelegramLogFilter(logging.Filter):
    def filter(self, record):
        # httpx normally logs the complete URL, including the mandatory bot token.
        try:
            message = record.getMessage()
        except Exception:
            message = "Telegram HTTP diagnostic suppressed."
        record.msg = _LOG_TOKEN.sub("[TELEGRAM_TOKEN_REDACTED]", message)
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def _protect_http_logs():
    names = {
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.connection_pool",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    }
    names.update(
        name for name in tuple(logging.Logger.manager.loggerDict) if name.startswith("httpcore.")
    )
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(item, _TelegramLogFilter) for item in logger.filters):
            logger.addFilter(_TelegramLogFilter())


def _chat_id(value) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or len(value) > 17 or not _CHAT.fullmatch(value):
        raise TelegramPublishError("CHAT_NOT_ALLOWED")
    if abs(int(value)) > (2**52 - 1):
        raise TelegramPublishError("CHAT_NOT_ALLOWED")
    return value


class TelegramPublisher:
    def __init__(self, settings, *, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    def close(self):
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def send(self, chat_id: str, message: RenderedMessage) -> TelegramSendResult:
        if self.settings.telegram_publisher_enabled is not True:
            raise TelegramPublishError("PUBLISHER_DISABLED")
        if self.settings.telegram_real_sends_authorized is not True:
            raise TelegramPublishError("SEND_NOT_AUTHORIZED")
        chat = _chat_id(chat_id)
        try:
            allowed = {_chat_id(item) for item in self.settings.telegram_allowed_chat_ids}
        except (TypeError, TelegramPublishError):
            raise TelegramPublishError("CHAT_NOT_ALLOWED") from None
        if chat not in allowed:
            raise TelegramPublishError("CHAT_NOT_ALLOWED")
        secret = self.settings.telegram_bot_token
        if not isinstance(secret, SecretStr) or not _TOKEN.fullmatch(secret.get_secret_value()):
            raise TelegramPublishError("MISSING_BOT_TOKEN")
        try:
            if not isinstance(message, RenderedMessage) or message.parse_mode != "HTML":
                raise MessageTemplateError
            if type(message.image_omitted) is not bool or (
                message.image_omitted and message.photo_url
            ):
                raise MessageTemplateError
            length = telegram_text_length(message.text)
            if length > (1024 if message.photo_url else 4096):
                raise MessageTemplateError
            if message.photo_url:
                validate_public_url(message.photo_url)
        except (ValueError, TypeError, UnicodeError):
            raise TelegramPublishError("INVALID_MESSAGE") from None
        payload = {"chat_id": chat, "parse_mode": message.parse_mode}
        method = "sendPhoto" if message.photo_url else "sendMessage"
        if message.photo_url:
            payload.update(photo=message.photo_url, caption=message.text)
        else:
            payload.update(text=message.text, link_preview_options={"is_disabled": True})
        _protect_http_logs()
        if self._client is None:
            self._client = httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        try:
            response = self._client.post(
                f"{_ORIGIN}/bot{secret.get_secret_value()}/{method}",
                json=payload,
                timeout=10,
                follow_redirects=False,
            )
        except Exception:
            raise TelegramPublishError("DELIVERY_UNCERTAIN", uncertain=True) from None
        status = response.status_code
        if status >= 500:
            raise TelegramPublishError("DELIVERY_UNCERTAIN", status=status, uncertain=True)
        try:
            if len(response.content) > 1_000_000:
                raise ValueError
            data = response.json()
            if not isinstance(data, dict) or type(data.get("ok")) is not bool:
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise TelegramPublishError(
                "DELIVERY_UNCERTAIN", status=status, uncertain=True
            ) from None
        if data["ok"] is False:
            error_code = data.get("error_code")
            if (
                type(error_code) is int
                and error_code in _KNOWN_ERRORS
                and status in {200, error_code}
            ):
                parameters = data.get("parameters")
                delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
                raise TelegramPublishError(
                    _KNOWN_ERRORS[error_code],
                    status=error_code,
                    retry_after=delay if error_code == 429 else None,
                )
            raise TelegramPublishError("DELIVERY_UNCERTAIN", status=status, uncertain=True)
        result = data.get("result")
        valid = status == 200 and isinstance(result, dict)
        if valid:
            message_id = result.get("message_id")
            returned_chat = result.get("chat")
            valid = (
                type(message_id) is int
                and message_id > 0
                and isinstance(returned_chat, dict)
                and type(returned_chat.get("id")) is int
                and str(returned_chat["id"]) == chat
                and returned_chat.get("type") in {"group", "supergroup"}
            )
        if not valid:
            raise TelegramPublishError("DELIVERY_UNCERTAIN", status=status, uncertain=True)
        return TelegramSendResult(message_id=message_id, chat_id=chat)
