"""Reuse the official Telegram publisher, sealed to MockTransport and fake identity."""

import json

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.telegram import TelegramPublisher

MOCK_CHAT = "-9999999999999"


class MockTelegramDelivery:
    mode = "SIMULATED"

    def __init__(self, fault=None):
        if fault not in (None, "timeout", "forbidden", "invalid"):
            raise ValueError("Cenário mock inválido.")
        self.fault = fault
        self.calls = 0
        # These synthetic settings never reuse the actual token, allowlist or app flags.
        synthetic = Settings(
            _env_file=None,
            app_env="test",
            database_url="sqlite://",
            telegram_publisher_enabled=True,
            telegram_real_sends_authorized=True,
            telegram_bot_token=SecretStr("999999999:SIMULATED_ONLY_FAKE_TOKEN_NEVER_REAL_123456"),
            telegram_allowed_chat_ids=(MOCK_CHAT,),
        )
        self._client = httpx.Client(transport=httpx.MockTransport(self._respond), trust_env=False)
        self._publisher = TelegramPublisher(synthetic, client=self._client)

    def _respond(self, request):
        self.calls += 1
        if self.fault == "timeout":
            raise httpx.ReadTimeout("Simulated timeout")
        if self.fault == "forbidden":
            return httpx.Response(403, json={"ok": False, "error_code": 403})
        if self.fault == "invalid":
            return httpx.Response(200, json={"ok": True})
        assert json.loads(request.content)["chat_id"] == MOCK_CHAT
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": self.calls,
                    "chat": {"id": int(MOCK_CHAT), "type": "group"},
                },
            },
        )

    def send(self, message):
        if (
            type(self._client._transport) is not httpx.MockTransport
            or self._publisher._client is not self._client
        ):
            raise ValueError("Transporte real proibido na simulação.")
        return self._publisher.send(MOCK_CHAT, message)

    def close(self):
        self._client.close()
