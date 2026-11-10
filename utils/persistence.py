"""Crash-safe JSON replacement. Errors propagate to callers and block orders."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_json(path: Path, payload, *, backup: bool = True, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, allow_nan=False).encode("utf-8")
    if backup and path.exists():
        # Backups use the same replace protocol; never truncate the previous file.
        old = path.read_bytes()
        _replace(path.with_suffix(path.suffix + ".bak"), old)
    _replace(path, encoded, private=private)


def _replace(path: Path, encoded: bytes, *, private: bool = False) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if private and os.name == "nt":
                import win32api
                import win32con
                import win32security

                token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
                try:
                    user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
                    acl = win32security.ACL()
                    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, user)
                    win32security.SetNamedSecurityInfo(name, win32security.SE_FILE_OBJECT,
                        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                        None, None, acl, None)
                finally:
                    token.Close()
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
