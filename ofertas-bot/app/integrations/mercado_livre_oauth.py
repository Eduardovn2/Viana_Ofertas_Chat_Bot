"""Explicit Mercado Livre authorization-code flow; never activates commercial APIs.

Official contract: https://developers.mercadolivre.com.br/pt_br/mensagens-post-venda/autenticacao-e-autorizacao
The state is stored only as a hash and is consumed atomically by the private store.
"""

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.services.oauth_store import OAuthAttempt, OAuthStateError, OAuthStorageError

AUTHORIZATION_URL = "https://auth.mercadolivre.com.br/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
_OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_client",
        "invalid_grant",
        "invalid_scope",
        "invalid_request",
        "unauthorized_client",
        "unauthorized_application",
        "unsupported_grant_type",
        "forbidden",
        "local_rate_limited",
    }
)
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_STATE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
_SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*\Z")
_MESSAGES = {
    "OAUTH_DISABLED": "A autorização OAuth do Mercado Livre está desativada.",
    "OAUTH_CONFIGURATION": "A configuração OAuth do Mercado Livre está incompleta ou inválida.",
    "OAUTH_INVALID_STATE": "A tentativa OAuth é inválida, expirou ou já foi utilizada.",
    "OAUTH_DENIED": "A autorização OAuth não foi concedida.",
    "OAUTH_INVALID_CODE": "O código de autorização OAuth está ausente ou é inválido.",
    "OAUTH_TOKEN_EXCHANGE": "Não foi possível concluir a troca OAuth com o Mercado Livre.",
    "OAUTH_INVALID_TOKEN_RESPONSE": "A resposta OAuth do Mercado Livre é inválida.",
    "OAUTH_STORAGE_ERROR": "Não foi possível acessar o armazenamento protegido OAuth.",
}


def _error(code: str) -> IntegrationError:
    return IntegrationError(code, _MESSAGES[code])


class OAuthTokenExchangeError(IntegrationError):
    """Only controlled diagnostics may cross the boundary to the HTTP/log layer."""

    def __init__(self, status_code: int | None = None, oauth_error: str = "unclassified"):
        super().__init__("OAUTH_TOKEN_EXCHANGE", _MESSAGES["OAUTH_TOKEN_EXCHANGE"])
        self.status_code = (
            status_code
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else None
        )
        self.oauth_error = (
            oauth_error
            if isinstance(oauth_error, str)
            and oauth_error in _OAUTH_ERROR_CODES | {"unclassified", "network_error"}
            else "unclassified"
        )


class OAuthStorage(Protocol):
    def save_attempt(
        self,
        state_hash: str,
        verifier: str | None,
        binding_hash: str,
        redirect_uri: str,
        client_id_fingerprint: str,
        expires_at: datetime,
    ) -> None: ...

    def consume_attempt(
        self,
        state_hash: str,
        binding_hash: str,
        now: datetime,
    ) -> OAuthAttempt: ...

    def save_tokens(self, payload: dict) -> None: ...


@dataclass(frozen=True, repr=False)
class OAuthTokens:
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: datetime
    scope: str
    user_id: str
    token_type: str = "bearer"


@dataclass(frozen=True, repr=False)
class _Configuration:
    client_id: SecretStr
    client_secret: SecretStr
    redirect_uri: str
    fingerprint: str
    pkce: bool
    ttl: int


def _opaque(value, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC)


class OAuthService:
    def __init__(
        self,
        settings: Settings,
        store: OAuthStorage,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._settings = settings
        self._store = store
        self._client = http_client
        self._owns_client = http_client is None
        self._clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _now(self) -> datetime:
        try:
            return _utc(self._clock())
        except Exception:
            raise _error("OAUTH_CONFIGURATION") from None

    def _configuration(self) -> _Configuration:
        if not self._settings.mercadolivre_oauth_enabled:
            raise _error("OAUTH_DISABLED")
        try:
            client_id = self._settings.mercadolivre_client_id
            client_secret = self._settings.mercadolivre_client_secret
            redirect = self._settings.mercadolivre_redirect_uri
            if not isinstance(client_id, SecretStr) or not isinstance(client_secret, SecretStr):
                raise ValueError
            if not _opaque(client_id.get_secret_value(), 256):
                raise ValueError
            if not _opaque(client_secret.get_secret_value(), 8192):
                raise ValueError
            uri = urlsplit(redirect)
            if (
                not isinstance(redirect, str)
                or len(redirect) > 2048
                or uri.scheme != "https"
                or not uri.hostname
                or "." not in uri.hostname
                or uri.hostname.endswith(".")
                or uri.hostname in {"localhost", "localhost.localdomain"}
                or uri.port not in (None, 443)
                or uri.username is not None
                or uri.password is not None
                or uri.path != "/oauth/mercadolivre/callback"
                or uri.query
                or uri.fragment
                or "\\" in redirect
                or any(ord(character) <= 32 or ord(character) == 127 for character in redirect)
            ):
                raise ValueError
            try:
                ipaddress.ip_address(uri.hostname)
            except ValueError:
                pass
            else:
                raise ValueError
            ttl = self._settings.mercadolivre_oauth_state_ttl_seconds
            if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 900:
                raise ValueError
            return _Configuration(
                client_id,
                client_secret,
                redirect,
                hashlib.sha256(client_id.get_secret_value().encode("ascii")).hexdigest(),
                self._settings.mercadolivre_pkce_enabled,
                ttl,
            )
        except Exception:
            raise _error("OAUTH_CONFIGURATION") from None

    def begin(self, binding_hash: str) -> str:
        config = self._configuration()
        if not isinstance(binding_hash, str) or not _HASH.fullmatch(binding_hash):
            raise _error("OAUTH_INVALID_STATE")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64) if config.pkce else None
        expires_at = self._now() + timedelta(seconds=config.ttl)
        try:
            self._store.save_attempt(
                state_hash=hashlib.sha256(state.encode("ascii")).hexdigest(),
                verifier=verifier,
                binding_hash=binding_hash,
                redirect_uri=config.redirect_uri,
                client_id_fingerprint=config.fingerprint,
                expires_at=expires_at,
            )
        except Exception:
            raise _error("OAUTH_STORAGE_ERROR") from None
        query = {
            "response_type": "code",
            "client_id": config.client_id.get_secret_value(),
            "redirect_uri": config.redirect_uri,
            "state": state,
        }
        if verifier is not None:
            query["code_challenge"] = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode("ascii")).digest(),
                )
                .rstrip(b"=")
                .decode("ascii")
            )
            query["code_challenge_method"] = "S256"
        return AUTHORIZATION_URL + "?" + urlencode(query)

    def callback(
        self,
        state: str | None,
        code: str | None,
        error: str | None = None,
        *,
        binding_hash: str | None = None,
    ) -> None:
        config = self._configuration()
        now = self._now()
        if (
            not isinstance(state, str)
            or not _STATE.fullmatch(state)
            or not isinstance(binding_hash, str)
            or not _HASH.fullmatch(binding_hash)
        ):
            raise _error("OAUTH_INVALID_STATE")
        try:
            attempt = self._store.consume_attempt(
                hashlib.sha256(state.encode("ascii")).hexdigest(),
                binding_hash,
                now,
            )
        except OAuthStateError:
            raise _error("OAUTH_INVALID_STATE") from None
        except OAuthStorageError:
            raise _error("OAUTH_STORAGE_ERROR") from None
        except Exception:
            raise _error("OAUTH_STORAGE_ERROR") from None
        try:
            if (
                not isinstance(attempt, OAuthAttempt)
                or _utc(attempt.expires_at) <= now
                or not hmac.compare_digest(attempt.binding_hash, binding_hash)
                or not hmac.compare_digest(attempt.client_id_fingerprint, config.fingerprint)
                or attempt.redirect_uri != config.redirect_uri
                or (attempt.verifier is not None) != config.pkce
                or (attempt.verifier is not None and not _VERIFIER.fullmatch(attempt.verifier))
            ):
                raise ValueError
        except Exception:
            raise _error("OAUTH_INVALID_STATE") from None
        # Even an error or missing code consumes a valid attempt, preventing replay.
        if error is not None:
            raise _error("OAUTH_DENIED")
        if not _opaque(code, 4096):
            raise _error("OAUTH_INVALID_CODE")
        form = {
            "grant_type": "authorization_code",
            "client_id": config.client_id.get_secret_value(),
            "client_secret": config.client_secret.get_secret_value(),
            "redirect_uri": config.redirect_uri,
            "code": code,
        }
        if attempt.verifier is not None:
            form["code_verifier"] = attempt.verifier
        response = self._exchange(form)
        tokens = self._tokens(response, self._now())
        try:
            # This explicit boundary is the only place SecretStr tokens are unmasked.
            self._store.save_tokens(
                {
                    "access_token": tokens.access_token.get_secret_value(),
                    "refresh_token": tokens.refresh_token.get_secret_value(),
                    "token_type": tokens.token_type,
                    "expires_at": tokens.expires_at.isoformat(),
                    "scope": tokens.scope,
                    "user_id": tokens.user_id,
                    "client_id_fingerprint": config.fingerprint,
                }
            )
        except Exception:
            raise _error("OAUTH_STORAGE_ERROR") from None

    def _exchange(self, form: dict[str, str]) -> httpx.Response:
        try:
            if self._client is None:
                self._client = httpx.Client(timeout=5, follow_redirects=False, trust_env=False)
            response = self._client.post(
                TOKEN_URL,
                data=form,
                headers={"Accept": "application/json"},
                timeout=5,
                follow_redirects=False,
            )
        except httpx.TransportError:
            raise OAuthTokenExchangeError(oauth_error="network_error") from None
        except Exception:
            raise OAuthTokenExchangeError() from None
        if response.status_code != 200:
            oauth_error = "unclassified"
            try:
                if len(response.content) <= 262_144:
                    payload = response.json()
                    candidate = payload.get("error") if isinstance(payload, dict) else None
                    if isinstance(candidate, str) and candidate in _OAUTH_ERROR_CODES:
                        oauth_error = candidate
            except Exception:
                pass
            raise OAuthTokenExchangeError(response.status_code, oauth_error) from None
        return response

    @staticmethod
    def _tokens(response: httpx.Response, now: datetime) -> OAuthTokens:
        try:
            if len(response.content) > 262_144:
                raise ValueError
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            access = payload.get("access_token")
            refresh = payload.get("refresh_token")
            expires = payload.get("expires_in")
            token_type = payload.get("token_type")
            scope = payload.get("scope")
            user_id = payload.get("user_id")
            if not _opaque(access, 65_536) or not _opaque(refresh, 65_536):
                raise ValueError
            if not isinstance(token_type, str) or token_type.casefold() != "bearer":
                raise ValueError
            if (
                isinstance(expires, bool)
                or not isinstance(expires, int)
                or not 0 < expires <= 604_800
            ):
                raise ValueError
            if not isinstance(scope, str) or len(scope) > 8192 or not _SCOPE.fullmatch(scope):
                raise ValueError
            if isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
                raise ValueError
            if not re.fullmatch(r"[1-9][0-9]{0,19}", str(user_id)):
                raise ValueError
            return OAuthTokens(
                SecretStr(access),
                SecretStr(refresh),
                now + timedelta(seconds=expires),
                scope,
                str(user_id),
            )
        except Exception:
            raise _error("OAUTH_INVALID_TOKEN_RESPONSE") from None
