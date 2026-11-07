"""Explicit OAuth setup, private per-user storage and one-use browser launch tickets."""

import hashlib
import hmac
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode

from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.integrations.mercado_livre_oauth import OAuthService
from app.services.oauth_permissions import secure_directory
from app.services.oauth_store import OAuthStateError, OAuthStorageError, OAuthStore

CALLBACK_PATH = "/oauth/mercadolivre/callback"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def default_store_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not base:
        raise OAuthStorageError("PROTECTION_UNAVAILABLE")
    return Path(base) / "VianaOfertasChatBot" / "oauth" / "mercadolivre.sqlite3"


class OAuthContext:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store=None,
        service_factory=OAuthService,
        clock=None,
    ):
        self._fixed_settings = settings
        self._store = store
        self._service_factory = service_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = Lock()

    @property
    def settings(self) -> Settings:
        # OAuth-only reload: the commercial connector registry keeps its original disabled flags.
        return self._fixed_settings if self._fixed_settings is not None else Settings()

    @property
    def store(self):
        with self._lock:
            if self._store is None:
                if self.settings.app_env == "test":
                    raise OAuthStorageError("PROTECTION_UNAVAILABLE")
                path = default_store_path()
                secure_directory(path.parent)
                self._store = OAuthStore(path)
        return self._store

    def service(self, settings=None):
        return self._service_factory(settings or self.settings, self.store, clock=self.clock)

    def configuration(self):
        settings = self.settings
        if not settings.mercadolivre_oauth_enabled:
            raise IntegrationError(
                "OAUTH_DISABLED",
                "OAuth permanece desativado; habilite-o localmente após cadastrar a URI.",
            )
        if not (
            settings.mercadolivre_client_id
            and settings.mercadolivre_client_secret
            and settings.mercadolivre_client_id.get_secret_value().strip()
            and settings.mercadolivre_client_secret.get_secret_value().strip()
            and settings.mercadolivre_redirect_uri
        ):
            raise IntegrationError(
                "OAUTH_CONFIGURATION",
                "Configure Client ID, Client Secret e Redirect URI no ambiente local.",
            )
        return settings

    def create_launch(self):
        settings = self.configuration()
        ticket = secrets.token_urlsafe(32)
        self.store.save_attempt(
            state_hash=digest("launch:" + ticket),
            verifier=None,
            binding_hash=digest(ticket),
            redirect_uri=settings.mercadolivre_redirect_uri,
            client_id_fingerprint=digest(settings.mercadolivre_client_id.get_secret_value()),
            expires_at=self.clock()
            + timedelta(seconds=settings.mercadolivre_oauth_state_ttl_seconds),
        )
        return {
            "launch_url": settings.mercadolivre_redirect_uri + "?" + urlencode({"launch": ticket}),
            "expires_in": settings.mercadolivre_oauth_state_ttl_seconds,
        }

    def launch(self, ticket: str):
        settings = self.configuration()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
            raise OAuthStateError()
        attempt = self.store.consume_attempt(
            digest("launch:" + ticket), digest(ticket), self.clock()
        )
        if not hmac.compare_digest(
            attempt.redirect_uri, settings.mercadolivre_redirect_uri
        ) or not hmac.compare_digest(
            attempt.client_id_fingerprint,
            digest(settings.mercadolivre_client_id.get_secret_value()),
        ):
            raise OAuthStateError()
        binding = secrets.token_urlsafe(32)
        service = self.service(settings)
        try:
            url = service.begin(digest(binding))
        finally:
            service.close()
        return url, binding, settings.mercadolivre_oauth_state_ttl_seconds

    def status(self):
        settings = self.settings
        metadata = {
            "configured": False,
            "expires_at": None,
            "refresh_state": None,
            "retry_at": None,
        }
        if self._store is not None:
            metadata = self._store.refresh_metadata()
        elif settings.app_env != "test" and os.name == "nt" and default_store_path().is_file():
            metadata = self.store.refresh_metadata()
        return {
            "oauth_enabled": settings.mercadolivre_oauth_enabled,
            "client_id_configured": bool(
                settings.mercadolivre_client_id
                and settings.mercadolivre_client_id.get_secret_value().strip()
            ),
            "client_secret_configured": bool(
                settings.mercadolivre_client_secret
                and settings.mercadolivre_client_secret.get_secret_value().strip()
            ),
            "redirect_uri": settings.mercadolivre_redirect_uri,
            "pkce_enabled": settings.mercadolivre_pkce_enabled,
            "tokens_stored": metadata["configured"],
            "expires_at": metadata["expires_at"],
            "refresh_state": metadata["refresh_state"],
            "refresh_retry_at": metadata["retry_at"],
            "reauthorization_required": metadata["refresh_state"] in {"REVOKED", "UNCERTAIN"},
            "commercial_integration_activated": False,
            "publication_mode": "dry-run",
        }
