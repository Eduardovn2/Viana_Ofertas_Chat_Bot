"""Exact Windows ACLs for the application's private, flat OAuth SQLite directory.

Only the supplied directory and recognized SQLite files in it may be changed.
No parent directories, account policies or application database ACLs are modified.
"""

import ctypes
import os
import re
import stat
from pathlib import Path

from app.services.oauth_store import OAuthStorageError

_DWORD = ctypes.c_uint32
_POINTER = ctypes.c_void_p
_HASHLESS_SID = re.compile(r"S-1-[0-9-]+\Z")
_SQLITE_FILE = re.compile(r".+\.sqlite3(?:-(?:journal|wal|shm)|\.refresh-lock)?\Z", re.IGNORECASE)
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000


class _SIDAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", _POINTER), ("Attributes", _DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SIDAndAttributes)]


class _WindowsACL:
    def __init__(self):
        if os.name != "nt":
            raise OAuthStorageError("PROTECTION_UNAVAILABLE")
        try:
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self._process = kernel.GetCurrentProcess
            self._process.argtypes = []
            self._process.restype = _POINTER
            self._close = kernel.CloseHandle
            self._close.argtypes = [_POINTER]
            self._close.restype = ctypes.c_int
            self._free = kernel.LocalFree
            self._free.argtypes = [_POINTER]
            self._free.restype = _POINTER
            self._open_token = advapi.OpenProcessToken
            self._open_token.argtypes = [_POINTER, _DWORD, ctypes.POINTER(_POINTER)]
            self._open_token.restype = ctypes.c_int
            self._token_info = advapi.GetTokenInformation
            self._token_info.argtypes = [
                _POINTER,
                ctypes.c_int,
                _POINTER,
                _DWORD,
                ctypes.POINTER(_DWORD),
            ]
            self._token_info.restype = ctypes.c_int
            self._sid_text = advapi.ConvertSidToStringSidW
            self._sid_text.argtypes = [_POINTER, ctypes.POINTER(_POINTER)]
            self._sid_text.restype = ctypes.c_int
            self._convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
            self._convert.argtypes = [
                ctypes.c_wchar_p,
                _DWORD,
                ctypes.POINTER(_POINTER),
                ctypes.POINTER(_DWORD),
            ]
            self._convert.restype = ctypes.c_int
            self._get_dacl = advapi.GetSecurityDescriptorDacl
            self._get_dacl.argtypes = [
                _POINTER,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(_POINTER),
                ctypes.POINTER(ctypes.c_int),
            ]
            self._get_dacl.restype = ctypes.c_int
            self._set = advapi.SetNamedSecurityInfoW
            self._set.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_int,
                _DWORD,
                _POINTER,
                _POINTER,
                _POINTER,
                _POINTER,
            ]
            self._set.restype = _DWORD
            self._sid = self._current_sid()
        except (OSError, AttributeError, ValueError):
            raise OAuthStorageError() from None

    def _current_sid(self) -> str:
        token, sid_text = _POINTER(), _POINTER()
        try:
            if not self._open_token(self._process(), 0x0008, ctypes.byref(token)):
                raise OAuthStorageError()
            size = _DWORD()
            self._token_info(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            if not 0 < size.value <= 65_536:
                raise OAuthStorageError()
            buffer = ctypes.create_string_buffer(size.value)
            if not self._token_info(token, 1, buffer, size, ctypes.byref(size)):
                raise OAuthStorageError()
            user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
            if not self._sid_text(user.User.Sid, ctypes.byref(sid_text)):
                raise OAuthStorageError()
            value = ctypes.wstring_at(sid_text)
            if not _HASHLESS_SID.fullmatch(value):
                raise OAuthStorageError()
            return value
        finally:
            if sid_text:
                self._free(sid_text)
            if token:
                self._close(token)

    def _apply_sddl(self, path: Path, sddl: str) -> None:
        descriptor = _POINTER()
        try:
            if not self._convert(sddl, 1, ctypes.byref(descriptor), None):
                raise OAuthStorageError()
            present, defaulted, dacl = ctypes.c_int(), ctypes.c_int(), _POINTER()
            if (
                not self._get_dacl(
                    descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
                )
                or not present.value
                or not dacl
            ):
                raise OAuthStorageError()
            information = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
            # SE_FILE_OBJECT=1. PROTECTED replaces old explicit and inherited ACEs.
            if self._set(str(path), 1, information, None, None, dacl, None) != 0:
                raise OAuthStorageError()
        finally:
            if descriptor:
                self._free(descriptor)

    def restrict(self, path: Path, *, directory: bool) -> None:
        inheritance = "OICI" if directory else ""
        principals = (self._sid, "SY") if self._sid != "S-1-5-18" else ("SY",)
        sddl = "D:P" + "".join(f"(A;{inheritance};FA;;;{sid})" for sid in principals)
        self._apply_sddl(path, sddl)


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _validate_directory_path(directory: Path) -> None:
    if not directory.is_absolute() or any(
        _is_reparse(path) for path in (directory, *directory.parents)
    ):
        raise OAuthStorageError()


def _known_files(directory: Path) -> list[Path]:
    files = []
    for entry in directory.iterdir():
        if _is_reparse(entry) or not entry.is_file() or not _SQLITE_FILE.fullmatch(entry.name):
            # Native inherited ACE propagation must not touch unknown descendants.
            raise OAuthStorageError()
        files.append(entry)
    return sorted(files)


def secure_directory(directory: Path) -> None:
    """Fail closed unless a private Windows directory can receive an exact DACL.

    Existing SQLite files and their rollback/WAL/SHM sidecars receive protected
    DACLs as well. Unknown entries and all reparse points are rejected before
    applying any ACL; the operation never recurses or changes a parent ACL.
    """
    if os.name != "nt":
        raise OAuthStorageError("PROTECTION_UNAVAILABLE")
    try:
        directory = Path(directory)
        _validate_directory_path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        _validate_directory_path(directory)
        files = _known_files(directory)
        acl = _WindowsACL()
        acl.restrict(directory, directory=True)
        for file in files:
            if _is_reparse(file) or not file.is_file():
                raise OAuthStorageError()
            acl.restrict(file, directory=False)
    except (OSError, ValueError, TypeError, AttributeError):
        raise OAuthStorageError() from None
