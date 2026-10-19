"""ACL tests use synthetic files in temporary directories only."""

import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import oauth_permissions as permissions
from app.services.oauth_permissions import secure_directory
from app.services.oauth_store import OAuthStorageError


@pytest.fixture
def recording_acl(monkeypatch):
    class RecordingACL:
        def __init__(self):
            self.calls = []
            self.fail = False

        def restrict(self, path, *, directory):
            if self.fail:
                raise PermissionError("SYNTHETIC-PRIVATE-PATH")
            self.calls.append((path, directory))

    acl = RecordingACL()
    monkeypatch.setattr(permissions, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(permissions, "_WindowsACL", lambda: acl)
    return acl


def test_only_directory_and_recognized_sqlite_files_are_restricted(tmp_path, recording_acl):
    directory = tmp_path / "oauth"
    directory.mkdir()
    names = [
        "mercadolivre.sqlite3",
        "mercadolivre.sqlite3-journal",
        "mercadolivre.sqlite3-wal",
        "mercadolivre.sqlite3-shm",
        "mercadolivre.sqlite3.refresh-lock",
        "synthetic-test.sqlite3",
    ]
    for name in names:
        (directory / name).write_bytes(b"SYNTHETIC-CIPHERTEXT")
    secure_directory(directory)
    assert recording_acl.calls == [(directory, True)] + [
        (file, False) for file in sorted(directory.iterdir())
    ]
    assert all(path.parent == directory for path, is_dir in recording_acl.calls if not is_dir)
    assert all(file.read_bytes() == b"SYNTHETIC-CIPHERTEXT" for file in directory.iterdir())


def test_new_directory_is_created_without_changing_parent_acl(tmp_path, recording_acl):
    directory = tmp_path / "app" / "oauth"
    secure_directory(directory)
    assert directory.is_dir()
    assert recording_acl.calls == [(directory, True)]


@pytest.mark.parametrize("unknown", ["unrelated.txt", "unrelated.sqlite", "subdirectory"])
def test_unknown_entries_abort_before_any_acl_change(tmp_path, recording_acl, unknown):
    directory = tmp_path / "oauth"
    directory.mkdir()
    item = directory / unknown
    if unknown == "subdirectory":
        item.mkdir()
    else:
        item.write_text("SYNTHETIC-UNRELATED-FILE", encoding="utf-8")
    with pytest.raises(OAuthStorageError, match="OAuth storage is unavailable"):
        secure_directory(directory)
    assert recording_acl.calls == []


@pytest.mark.parametrize("target_kind", ["directory", "ancestor", "sqlite", "sidecar", "lock"])
def test_reparse_points_abort_before_any_acl_change(
    tmp_path, recording_acl, monkeypatch, target_kind
):
    directory = tmp_path / "oauth"
    directory.mkdir()
    sqlite_file = directory / "synthetic.sqlite3"
    sqlite_file.write_bytes(b"SYNTHETIC")
    sidecar = directory / "synthetic.sqlite3-wal"
    refresh_lock = directory / "synthetic.sqlite3.refresh-lock"
    sidecar.write_bytes(b"SYNTHETIC")
    refresh_lock.write_bytes(b"\0")
    targets = {
        "directory": directory,
        "ancestor": tmp_path,
        "sqlite": sqlite_file,
        "sidecar": sidecar,
        "lock": refresh_lock,
    }
    original = permissions._is_reparse
    monkeypatch.setattr(
        permissions, "_is_reparse", lambda path: path == targets[target_kind] or original(path)
    )
    with pytest.raises(OAuthStorageError):
        secure_directory(directory)
    assert recording_acl.calls == []


def test_non_windows_fails_before_creating_a_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "os", SimpleNamespace(name="posix"))
    target = tmp_path / "must-not-exist"
    with pytest.raises(OAuthStorageError) as captured:
        secure_directory(target)
    assert captured.value.code == "PROTECTION_UNAVAILABLE"
    assert not target.exists()


def test_relative_path_fails_before_creating_anything(recording_acl):
    with pytest.raises(OAuthStorageError):
        secure_directory(Path("SYNTHETIC-RELATIVE-DIRECTORY"))
    assert recording_acl.calls == []


def test_directory_creation_error_has_no_path_or_os_diagnostics(
    tmp_path, recording_acl, monkeypatch
):
    def denied(*args, **kwargs):
        raise PermissionError("SYNTHETIC-PRIVATE-PATH")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(OAuthStorageError) as captured:
        secure_directory(tmp_path / "oauth")
    assert str(captured.value) == "OAuth storage is unavailable."
    assert captured.value.__suppress_context__
    assert recording_acl.calls == []


def test_native_acl_failure_is_sanitized(tmp_path, recording_acl):
    recording_acl.fail = True
    with pytest.raises(OAuthStorageError) as captured:
        secure_directory(tmp_path / "oauth")
    assert str(captured.value) == "OAuth storage is unavailable."
    assert "SYNTHETIC" not in str(captured.value)


def _native_acl_is_exact(path, expected_sid, *, directory, inherited=False):
    """Inspect native ACLs without returning or printing the current user's SID."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer, dword = ctypes.c_void_p, ctypes.c_uint32
    get_security = advapi.GetNamedSecurityInfoW
    get_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        dword,
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
    ]
    get_security.restype = dword
    get_control = advapi.GetSecurityDescriptorControl
    get_control.argtypes = [pointer, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(dword)]
    get_control.restype = ctypes.c_int
    get_ace = advapi.GetAce
    get_ace.argtypes = [pointer, dword, ctypes.POINTER(pointer)]
    get_ace.restype = ctypes.c_int
    sid_text = advapi.ConvertSidToStringSidW
    sid_text.argtypes = [pointer, ctypes.POINTER(pointer)]
    sid_text.restype = ctypes.c_int
    local_free = kernel.LocalFree
    local_free.argtypes = [pointer]
    local_free.restype = pointer

    class ACL(ctypes.Structure):
        _fields_ = [
            ("revision", ctypes.c_ubyte),
            ("sbz1", ctypes.c_ubyte),
            ("size", ctypes.c_uint16),
            ("count", ctypes.c_uint16),
            ("sbz2", ctypes.c_uint16),
        ]

    class AllowedACE(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ubyte),
            ("flags", ctypes.c_ubyte),
            ("size", ctypes.c_uint16),
            ("mask", dword),
            ("sid_start", dword),
        ]

    descriptor, acl = pointer(), pointer()
    try:
        if get_security(
            str(path), 1, 0x4, None, None, ctypes.byref(acl), None, ctypes.byref(descriptor)
        ):
            return False
        if not acl:
            return False
        control, revision = ctypes.c_uint16(), dword()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            return False
        if bool(control.value & 0x1000) != (not inherited):
            return False
        entries = []
        for index in range(ctypes.cast(acl, ctypes.POINTER(ACL)).contents.count):
            ace_pointer = pointer()
            if not get_ace(acl, index, ctypes.byref(ace_pointer)):
                return False
            ace = ctypes.cast(ace_pointer, ctypes.POINTER(AllowedACE)).contents
            text = pointer()
            try:
                if not sid_text(
                    ace_pointer.value + AllowedACE.sid_start.offset, ctypes.byref(text)
                ):
                    return False
                entries.append((ctypes.wstring_at(text), ace.type, ace.flags, ace.mask))
            finally:
                if text:
                    local_free(text)
        flags = 0x03 if directory else 0x10 if inherited else 0
        expected = {(sid, 0, flags, 0x1F01FF) for sid in (expected_sid, "S-1-5-18")}
        return len(entries) == len(expected) and set(entries) == expected
    finally:
        if descriptor:
            local_free(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="Native ACL round trip requires Windows")
def test_windows_exact_acl_removes_old_grants_and_protects_existing_sqlite(tmp_path):
    directory = tmp_path / "native-oauth"
    directory.mkdir()
    database = directory / "synthetic.sqlite3"
    sidecar = directory / "synthetic.sqlite3-wal"
    refresh_lock = directory / "synthetic.sqlite3.refresh-lock"
    database.write_bytes(b"SYNTHETIC-ENCRYPTED-PAYLOAD")
    sidecar.write_bytes(b"SYNTHETIC-ENCRYPTED-JOURNAL")
    refresh_lock.write_bytes(b"\0")
    acl = permissions._WindowsACL()
    # Seed a broad explicit grant on this temporary directory and its files only.
    broad = f"D:P(A;OICI;FA;;;{acl._sid})(A;OICI;FA;;;WD)"
    acl._apply_sddl(directory, broad)
    for file in (database, sidecar, refresh_lock):
        acl._apply_sddl(file, broad.replace("OICI", ""))
    assert not _native_acl_is_exact(directory, acl._sid, directory=True)
    secure_directory(directory)
    assert _native_acl_is_exact(directory, acl._sid, directory=True)
    assert _native_acl_is_exact(database, acl._sid, directory=False)
    assert _native_acl_is_exact(sidecar, acl._sid, directory=False)
    assert _native_acl_is_exact(refresh_lock, acl._sid, directory=False)
    created_later = directory / "later.sqlite3"
    created_later.write_bytes(b"SYNTHETIC-LATER-PAYLOAD")
    assert _native_acl_is_exact(created_later, acl._sid, directory=False, inherited=True)
    assert database.read_bytes() == b"SYNTHETIC-ENCRYPTED-PAYLOAD"
    assert sidecar.read_bytes() == b"SYNTHETIC-ENCRYPTED-JOURNAL"
