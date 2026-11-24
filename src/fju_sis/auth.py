"""Secure local session storage (Windows Credential Manager; 0600 file on POSIX).

Only stores JWT auth_token, refresh_token, student ID and display metadata.
Never persists the user's LDAP password.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

DEFAULT_TARGET = "FJU-SIS-MCP/session"
DEFAULT_CRED_TARGET = "FJU-SIS-MCP/credentials"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


@dataclass
class Session:
    emp_no: str
    auth_token: str
    refresh_token: str = ""
    exp: int = 0
    name: str = ""

    def is_expired(self, leeway_seconds: int = 60) -> bool:
        if not self.exp:
            return False
        return time.time() + leeway_seconds >= self.exp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            emp_no=str(data.get("emp_no") or "").strip(),
            auth_token=str(data.get("auth_token") or "").strip(),
            refresh_token=str(data.get("refresh_token") or "").strip(),
            exp=int(data.get("exp") or 0),
            name=str(data.get("name") or "").strip(),
        )


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode the JWT payload segment without verifying signature (for local metadata only)."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def session_from_tokens(auth_token: str, refresh_token: str = "", emp_no: str = "") -> Session:
    claims = decode_jwt_payload(auth_token)
    resolved_emp = emp_no.strip() or str(
        claims.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
        or claims.get("unique_name")
        or claims.get("sub")
        or ""
    ).strip()
    resolved_name = str(claims.get("CName") or claims.get("name") or "").strip()
    try:
        exp = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    return Session(
        emp_no=resolved_emp,
        auth_token=auth_token.strip(),
        refresh_token=refresh_token.strip(),
        exp=exp,
        name=resolved_name,
    )


if sys.platform == "win32":

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]
    _advapi32.CredFree.restype = None


class SessionStore:
    """Persists Session in Windows Credential Manager (no plaintext fallback on Windows)."""

    def __init__(self, target: str = DEFAULT_TARGET, posix_path: Path | None = None):
        self.target = target
        self.posix_path = posix_path or (Path.home() / ".config" / "fju-sis" / "session.json")

    def save(self, session: Session) -> None:
        raw = json.dumps(session.to_dict(), ensure_ascii=False).encode("utf-8")
        if sys.platform == "win32":
            blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
            cred = _CREDENTIALW()
            cred.Flags = 0
            cred.Type = _CRED_TYPE_GENERIC
            cred.TargetName = self.target
            cred.Comment = "FJU SIS session tokens (no password stored)"
            cred.CredentialBlobSize = len(raw)
            cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
            cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
            cred.UserName = session.emp_no or "fju-sis"
            if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
                raise OSError(f"Windows Credential Manager write failed (error {ctypes.get_last_error()})")
            return
        self.posix_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.posix_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.replace(tmp, self.posix_path)

    def load(self) -> Session | None:
        if sys.platform == "win32":
            ptr = ctypes.POINTER(_CREDENTIALW)()
            if not _advapi32.CredReadW(self.target, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
                err = ctypes.get_last_error()
                if err == 1168:  # ERROR_NOT_FOUND
                    return None
                raise OSError(f"Windows Credential Manager read failed (error {err})")
            try:
                cred = ptr.contents
                raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            finally:
                _advapi32.CredFree(ptr)
            data = json.loads(raw.decode("utf-8"))
            return Session.from_dict(data)
        if not self.posix_path.is_file():
            return None
        data = json.loads(self.posix_path.read_text(encoding="utf-8"))
        return Session.from_dict(data)

    def delete(self) -> bool:
        if sys.platform == "win32":
            if _advapi32.CredDeleteW(self.target, _CRED_TYPE_GENERIC, 0):
                return True
            err = ctypes.get_last_error()
            if err == 1168:  # ERROR_NOT_FOUND
                return False
            raise OSError(f"Windows Credential Manager delete failed (error {err})")
        if self.posix_path.is_file():
            self.posix_path.unlink()
            return True
        return False


class CredentialStore:
    """Optional OS-encrypted credential storage for automatic re-login after sleep/expiry."""

    def __init__(self, target: str = DEFAULT_CRED_TARGET, posix_path: Path | None = None):
        self.target = target
        self.posix_path = posix_path or (Path.home() / ".config" / "fju-sis" / "credentials.json")

    def save(self, emp_no: str, password: str) -> None:
        raw = json.dumps({"emp_no": emp_no.strip(), "password": password}, ensure_ascii=False).encode("utf-8")
        if sys.platform == "win32":
            blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
            cred = _CREDENTIALW()
            cred.Flags = 0
            cred.Type = _CRED_TYPE_GENERIC
            cred.TargetName = self.target
            cred.Comment = "FJU SIS auto-relogin credentials (OS Credential Manager)"
            cred.CredentialBlobSize = len(raw)
            cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
            cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
            cred.UserName = emp_no.strip() or "fju-sis"
            if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
                raise OSError(f"Windows Credential Manager write failed (error {ctypes.get_last_error()})")
            return
        self.posix_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.posix_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.replace(tmp, self.posix_path)

    def load(self) -> tuple[str, str] | None:
        if sys.platform == "win32":
            ptr = ctypes.POINTER(_CREDENTIALW)()
            if not _advapi32.CredReadW(self.target, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
                err = ctypes.get_last_error()
                if err == 1168:
                    return None
                raise OSError(f"Windows Credential Manager read failed (error {err})")
            try:
                cred = ptr.contents
                raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            finally:
                _advapi32.CredFree(ptr)
            data = json.loads(raw.decode("utf-8"))
        else:
            if not self.posix_path.is_file():
                return None
            data = json.loads(self.posix_path.read_text(encoding="utf-8"))
        emp_no = str(data.get("emp_no") or "").strip()
        password = str(data.get("password") or "")
        return (emp_no, password) if emp_no and password else None

    def delete(self) -> bool:
        if sys.platform == "win32":
            if _advapi32.CredDeleteW(self.target, _CRED_TYPE_GENERIC, 0):
                return True
            err = ctypes.get_last_error()
            if err == 1168:
                return False
            raise OSError(f"Windows Credential Manager delete failed (error {err})")
        if self.posix_path.is_file():
            self.posix_path.unlink()
            return True
        return False
