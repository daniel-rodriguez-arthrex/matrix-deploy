"""Encrypt saved passwords/tokens with Windows DPAPI (current user).

Values in the ``.env`` files are stored as ``dpapi:<base64>``: only the same
Windows account on the same computer can decrypt them, so a copied ``.env``
(backup, OneDrive, a shared folder) doesn't leak the secrets. Plaintext
values still load and are encrypted on the next start (see
``env_settings.protect_env_file``). Off Windows, values stay as they are.
"""

from __future__ import annotations

import base64
import ctypes
import sys
from typing import Optional

PREFIX = "dpapi:"
# Ties the ciphertext to this app (other DPAPI users can't decrypt it without
# it). Changing it makes every saved value unreadable.
_ENTROPY = b"matrix-facility-manager"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
AVAILABLE = sys.platform == "win32"

if AVAILABLE:
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _SIG = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.argtypes = _SIG
    _crypt32.CryptUnprotectData.argtypes = _SIG
    _crypt32.CryptProtectData.restype = _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _call(fn, data: bytes) -> bytes:
    buf, ent = ctypes.create_string_buffer(data, len(data)), ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_ent = _Blob(len(_ENTROPY), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None,
              _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def is_protected(value: str) -> bool:
    return value.startswith(PREFIX)


def protect(value: str) -> str:
    """``dpapi:<base64>`` for ``value`` (unchanged if empty, already
    protected, or not on Windows)."""
    if not AVAILABLE or not value or is_protected(value):
        return value
    return PREFIX + base64.b64encode(_call(_crypt32.CryptProtectData, value.encode("utf-8"))).decode("ascii")


def unprotect(value: str) -> Optional[str]:
    """Plain text for a stored value; ``None`` if it can't be decrypted here
    (saved by another Windows user or computer)."""
    if not is_protected(value):
        return value
    if not AVAILABLE:
        return None
    try:
        return _call(_crypt32.CryptUnprotectData, base64.b64decode(value[len(PREFIX):])).decode("utf-8")
    except (OSError, ValueError):
        return None
