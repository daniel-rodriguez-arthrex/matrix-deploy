"""Trust-on-first-use certificate pinning for the rooms' NMS (self-signed TLS).

CA verification can't work for the NMS's self-signed certificates, but
skipping verification entirely lets anyone on the lab network pose as an NMS
and collect its login. So the first certificate seen for host:port is pinned
(SHA-256) and later connections must present the same one. Pins live per
Windows user next to the SSH host keys (``ssh_client.KNOWN_HOSTS``) and are
cleared together with them by Actions > Remove Fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import threading
from typing import Dict
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from .ssh_client import KNOWN_HOSTS

PINS_PATH = KNOWN_HOSTS.with_name("tls_pins.json")
_LOCK = threading.Lock()


class PinError(requests.exceptions.SSLError):
    """The server's certificate isn't the one pinned for it."""


def _load() -> Dict[str, str]:
    try:
        data = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(pins: Dict[str, str]) -> None:
    PINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PINS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, PINS_PATH)


def _fetch_fingerprint(host: str, port: int, timeout: float) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
    except socket.timeout as exc:
        raise requests.Timeout(str(exc)) from exc
    except OSError as exc:
        raise requests.ConnectionError(str(exc)) from exc


def pin_for(base_url: str, timeout: float = 10) -> str:
    """The pinned SHA-256 for ``base_url``'s host:port, pinning it now if new."""
    u = urlparse(base_url)
    key = f"{u.hostname}:{u.port or 443}"
    with _LOCK:
        pins = _load()
        if key not in pins:
            pins[key] = _fetch_fingerprint(u.hostname, u.port or 443, timeout)
            _save(pins)
        return pins[key]


def forget(host: str, port: int) -> bool:
    with _LOCK:
        pins = _load()
        if pins.pop(f"{host}:{port}", None) is None:
            return False
        _save(pins)
    return True


class _PinnedAdapter(HTTPAdapter):
    def __init__(self, fingerprint: str, **kwargs):
        self._fingerprint = fingerprint
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_fingerprint"] = self._fingerprint
        super().init_poolmanager(*args, **kwargs)


def pinned_session(base_url: str, timeout: float = 10) -> requests.Session:
    """A Session for ``base_url`` that only talks to its pinned certificate.
    Requests must pass ``verify=False`` (the fingerprint replaces CA checks)."""
    session = requests.Session()
    session.mount(base_url.rstrip("/") + "/", _PinnedAdapter(pin_for(base_url, timeout)))
    return session


def describe_error(exc: Exception, base_url: str) -> str:
    """A readable message for a fingerprint mismatch, else ``str(exc)``."""
    if "fingerprint" in str(exc).lower():
        u = urlparse(base_url)
        return (f"Refused {u.hostname}:{u.port}: the NMS certificate changed since this computer last "
                "connected. If the room was re-imaged, use Actions > Remove Fingerprint for it; otherwise "
                "something may be impersonating the NMS.")
    return str(exc)
