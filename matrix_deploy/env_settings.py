"""``.env`` loading/saving for prefill values and saved credentials.

Reads non-secret fields and, if present, secrets (SSH/sudo passwords,
Artifactory/Jenkins tokens) from the shared root ``.env`` and each site
profile's sibling ``<lab>.env``. The web UI's credentials dialog writes back
here via ``save_env_values``. Storing secrets here is a convenience tradeoff
(plaintext on disk, gitignored) - leave them blank to type them into the UI
each session instead.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Dict, Tuple


# Recognized non-secret keys and the settings field they map to.
NON_SECRET_KEY_MAP = {
    "ROUTER_IP": "router_ip",
    "SSH_USERNAME": "username",
    "ARTIFACTORY_EMAIL": "artifactory_email",
    "JENKINS_USERNAME": "jenkins_username",
    "SWU_FILE": "swu_file",
    "BACKEND_REPO": "backend_repo",
    "WEB_REPO": "web_repo",
}

# Secret keys and the field they map to. ``MATRIX_*`` aliases match the per-lab
# credential files exported by the Matrix Lab VS Code extension
# (e.g. qa1lab.env / qa2lab.env) so those can be used verbatim as profile envs.
SECRET_KEY_MAP = {
    "SSH_PASSWORD": "ssh_password",
    "MATRIX_SSH_PASSWORD": "ssh_password",
    "SUDO_PASSWORD": "sudo_password",
    "MATRIX_SUDO_PASSWORD": "sudo_password",
    # ARTIFACTORY_API_KEY is an accepted alias for ARTIFACTORY_TOKEN.
    "ARTIFACTORY_TOKEN": "artifactory_token",
    "ARTIFACTORY_API_KEY": "artifactory_token",
    "JENKINS_TOKEN": "jenkins_token",
}

ENV_KEY_MAP = {**NON_SECRET_KEY_MAP, **SECRET_KEY_MAP}


def default_env_path() -> Path:
    """Return the default ``.env`` location.

    When frozen this is next to the executable (never inside the bundle), so
    it can be edited and saved to after packaging.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path(__file__).resolve().parent.parent / ".env"


def profile_env_path(profile_path) -> Path:
    """Return the sibling ``<stem>.env`` for a given profile JSON path.

    e.g. ``config/qa2lab.json`` -> ``config/qa2lab.env``. Lets each site/lab
    profile carry its own credential file (matching the Matrix Lab extension's
    per-lab env files) instead of one shared ``.env``.
    """
    p = Path(profile_path)
    return p.with_suffix(".env")


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
        # utf-8-sig: tolerate a BOM (Notepad / PowerShell 5 Set-Content).
        text = path.read_text(encoding="utf-8-sig")
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
        value = raw.get(env_key, "")
        if value.strip():
            settings[field_name] = value
    return settings


def _format_env_value(value: str) -> str:
    """Quote only when the parser would otherwise alter the value (edge
    whitespace, or a value that already looks quoted)."""
    looks_quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')
    if value == value.strip() and not looks_quoted:
        return value
    quote = '"' if '"' not in value else "'"
    return f"{quote}{value}{quote}"


def save_env_values(path: Path, values: Dict[str, str]) -> None:
    """Write field values (``ssh_password``, ``artifactory_email``, ...) into
    the .env at ``path``, creating it if needed. An existing line for the
    field - under any accepted alias, e.g. ``MATRIX_SSH_PASSWORD`` - is
    updated in place; everything else in the file is left untouched. Empty
    values are skipped (they never erase a saved value)."""
    pending = {f: v for f, v in values.items() if v}
    if not pending:
        return
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    written = set()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line[len("export "):] if line.startswith("export ") else line
        key = key.partition("=")[0].strip()
        field_name = ENV_KEY_MAP.get(key)
        if field_name in pending:
            lines[i] = f"{key}={_format_env_value(pending[field_name])}"
            written.add(field_name)
    canonical = {field_name: key for key, field_name in reversed(list(ENV_KEY_MAP.items()))}
    for field_name, value in pending.items():
        if field_name not in written:
            lines.append(f"{canonical[field_name]}={_format_env_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_env_settings(path: Path | None = None) -> Dict[str, str]:
    """Load non-secret settings from a .env file.

    Returns a dict keyed by the internal settings field names (e.g.
    ``router_ip``), containing only present, non-empty, non-secret values.
    """
    raw = parse_env_file(path or default_env_path())
    return _collect(raw, NON_SECRET_KEY_MAP)


def active_env_paths(profile_path=None) -> Tuple[Path, Path]:
    """``(root_env, profile_env)`` for a profile: the shared root ``.env`` and
    the profile's sibling ``<stem>.env`` if it exists (else the root again)."""
    root = default_env_path()
    if profile_path:
        sib = profile_env_path(profile_path)
        if sib.exists():
            return root, sib
    return root, root


def load_merged_env(profile_path=None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``(settings, secrets)`` from the root ``.env`` overlaid with the
    profile's sibling ``.env`` (profile wins where both set a value)."""
    root, profile = active_env_paths(profile_path)
    return (
        {**load_env_settings(root), **load_env_settings(profile)},
        {**load_env_secrets(root), **load_env_secrets(profile)},
    )


def load_env_secrets(path: Path | None = None) -> Dict[str, str]:
    """Load secret values from a .env file.

    Returns a dict keyed by field name (``ssh_password``, ``sudo_password``,
    ``artifactory_token``, ``jenkins_token``), containing only present,
    non-empty values, used to prefill the UI's credential fields.
    """
    raw = parse_env_file(path or default_env_path())
    return _collect(raw, SECRET_KEY_MAP)
