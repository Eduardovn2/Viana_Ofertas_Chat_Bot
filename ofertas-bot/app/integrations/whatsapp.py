"""Send one beta offer to WhatsApp through the local gateway (whatsapp-gateway/).

The gateway is a small Node process (Baileys) logged in to a WhatsApp number by
QR code or pairing code. It only listens on 127.0.0.1. One POST per message, no
retries: if the gateway may have sent it, the error is marked uncertain so the
offer is never posted twice.
"""

import logging
import re

import httpx
from pydantic import SecretStr

from app.services.message_templates import RenderedMessage, validate_public_url

logger = logging.getLogger("ofertas_bot.whatsapp")

_JID = re.compile(r"([0-9]{5,20}(-[0-9]{5,20})?@g\.us|[0-9]{5,30}@newsletter)\Z")
_MESSAGES = {
    "PUBLISHER_DISABLED": "Publicador WhatsApp desativado (WHATSAPP_PUBLISHER_ENABLED).",
    "CHAT_NOT_ALLOWED": "Grupo/canal WhatsApp diferente de WHATSAPP_CHAT_ID.",
    "INVALID_MESSAGE": "Mensagem WhatsApp inválida.",
    "GATEWAY_DOWN": "Gateway WhatsApp fora do ar; inicie o whatsapp-gateway.",
    "NOT_CONNECTED": "Gateway WhatsApp sem login; leia o QR code ou o código de pareamento.",
    "GATEWAY_UNAUTHORIZED": "Gateway WhatsApp recusou o token (WHATSAPP_GATEWAY_TOKEN).",
    "WHATSAPP_REJECTED": "WhatsApp recusou a mensagem.",
    "DELIVERY_UNCERTAIN": "Resultado do envio WhatsApp incerto; não repetir automaticamente.",
}


class WhatsAppPublishError(Exception):
    def __init__(self, code, *, uncertain=False):
        self.code = code if code in _MESSAGES else "DELIVERY_UNCERTAIN"
        self.uncertain = uncertain or self.code == "DELIVERY_UNCERTAIN"
        super().__init__(_MESSAGES[self.code])


class WhatsAppPublisher:
    def __init__(self, settings, *, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    def close(self):
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _headers(self):
        token = self.settings.whatsapp_gateway_token
        if isinstance(token, SecretStr) and token.get_secret_value():
            return {"Authorization": f"Bearer {token.get_secret_value()}"}
        return {}

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
        return self._client

    def status(self) -> dict:
        """Gateway state, for diagnostics. Never raises."""
        try:
            response = self._http().get(
                f"{self.settings.whatsapp_gateway_url}/status", headers=self._headers(), timeout=5
            )
            return response.json()
        except Exception:
            return {"connected": False, "error": "GATEWAY_DOWN"}

    def send(self, jid: str, message: RenderedMessage) -> str:
        if self.settings.whatsapp_publisher_enabled is not True:
            raise WhatsAppPublishError("PUBLISHER_DISABLED")
        if (
            not isinstance(jid, str)
            or not _JID.fullmatch(jid)
            or jid != self.settings.whatsapp_chat_id
        ):
            raise WhatsAppPublishError("CHAT_NOT_ALLOWED")
        if not isinstance(message, RenderedMessage) or not message.text or len(message.text) > 4000:
            raise WhatsAppPublishError("INVALID_MESSAGE")
        payload = {"jid": jid, "text": message.text}
        if message.photo_url and self.settings.whatsapp_send_images:
            try:
                payload["imageUrl"] = validate_public_url(message.photo_url)
            except ValueError:
                pass
        try:
            response = self._http().post(
                f"{self.settings.whatsapp_gateway_url}/send",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
        except httpx.ConnectError:
            raise WhatsAppPublishError("GATEWAY_DOWN") from None
        except Exception:
            raise WhatsAppPublishError("DELIVERY_UNCERTAIN") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code == 200 and isinstance(data.get("id"), str) and data["id"]:
            return data["id"][:128]
        code = data.get("error") if isinstance(data, dict) else None
        if response.status_code == 503:
            raise WhatsAppPublishError("NOT_CONNECTED")
        if response.status_code == 401:
            raise WhatsAppPublishError("GATEWAY_UNAUTHORIZED")
        if response.status_code == 400:
            raise WhatsAppPublishError("INVALID_MESSAGE")
        if response.status_code == 502 and code == "WHATSAPP_REJECTED":
            raise WhatsAppPublishError("WHATSAPP_REJECTED")
        raise WhatsAppPublishError("DELIVERY_UNCERTAIN")
