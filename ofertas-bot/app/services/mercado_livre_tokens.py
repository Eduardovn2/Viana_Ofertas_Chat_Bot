"""On-demand Mercado Livre token rotation with durable, single-use refresh claims.

Constructing this service never opens the private profile or makes a request.
A committed IN_PROGRESS claim precedes every POST. After an uncertain outcome,
the old refresh token is blocked until a new, explicit authorization is stored.
"""

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from threading import Lock

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.integrations.mercado_livre_oauth import TOKEN_URL, OAuthService
from app.services.oauth_store import OAuthStorageError, OAuthStore, OAuthTokenSnapshot

_MESSAGES = {
    "OAUTH_NOT_CONFIGURED": "Nenhuma autorização OAuth foi armazenada.",
    "OAUTH_CONFIGURATION": "A configuração local para renovar o OAuth é inválida.",
    "OAUTH_REAUTHORIZATION_REQUIRED": "A autorização OAuth precisa ser refeita localmente.",
    "OAUTH_REFRESH_UNCERTAIN": "A renovação OAuth teve resultado incerto; refaça a autorização local.",
    "OAUTH_REFRESH_BUSY": "Outra renovação OAuth está em andamento; tente novamente depois.",
    "OAUTH_RATE_LIMITED": "O provedor limitou a renovação OAuth; aguarde antes de tentar novamente.",
    "OAUTH_STORAGE_ERROR": "O armazenamento protegido OAuth está indisponível.",
}


class MercadoLivreTokenError(IntegrationError):
    def __init__(self, code: str, *, retry_at: datetime | None = None):
        code = code if code in _MESSAGES else "OAUTH_STORAGE_ERROR"
        self.retry_at = retry_at
        super().__init__(code, _MESSAGES[code])


def _error(code: str, *, retry_at: datetime | None = None):
    return MercadoLivreTokenError(code, retry_at=retry_at)


def _opaque(value, maximum=65_536):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC)


class MercadoLivreTokens:
    def __init__(
        self,
        settings: Settings,
        store: OAuthStore | Callable[[], OAuthStore],
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        *,
        lock_timeout_seconds: float = 5,
    ):
        self._settings = settings
        self._store_source = store
        self._resolved_store = None
        self._store_lock = Lock()
        self._client = http_client
        self._client_lock = Lock()
        self._owns_client = http_client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock_timeout = lock_timeout_seconds

    @property
    def _store(self) -> OAuthStore:
        with self._store_lock:
            if self._resolved_store is None:
                self._resolved_store = (
                    self._store_source() if callable(self._store_source) else self._store_source
                )
            return self._resolved_store

    def close(self):
        with self._client_lock:
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None

    def configured(self) -> bool:
        return self.status()["configured"]

    def status(self) -> dict:
        try:
            metadata = self._store.refresh_metadata()
            return {
                **metadata,
                "reauthorization_required": metadata["refresh_state"] in {"REVOKED", "UNCERTAIN"},
            }
        except OAuthStorageError:
            raise _error("OAUTH_STORAGE_ERROR") from None

    def _now(self):
        try:
            return _utc(self._clock())
        except Exception:
            raise _error("OAUTH_CONFIGURATION") from None

    def _credentials(self):
        try:
            client_id = self._settings.mercadolivre_client_id
            client_secret = self._settings.mercadolivre_client_secret
            if (
                not isinstance(client_id, SecretStr)
                or not isinstance(client_secret, SecretStr)
                or not _opaque(client_id.get_secret_value(), 256)
                or not _opaque(client_secret.get_secret_value(), 8192)
            ):
                raise ValueError
            fingerprint = hashlib.sha256(client_id.get_secret_value().encode("ascii")).hexdigest()
            credential_version = hashlib.sha256(
                json.dumps(
                    [client_id.get_secret_value(), client_secret.get_secret_value()],
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest()
            return client_id, client_secret, fingerprint, credential_version
        except Exception:
            raise _error("OAUTH_CONFIGURATION") from None

    @staticmethod
    def _validate_snapshot(snapshot, fingerprint):
        if snapshot is None:
            raise _error("OAUTH_NOT_CONFIGURED")
        payload = snapshot.payload
        try:
            stored = payload.get("client_id_fingerprint")
            if not isinstance(stored, str) or not hmac.compare_digest(stored, fingerprint):
                raise _error("OAUTH_CONFIGURATION")
            if (
                not _opaque(payload.get("access_token"))
                or not re.fullmatch(r"[1-9][0-9]{0,19}", str(payload.get("user_id", "")))
                or str(payload.get("token_type", "")).casefold() != "bearer"
            ):
                raise ValueError
            return _utc(payload.get("expires_at"))
        except MercadoLivreTokenError:
            raise
        except Exception:
            raise _error("OAUTH_REAUTHORIZATION_REQUIRED") from None

    def get_access_token(self) -> SecretStr:
        return self._get(None)

    def refresh_after_rejection(self, rejected_token: SecretStr) -> SecretStr:
        if not isinstance(rejected_token, SecretStr) or not _opaque(
            rejected_token.get_secret_value()
        ):
            raise _error("OAUTH_CONFIGURATION")
        return self._get(rejected_token)

    def _get(self, rejected_token: SecretStr | None):
        credentials = self._credentials()
        try:
            snapshot = self._store.load_token_snapshot()
            expiry = self._validate_snapshot(snapshot, credentials[2])
            if (
                rejected_token is None
                and snapshot.refresh_state is None
                and expiry > self._now() + timedelta(seconds=30)
            ):
                return SecretStr(snapshot.payload["access_token"])
            with self._store.refresh_lock(timeout_seconds=self._lock_timeout):
                snapshot = self._store.load_token_snapshot()
                expiry = self._validate_snapshot(snapshot, credentials[2])
                now = self._now()
                self._check_block(snapshot, credentials[3], now)
                different = rejected_token is not None and not hmac.compare_digest(
                    snapshot.payload["access_token"], rejected_token.get_secret_value()
                )
                if (
                    snapshot.refresh_state is None
                    and expiry > now + timedelta(seconds=30)
                    and (rejected_token is None or different)
                ):
                    return SecretStr(snapshot.payload["access_token"])
                return self._refresh(snapshot, credentials)
        except OAuthStorageError as error:
            code = "OAUTH_REFRESH_BUSY" if error.code == "REFRESH_BUSY" else "OAUTH_STORAGE_ERROR"
            raise _error(code) from None

    def _check_block(self, snapshot, credential_version, now):
        state = snapshot.refresh_state
        if state in {"IN_PROGRESS", "UNCERTAIN"}:
            if state == "IN_PROGRESS":
                self._store.block_refresh(snapshot.version, snapshot.attempt_id, "UNCERTAIN", now)
            raise _error("OAUTH_REFRESH_UNCERTAIN")
        if state == "REVOKED":
            raise _error("OAUTH_REAUTHORIZATION_REQUIRED")
        if state == "CONFIGURATION" and hmac.compare_digest(
            snapshot.credential_fingerprint, credential_version
        ):
            raise _error("OAUTH_CONFIGURATION")
        if state == "RATE_LIMITED" and snapshot.retry_at is not None and now < snapshot.retry_at:
            raise _error("OAUTH_RATE_LIMITED", retry_at=snapshot.retry_at)

    def _refresh(self, snapshot: OAuthTokenSnapshot, credentials):
        client_id, client_secret, fingerprint, credential_version = credentials
        if not _opaque(snapshot.payload.get("refresh_token")):
            raise _error("OAUTH_REAUTHORIZATION_REQUIRED")
        attempt = secrets.token_hex(32)
        if not self._store.begin_refresh(
            snapshot.version, attempt, credential_version, self._now()
        ):
            return self._replacement(fingerprint)
        form = {
            "grant_type": "refresh_token",
            "client_id": client_id.get_secret_value(),
            "client_secret": client_secret.get_secret_value(),
            "refresh_token": snapshot.payload["refresh_token"],
        }
        try:
            response = self._post(form)
        except Exception:
            self._block(snapshot, attempt, "UNCERTAIN")
            raise _error("OAUTH_REFRESH_UNCERTAIN") from None
        if response.status_code != 200:
            self._response_error(snapshot, attempt, response)
        try:
            tokens = OAuthService._tokens(response, self._now())
            if not hmac.compare_digest(tokens.user_id, str(snapshot.payload["user_id"])):
                raise ValueError
            if hmac.compare_digest(
                tokens.refresh_token.get_secret_value(), snapshot.payload["refresh_token"]
            ):
                raise ValueError
            payload = {
                "access_token": tokens.access_token.get_secret_value(),
                "refresh_token": tokens.refresh_token.get_secret_value(),
                "token_type": tokens.token_type,
                "expires_at": tokens.expires_at.isoformat(),
                "scope": tokens.scope,
                "user_id": tokens.user_id,
                "client_id_fingerprint": fingerprint,
            }
            saved = self._store.finish_refresh(snapshot.version, attempt, payload)
        except Exception:
            self._block(snapshot, attempt, "UNCERTAIN")
            raise _error("OAUTH_REFRESH_UNCERTAIN") from None
        if not saved:
            return self._replacement(fingerprint)
        return SecretStr(payload["access_token"])

    def _replacement(self, fingerprint):
        snapshot = self._store.load_token_snapshot()
        expiry = self._validate_snapshot(snapshot, fingerprint)
        if snapshot.refresh_state is None and expiry > self._now() + timedelta(seconds=30):
            return SecretStr(snapshot.payload["access_token"])
        raise _error("OAUTH_REFRESH_BUSY")

    def _block(self, snapshot, attempt, state, retry_at=None):
        self._store.block_refresh(snapshot.version, attempt, state, self._now(), retry_at)

    def _post(self, form):
        with self._client_lock:
            if self._client is None:
                self._client = httpx.Client(timeout=5, follow_redirects=False, trust_env=False)
            client = self._client
        return client.post(
            TOKEN_URL,
            data=form,
            headers={"Accept": "application/json"},
            timeout=5,
            follow_redirects=False,
        )

    def _response_error(self, snapshot, attempt, response):
        candidate = None
        try:
            if len(response.content) <= 262_144:
                payload = response.json()
                candidate = payload.get("error") if isinstance(payload, dict) else None
                if not isinstance(candidate, str):
                    candidate = None
        except Exception:
            pass
        if response.status_code == 429:
            retry_at = self._retry_at(response)
            self._block(snapshot, attempt, "RATE_LIMITED", retry_at)
            raise _error("OAUTH_RATE_LIMITED", retry_at=retry_at)
        if response.status_code in {400, 401, 403} and candidate == "invalid_grant":
            self._block(snapshot, attempt, "REVOKED")
            raise _error("OAUTH_REAUTHORIZATION_REQUIRED")
        if response.status_code in {400, 401, 403} and candidate in {
            "invalid_client",
            "unauthorized_client",
            "invalid_request",
            "unsupported_grant_type",
        }:
            self._block(snapshot, attempt, "CONFIGURATION")
            raise _error("OAUTH_CONFIGURATION")
        self._block(snapshot, attempt, "UNCERTAIN")
        raise _error("OAUTH_REFRESH_UNCERTAIN")

    def _retry_at(self, response):
        now = self._now()
        maximum = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
        try:
            value = response.headers.get("Retry-After", "60")
            if re.fullmatch(r"[0-9]+", value):
                if len(value) > 12:
                    return maximum
                seconds = int(value)
                if seconds >= (maximum - now).total_seconds():
                    return maximum
                return now + timedelta(seconds=max(1, seconds))
            return max(now + timedelta(seconds=1), _utc(parsedate_to_datetime(value)))
        except (ValueError, TypeError, OverflowError):
            return now + timedelta(seconds=60)
