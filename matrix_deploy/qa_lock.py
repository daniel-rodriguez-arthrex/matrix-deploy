"""QA lock: the QA-only features (Tunnels, Config, Software Update, Web App)
need a shared QA password.

An app-level lock: the server refuses those features until the password is
entered, so picking "QA" in Settings or calling the API isn't enough. It does
not stop someone who uses the room's sudo password over plain SSH.

``config/qa_lock.cfg`` never holds the password: a salt, a PBKDF2 hash to
check the password, and the SHA-256 of the "remember this computer" token.
build_exe.ps1 bakes it into the packaged app (``_internal``), where it's the
only lock that counts, so deleting or replacing a file in the app's config
folder doesn't unlock anything. That token lives only in the root ``.env``
of a computer where a QA user ticked "remember" (the root .env never ships).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import config_dir
from .env_settings import default_env_path, parse_env_file, save_env_values

# The hash ships in every build, so it can be attacked offline: OWASP's
# PBKDF2-SHA256 work factor and a long minimum length. Existing lock files
# keep the iteration count they were made with.
ITERATIONS = 600_000
MIN_LENGTH = 12
TOKEN_KEY = "MATRIX_QA_TOKEN"
_CHECK_LOCK = threading.Lock()


def lock_path() -> Path:
    return config_dir() / "qa_lock.cfg"


def builtin_lock_path() -> Optional[Path]:
    """The copy build_exe.ps1 bakes into the packaged app (``_internal``), so
    deleting ``config/qa_lock.cfg`` from the app folder doesn't unlock it."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "matrix_deploy" / "qa_lock.cfg" if base else None


def _derive(password: str, salt_hex: str, purpose: str, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", f"{purpose}:{password}".encode(), bytes.fromhex(salt_hex), iterations).hex()


def _read_lock(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return lock if isinstance(lock, dict) and lock.get("salt") and lock.get("verify") else None


def has_builtin_lock() -> bool:
    path = builtin_lock_path()
    return bool(path and path.exists())


def load_lock() -> Optional[Dict[str, Any]]:
    """The built-in lock of a packaged build (the only one that counts there,
    so a hand-made ``config/qa_lock.cfg`` can't replace it), else the lock set
    in Settings (``config/qa_lock.cfg``)."""
    if has_builtin_lock():
        return _read_lock(builtin_lock_path())
    return _read_lock(lock_path())


_BUILTIN_MSG = ("This build has the QA lock built in, so it can't be changed here. Change it in Settings "
                "on the computer that builds the app (python run_server.py), then rebuild.")


def is_configured() -> bool:
    # A lock file that exists but can't be read stays locked (fail closed);
    # no password can unlock it, so delete the file to reset the lock. A
    # packaged build with a built-in lock stays locked even then.
    return load_lock() is not None or lock_path().exists() or has_builtin_lock()


def check_password(password: str) -> bool:
    lock = load_lock()
    if not lock or not password:
        return False
    with _CHECK_LOCK:  # one guess at a time, however many requests arrive
        got = _derive(password, lock["salt"], "verify", int(lock.get("iterations", ITERATIONS)))
        ok = hmac.compare_digest(got, lock["verify"])
        if not ok:
            time.sleep(1)  # slow down guessing
    return ok


def remember_token(password: str) -> str:
    lock = load_lock() or {}
    return _derive(password, lock["salt"], "remember", int(lock.get("iterations", ITERATIONS)))


def set_password(password: str) -> str:
    """Write a new lock file; returns this computer's remember token."""
    if has_builtin_lock():
        raise ValueError(_BUILTIN_MSG)
    salt = secrets.token_hex(16)
    token = _derive(password, salt, "remember")
    lock_path().write_text(json.dumps({
        "version": 1,
        "iterations": ITERATIONS,
        "salt": salt,
        "verify": _derive(password, salt, "verify"),
        "remember": hashlib.sha256(token.encode()).hexdigest(),
    }, indent=2) + "\n", encoding="utf-8")
    return token


def remove_lock() -> None:
    if has_builtin_lock():
        raise ValueError(_BUILTIN_MSG)
    lock_path().unlink(missing_ok=True)


def remembered() -> bool:
    """True if this computer saved a valid "remember" token for the current lock."""
    lock = load_lock()
    token = parse_env_file(default_env_path()).get(TOKEN_KEY, "")
    if not lock or not token or not lock.get("remember"):
        return False
    return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), lock["remember"])


def save_token(token: str) -> None:
    """Remember (token) or forget ("") this computer in the root .env."""
    save_env_values(default_env_path(), {"qa_token": token}, allow_clear=True)
