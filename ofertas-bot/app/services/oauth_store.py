"""Private OAuth storage, independent of the application database and its models.

The caller selects an absolute path under its private local configuration folder
and applies that folder's Windows ACL. Only ciphertext is ever bound to SQLite
for PKCE verifiers and token payloads. DPAPI is scoped to the current Windows user.
"""

import ctypes
import hmac
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

_MESSAGES = {
    "STORAGE_UNAVAILABLE": "OAuth storage is unavailable.",
    "PROTECTION_UNAVAILABLE": "OAuth protection is unavailable on this platform.",
    "PROTECTION_FAILED": "OAuth data could not be protected or recovered.",
    "INVALID_PAYLOAD": "OAuth storage input is invalid.",
    "INVALID_STATE": "OAuth attempt is invalid or expired.",
}
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_MAX_PAYLOAD_BYTES = 262_144
_MAX_CIPHERTEXT_BYTES = 1_000_000
_CLEANUP_LIMIT = 100


class OAuthStorageError(RuntimeError):
    """Public diagnostics never include paths, tokens, verifier or SQLite errors."""

    def __init__(self, code: str = "STORAGE_UNAVAILABLE"):
        self.code = code if code in _MESSAGES else "STORAGE_UNAVAILABLE"
        super().__init__(_MESSAGES[self.code])


class OAuthStateError(OAuthStorageError):
    def __init__(self):
        super().__init__("INVALID_STATE")


@dataclass(frozen=True, repr=False)
class OAuthAttempt:
    verifier: str | None
    binding_hash: str
    redirect_uri: str
    client_id_fingerprint: str
    expires_at: datetime


class Protector(Protocol):
    def protect(self, data: bytes) -> bytes: ...
    def unprotect(self, data: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsDPAPIProtector:
    """Noninteractive CryptProtectData/CryptUnprotectData with LocalFree cleanup.

    See Microsoft's dpapi.h documentation for both functions. In particular,
    CRYPTPROTECT_LOCAL_MACHINE is deliberately absent: other Windows users must
    not be able to decrypt this application's credentials.
    """

    def __init__(self):
        if os.name != "nt":
            raise OAuthStorageError("PROTECTION_UNAVAILABLE")
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._protect = crypt32.CryptProtectData
            self._unprotect = crypt32.CryptUnprotectData
            common_args = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DataBlob),
            ]
            self._protect.argtypes = common_args
            self._protect.restype = ctypes.c_int
            self._unprotect.argtypes = common_args
            self._unprotect.restype = ctypes.c_int
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._free = kernel32.LocalFree
            self._free.argtypes = [ctypes.c_void_p]
            self._free.restype = ctypes.c_void_p
        except (OSError, AttributeError):
            raise OAuthStorageError("PROTECTION_UNAVAILABLE") from None

    def _transform(self, data: bytes, *, decrypt: bool) -> bytes:
        if not isinstance(data, bytes) or len(data) > _MAX_CIPHERTEXT_BYTES:
            raise OAuthStorageError("PROTECTION_FAILED")
        buffer = ctypes.create_string_buffer(data, max(1, len(data)))
        source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = _DataBlob()
        try:
            function = self._unprotect if decrypt else self._protect
            # UI_FORBIDDEN=0x1; optional description, entropy and prompt are NULL.
            if not function(
                ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)
            ):
                raise OAuthStorageError("PROTECTION_FAILED")
            if not result.pbData or result.cbData > _MAX_CIPHERTEXT_BYTES:
                raise OAuthStorageError("PROTECTION_FAILED")
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            ctypes.memset(ctypes.addressof(buffer), 0, len(buffer))
            if result.pbData:
                ctypes.memset(result.pbData, 0, result.cbData)
                self._free(ctypes.cast(result.pbData, ctypes.c_void_p))

    def protect(self, data: bytes) -> bytes:
        return self._transform(data, decrypt=False)

    def unprotect(self, data: bytes) -> bytes:
        return self._transform(data, decrypt=True)


class _TokenPayload(dict):
    def __repr__(self):
        return "<OAuth token payload redacted>"

    __str__ = __repr__


def _aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC)


def _timestamp(value: datetime | str) -> int:
    return int(_aware(value).timestamp() * 1_000_000)


def _iso_timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000, UTC).isoformat()


def _valid_text(value, maximum: int, *, required: bool = True) -> bool:
    return (
        (value is None and not required)
        or isinstance(value, str)
        and (bool(value) or not required)
        and len(value) <= maximum
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


class OAuthStore:
    def __init__(self, path: Path, protector: Protector | None = None):
        self._protector = protector if protector is not None else WindowsDPAPIProtector()
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise OAuthStorageError("INVALID_PAYLOAD")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.executescript(
                    "CREATE TABLE IF NOT EXISTS oauth_attempts ("
                    "state_hash TEXT PRIMARY KEY, verifier BLOB, binding_hash TEXT NOT NULL, "
                    "redirect_uri TEXT NOT NULL, client_id_fingerprint TEXT NOT NULL, "
                    "expires_at INTEGER NOT NULL);"
                    "CREATE INDEX IF NOT EXISTS ix_oauth_attempts_expires_at ON oauth_attempts(expires_at);"
                    "CREATE TABLE IF NOT EXISTS oauth_tokens ("
                    "slot INTEGER PRIMARY KEY CHECK (slot = 1), payload BLOB NOT NULL, "
                    "expires_at INTEGER, updated_at INTEGER NOT NULL);"
                )
            if os.name != "nt":
                self.path.chmod(0o600)
        except (OSError, sqlite3.Error):
            raise OAuthStorageError() from None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA secure_delete=ON")
            yield connection
        except sqlite3.Error:
            raise OAuthStorageError() from None
        finally:
            if connection is not None:
                connection.close()

    def _encrypt(self, value: bytes) -> bytes:
        try:
            protected = self._protector.protect(value)
            if (
                not isinstance(protected, bytes)
                or not protected
                or len(protected) > _MAX_CIPHERTEXT_BYTES
            ):
                raise ValueError
            if hmac.compare_digest(protected, value):
                raise ValueError
            return protected
        except Exception:
            raise OAuthStorageError("PROTECTION_FAILED") from None

    def _decrypt(self, value: bytes) -> bytes:
        try:
            plaintext = self._protector.unprotect(value)
            if not isinstance(plaintext, bytes) or len(plaintext) > _MAX_PAYLOAD_BYTES:
                raise ValueError
            return plaintext
        except Exception:
            raise OAuthStorageError("PROTECTION_FAILED") from None

    @staticmethod
    def _cleanup(connection, now: int, limit: int = _CLEANUP_LIMIT) -> int:
        return connection.execute(
            "DELETE FROM oauth_attempts WHERE state_hash IN "
            "(SELECT state_hash FROM oauth_attempts WHERE expires_at <= ? ORDER BY expires_at LIMIT ?)",
            (now, limit),
        ).rowcount

    def cleanup_expired(self, now: datetime, *, limit: int = _CLEANUP_LIMIT) -> int:
        try:
            instant = _timestamp(now)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise OAuthStorageError("INVALID_PAYLOAD") from None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = self._cleanup(connection, instant, limit)
            connection.commit()
            return removed

    def save_attempt(
        self,
        state_hash: str,
        verifier: str | None,
        binding_hash: str,
        redirect_uri: str,
        client_id_fingerprint: str,
        expires_at: datetime,
    ) -> None:
        try:
            if not isinstance(state_hash, str) or not _HASH.fullmatch(state_hash):
                raise ValueError
            if not isinstance(binding_hash, str) or not _HASH.fullmatch(binding_hash):
                raise ValueError
            if (
                not _valid_text(verifier, 1024, required=False)
                or not _valid_text(redirect_uri, 2048)
                or not _valid_text(client_id_fingerprint, 128)
            ):
                raise ValueError
            expires = _timestamp(expires_at)
            encrypted = None if verifier is None else self._encrypt(verifier.encode("utf-8"))
        except (ValueError, TypeError, OverflowError):
            raise OAuthStorageError("INVALID_PAYLOAD") from None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup(connection, _timestamp(datetime.now(UTC)))
            try:
                connection.execute(
                    "INSERT INTO oauth_attempts "
                    "(state_hash, verifier, binding_hash, redirect_uri, client_id_fingerprint, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        state_hash,
                        encrypted,
                        binding_hash,
                        redirect_uri,
                        client_id_fingerprint,
                        expires,
                    ),
                )
            except sqlite3.IntegrityError:
                raise OAuthStateError() from None
            connection.commit()

    def consume_attempt(self, state_hash: str, binding_hash: str, now: datetime) -> OAuthAttempt:
        try:
            if not isinstance(state_hash, str) or not _HASH.fullmatch(state_hash):
                raise ValueError
            if not isinstance(binding_hash, str) or not _HASH.fullmatch(binding_hash):
                raise ValueError
            instant = _timestamp(now)
        except (ValueError, TypeError, OverflowError):
            raise OAuthStateError() from None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT verifier, binding_hash, redirect_uri, client_id_fingerprint, expires_at "
                "FROM oauth_attempts WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            if row is None:
                raise OAuthStateError()
            if (
                not isinstance(row[4], int)
                or not isinstance(row[1], str)
                or not _HASH.fullmatch(row[1])
                or not _valid_text(row[2], 2048)
                or not _valid_text(row[3], 128)
            ):
                raise OAuthStorageError()
            if row[4] <= instant:
                connection.execute("DELETE FROM oauth_attempts WHERE state_hash = ?", (state_hash,))
                connection.commit()
                raise OAuthStateError()
            if not hmac.compare_digest(row[1], binding_hash):
                raise OAuthStateError()
            try:
                verifier = None if row[0] is None else self._decrypt(row[0]).decode("utf-8")
                result = OAuthAttempt(
                    verifier, row[1], row[2], row[3], _aware(_iso_timestamp(row[4]))
                )
            except (ValueError, TypeError, UnicodeError, OverflowError):
                raise OAuthStorageError("PROTECTION_FAILED") from None
            connection.execute("DELETE FROM oauth_attempts WHERE state_hash = ?", (state_hash,))
            connection.commit()
            return result

    @staticmethod
    def _normalize_tokens(payload: dict) -> dict:
        try:
            if not isinstance(payload, dict) or not _valid_text(
                payload.get("access_token"), 65_536
            ):
                raise ValueError
            normalized = {"access_token": payload["access_token"]}
            for key, maximum in (
                ("refresh_token", 65_536),
                ("token_type", 50),
                ("scope", 8192),
                ("client_id_fingerprint", 128),
            ):
                if key in payload:
                    if not _valid_text(payload[key], maximum, required=False):
                        raise ValueError
                    normalized[key] = payload[key]
            if payload.get("expires_at") is not None:
                normalized["expires_at"] = _aware(payload["expires_at"]).isoformat()
            if "user_id" in payload:
                value = payload["user_id"]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, str))
                    or len(str(value)) > 100
                ):
                    raise ValueError
                if isinstance(value, str) and not _valid_text(value, 100):
                    raise ValueError
                normalized["user_id"] = value
            return normalized
        except (ValueError, TypeError, OverflowError):
            raise OAuthStorageError("INVALID_PAYLOAD") from None

    def save_tokens(self, payload: dict) -> None:
        normalized = self._normalize_tokens(payload)
        try:
            serialized = json.dumps(normalized, ensure_ascii=True, separators=(",", ":")).encode(
                "utf-8"
            )
            if len(serialized) > _MAX_PAYLOAD_BYTES:
                raise ValueError
            encrypted = self._encrypt(serialized)
            expires = _timestamp(normalized["expires_at"]) if normalized.get("expires_at") else None
        except (ValueError, TypeError, OverflowError):
            raise OAuthStorageError("INVALID_PAYLOAD") from None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO oauth_tokens (slot, payload, expires_at, updated_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(slot) DO UPDATE SET payload=excluded.payload, "
                "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                (encrypted, expires, _timestamp(datetime.now(UTC))),
            )
            connection.commit()

    def load_tokens(self) -> dict:
        with self._connection() as connection:
            row = connection.execute("SELECT payload FROM oauth_tokens WHERE slot=1").fetchone()
        if row is None:
            return _TokenPayload()
        try:
            payload = json.loads(self._decrypt(row[0]).decode("utf-8"))
        except (ValueError, TypeError, UnicodeError):
            raise OAuthStorageError("PROTECTION_FAILED") from None
        return _TokenPayload(self._normalize_tokens(payload))

    def metadata(self) -> dict:
        with self._connection() as connection:
            row = connection.execute("SELECT expires_at FROM oauth_tokens WHERE slot=1").fetchone()
        try:
            return {
                "configured": row is not None,
                "expires_at": None if row is None else _iso_timestamp(row[0]),
            }
        except (ValueError, TypeError, OverflowError):
            raise OAuthStorageError() from None

    def status(self) -> dict:
        return self.metadata()
