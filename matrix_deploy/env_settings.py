"""Optional .env loading for prefill values (Qt-free).

Reads connection fields and, if present, secrets (SSH/sudo password and the
Artifactory token). Secrets loaded from .env are still kept out of the saved
settings file; the .env file itself is gitignored. Storing secrets here is a
convenience tradeoff (plaintext on disk) - leave them blank to keep the
in-memory-only posture and type them in the GUI each session.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Dict


# Recognized non-secret keys and the settings field they map to.
NON_SECRET_KEY_MAP = {
    "ROUTER_IP": "router_ip",
    "SSH_USERNAME": "username",
    "ARTIFACTORY_EMAIL": "artifactory_email",
    "JENKINS_USERNAME": "jenkins_username",
    "SWU_FILE": "swu_file",
    "CONFIG_TEMPLATE": "config_file",
}

# Secret keys and the field they map to. Loaded into the GUI but never written
# to the persisted settings file.
SECRET_KEY_MAP = {
    "SSH_PASSWORD": "ssh_password",
    "SUDO_PASSWORD": "sudo_password",
    # ARTIFACTORY_API_KEY is an accepted alias for ARTIFACTORY_TOKEN.
    "ARTIFACTORY_TOKEN": "artifactory_token",
    "ARTIFACTORY_API_KEY": "artifactory_token",
    "JENKINS_TOKEN": "jenkins_token",
}

ENV_KEY_MAP = {**NON_SECRET_KEY_MAP, **SECRET_KEY_MAP}


def default_env_path() -> Path:
    """Return the default ``.env`` location.

    When frozen, prefer a .env next to the executable so it can be edited
    after packaging.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidate = exe_dir / ".env"
        if candidate.exists():
            return candidate
        bundle_dir = Path(getattr(sys, "_MEIPASS", exe_dir))
        bundled = bundle_dir / ".env"
        if bundled.exists():
            return bundled
        return candidate
    return Path(__file__).resolve().parent.parent / ".env"


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a minimal ``KEY=value`` .env file into a dict.

    Supports comments (``#``), blank lines, optional ``export`` prefix, and
    single/double quoted values. Returns an empty dict if the file is missing
    or unreadable.
    """
    result: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _strip_quotes(value.strip())
        if key:
            result[key] = value
    return result


def _collect(raw: Dict[str, str], key_map: Dict[str, str]) -> Dict[str, str]:
    settings: Dict[str, str] = {}
    for env_key, field_name in key_map.items():
        value = raw.get(env_key, "").strip()
        if value:
            settings[field_name] = value
    return settings


def load_env_settings(path: Path | None = None) -> Dict[str, str]:
    """Load non-secret settings from a .env file.

    Returns a dict keyed by the internal settings field names (e.g.
    ``router_ip``), containing only present, non-empty, non-secret values.
    """
    raw = parse_env_file(path or default_env_path())
    return _collect(raw, NON_SECRET_KEY_MAP)


def load_env_secrets(path: Path | None = None) -> Dict[str, str]:
    """Load secret values from a .env file.

    Returns a dict keyed by field name (``ssh_password``, ``sudo_password``,
    ``artifactory_token``), containing only present, non-empty values. These
    are intended to prefill GUI fields and must never be persisted.
    """
    raw = parse_env_file(path or default_env_path())
    return _collect(raw, SECRET_KEY_MAP)
