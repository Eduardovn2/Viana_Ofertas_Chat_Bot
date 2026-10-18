"""Private temporary stores only; test protectors never handle real credentials."""

import hashlib
import json
import os
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.oauth_store as oauth_store
from app.services.oauth_store import (
    OAuthStateError,
    OAuthStorageError,
    OAuthStore,
    WindowsDPAPIProtector,
)

NOW = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
CALLBACK = "http://127.0.0.1:8000/api/v1/integrations/MERCADO_LIVRE/oauth/callback"
VERIFIER = "SYNTHETIC-PKCE-VERIFIER-" + "x" * 64


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FakeProtector:
    """Opaque in-memory test mapping, intentionally not production encryption."""

    def __init__(self):
        self.values = {}
        self.fail_protect = False

    def protect(self, value: bytes) -> bytes:
        if self.fail_protect:
            raise RuntimeError("SYNTHETIC-SECRET-MUST-NOT-LEAK")
        opaque = b"SYNTHETIC-CIPHERTEXT:" + secrets.token_bytes(32)
        self.values[opaque] = value
        return opaque

    def unprotect(self, value: bytes) -> bytes:
        return self.values[value]


@pytest.fixture
def protector():
    return FakeProtector()


@pytest.fixture
def store(tmp_path, protector):
    return OAuthStore(tmp_path / "private" / "oauth.sqlite3", protector=protector)


def save_attempt(store, *, state="state", binding="browser", verifier=VERIFIER, expires=None):
    store.save_attempt(
        hashed(state),
        verifier,
        hashed(binding),
        CALLBACK,
        hashed("synthetic-client"),
        expires or NOW + timedelta(minutes=10),
    )


def token_payload():
    return {
        "access_token": "SYNTHETIC-ACCESS-TOKEN-DO-NOT-USE",
        "refresh_token": "SYNTHETIC-REFRESH-TOKEN-DO-NOT-USE",
        "token_type": "Bearer",
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "scope": "read offline_access",
        "user_id": 123456,
        "client_id_fingerprint": hashed("synthetic-client"),
    }


def test_attempt_is_encrypted_at_rest_single_use_and_redacted(store):
    save_attempt(store)
    assert VERIFIER.encode() not in store.path.read_bytes()
    with sqlite3.connect(store.path) as connection:
        (ciphertext,) = connection.execute("SELECT verifier FROM oauth_attempts").fetchone()
        assert isinstance(ciphertext, bytes)
        assert ciphertext != VERIFIER.encode()
    attempt = store.consume_attempt(hashed("state"), hashed("browser"), NOW)
    assert attempt.verifier == VERIFIER
    assert attempt.binding_hash == hashed("browser")
    assert attempt.redirect_uri == CALLBACK
    assert attempt.client_id_fingerprint == hashed("synthetic-client")
    assert attempt.expires_at == NOW + timedelta(minutes=10)
    assert not hasattr(attempt, "state_hash")
    assert VERIFIER not in repr(attempt)
    with pytest.raises(OAuthStateError, match="invalid or expired") as captured:
        store.consume_attempt(hashed("state"), hashed("browser"), NOW)
    assert captured.value.code == "INVALID_STATE"


def test_wrong_browser_does_not_consume_the_legitimate_attempt(store):
    save_attempt(store)
    with pytest.raises(OAuthStateError):
        store.consume_attempt(hashed("state"), hashed("other-browser"), NOW)
    assert store.consume_attempt(hashed("state"), hashed("browser"), NOW).verifier == VERIFIER


def test_expired_attempt_is_deleted_even_if_binding_is_wrong(store):
    save_attempt(store, expires=NOW)
    with pytest.raises(OAuthStateError):
        store.consume_attempt(hashed("state"), hashed("other-browser"), NOW)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM oauth_attempts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "state,binding", [(None, "browser"), ("invalid", "browser"), ("state", None)]
)
def test_invalid_state_inputs_have_constant_typed_diagnostics(store, state, binding):
    with pytest.raises(OAuthStateError) as captured:
        store.consume_attempt(state, binding, NOW)
    assert str(captured.value) == "OAuth attempt is invalid or expired."
    assert captured.value.code == "INVALID_STATE"


def test_launch_ticket_supports_no_verifier(store):
    save_attempt(store, state="launch:ticket", binding="ticket", verifier=None)
    attempt = store.consume_attempt(hashed("launch:ticket"), hashed("ticket"), NOW)
    assert attempt.verifier is None
    with pytest.raises(OAuthStateError):
        store.consume_attempt(hashed("launch:ticket"), hashed("ticket"), NOW)


def test_duplicate_state_never_overwrites_an_active_attempt(store):
    save_attempt(store)
    with pytest.raises(OAuthStateError):
        save_attempt(store, binding="different-browser", verifier="different-verifier")
    assert store.consume_attempt(hashed("state"), hashed("browser"), NOW).verifier == VERIFIER


def test_concurrent_consumers_have_exactly_one_winner(store, protector):
    save_attempt(store)

    def consume(_):
        other_store = OAuthStore(store.path, protector=protector)
        try:
            return other_store.consume_attempt(hashed("state"), hashed("browser"), NOW).verifier
        except OAuthStateError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(consume, range(8)))
    assert results.count(VERIFIER) == 1
    assert results.count(None) == 7


def test_token_payload_is_ciphertext_and_status_has_only_public_metadata(store):
    payload = token_payload()
    assert store.status() == {"configured": False, "expires_at": None}
    store.save_tokens(payload)
    loaded = store.load_tokens()
    assert isinstance(loaded, dict)
    assert loaded == payload
    assert payload["access_token"] not in repr(loaded)
    assert payload["refresh_token"] not in str(loaded)
    assert store.metadata() == {"configured": True, "expires_at": payload["expires_at"]}
    assert store.status() == store.metadata()
    disk = store.path.read_bytes()
    assert payload["access_token"].encode() not in disk
    assert payload["refresh_token"].encode() not in disk
    assert b'"access_token"' not in disk
    assert payload["access_token"] not in json.dumps(store.status())


def test_tokens_survive_store_recreation_and_unknown_upstream_fields_are_not_saved(
    store, protector
):
    payload = token_payload()
    store.save_tokens({**payload, "response_headers": {"secret": "SYNTHETIC-RAW-RESPONSE"}})
    reopened = OAuthStore(store.path, protector=protector)
    assert reopened.load_tokens() == payload
    assert b"SYNTHETIC-RAW-RESPONSE" not in store.path.read_bytes()


def test_failed_encryption_preserves_previous_tokens_and_hides_errors(store, protector):
    payload = token_payload()
    store.save_tokens(payload)
    protector.fail_protect = True
    with pytest.raises(OAuthStorageError) as captured:
        store.save_tokens({**payload, "access_token": "SYNTHETIC-NEW-INVALID-TOKEN"})
    assert captured.value.code == "PROTECTION_FAILED"
    assert "SYNTHETIC-SECRET" not in str(captured.value)
    assert store.load_tokens() == payload


@pytest.mark.parametrize(
    "changes",
    [
        {"access_token": None},
        {"access_token": ""},
        {"access_token": "invalid\nheader"},
        {"expires_at": "2030-01-01T00:00:00"},
        {"user_id": True},
    ],
)
def test_invalid_payload_does_not_replace_valid_tokens(store, changes):
    payload = token_payload()
    store.save_tokens(payload)
    with pytest.raises(OAuthStorageError) as captured:
        store.save_tokens({**payload, **changes})
    assert captured.value.code == "INVALID_PAYLOAD"
    assert store.load_tokens() == payload


def test_corrupt_ciphertext_fails_closed_without_echoing_payload(store):
    store.save_tokens(token_payload())
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE oauth_tokens SET payload=?", (b"SYNTHETIC-CORRUPT-TOKEN",))
    with pytest.raises(OAuthStorageError) as captured:
        store.load_tokens()
    assert captured.value.code == "PROTECTION_FAILED"
    assert "SYNTHETIC-CORRUPT-TOKEN" not in str(captured.value)


def test_recovery_failure_does_not_delete_the_pending_attempt(store, protector):
    save_attempt(store)
    original = protector.values.copy()
    protector.values.clear()
    with pytest.raises(OAuthStorageError):
        store.consume_attempt(hashed("state"), hashed("browser"), NOW)
    protector.values.update(original)
    assert store.consume_attempt(hashed("state"), hashed("browser"), NOW).verifier == VERIFIER


@pytest.mark.parametrize("column", ["binding_hash", "expires_at", "redirect_uri"])
def test_corrupt_attempt_metadata_has_only_sanitized_diagnostics(store, column):
    save_attempt(store)
    with sqlite3.connect(store.path) as connection:
        # Identifiers are selected from the fixed test parametrization above.
        connection.execute(
            f"UPDATE oauth_attempts SET {column}=?", ("SYNTHETIC-CORRUPT-METADATA\n",)
        )
    with pytest.raises(OAuthStorageError) as captured:
        store.consume_attempt(hashed("state"), hashed("browser"), NOW)
    assert str(captured.value) == "OAuth storage is unavailable."


def test_expiry_cleanup_is_bounded(store):
    expiry = int((NOW - timedelta(seconds=1)).timestamp() * 1_000_000)
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            "INSERT INTO oauth_attempts VALUES (?, NULL, ?, ?, ?, ?)",
            [
                (hashed(f"old-{i}"), hashed("browser"), CALLBACK, hashed("client"), expiry)
                for i in range(150)
            ],
        )
    assert store.cleanup_expired(NOW, limit=7) == 7
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM oauth_attempts").fetchone()[0] == 143
    assert store.cleanup_expired(NOW) == 100
    assert store.cleanup_expired(NOW) == 43


def test_default_protection_is_unavailable_off_windows_without_creating_a_file(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(oauth_store, "os", SimpleNamespace(name="posix"))
    target = tmp_path / "must-not-exist.sqlite3"
    with pytest.raises(OAuthStorageError) as captured:
        OAuthStore(target)
    assert captured.value.code == "PROTECTION_UNAVAILABLE"
    assert not target.exists()


def test_identity_protector_is_rejected_before_writing_a_verifier(tmp_path):
    class IdentityProtector:
        def protect(self, data):
            return data

        def unprotect(self, data):
            return data

    target = OAuthStore(tmp_path / "identity.sqlite3", protector=IdentityProtector())
    with pytest.raises(OAuthStorageError, match="protected or recovered"):
        save_attempt(target)
    assert VERIFIER.encode() not in target.path.read_bytes()


def test_naive_expiry_is_rejected_and_database_errors_are_sanitized(store, tmp_path, protector):
    with pytest.raises(OAuthStorageError) as captured:
        save_attempt(store, expires=datetime(2030, 1, 1))
    assert captured.value.code == "INVALID_PAYLOAD"
    invalid_file = tmp_path / "synthetic.sqlite3"
    invalid_file.write_bytes(b"SYNTHETIC-PRIVATE-PATH-CONTENT")
    with pytest.raises(OAuthStorageError) as captured:
        OAuthStore(invalid_file, protector=protector)
    assert str(captured.value) == "OAuth storage is unavailable."
    assert str(invalid_file) not in str(captured.value)


@pytest.mark.skipif(os.name != "nt", reason="DPAPI native current-user round trip requires Windows")
def test_windows_dpapi_round_trip_uses_only_synthetic_temporary_payloads(tmp_path):
    protector = WindowsDPAPIProtector()
    plaintext = b"SYNTHETIC-DPAPI-NATIVE-ROUNDTRIP"
    encrypted = protector.protect(plaintext)
    assert encrypted != plaintext
    assert protector.unprotect(encrypted) == plaintext
    store = OAuthStore(tmp_path / "native-dpapi.sqlite3")
    save_attempt(store)
    store.save_tokens(token_payload())
    assert store.consume_attempt(hashed("state"), hashed("browser"), NOW).verifier == VERIFIER
    assert store.load_tokens() == token_payload()
    disk = store.path.read_bytes()
    assert VERIFIER.encode() not in disk
    assert token_payload()["access_token"].encode() not in disk
