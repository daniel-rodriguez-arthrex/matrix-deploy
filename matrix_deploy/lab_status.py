"""Lab Status: one read-only health snapshot per operating room.

For each room this SSHes in (services, uptime, NMS password from
act-mfg-eeprom), then logs into the room's Barco NMS through the router's
forwarded NMS port (11000 + room number, or the room's ``nms_api_base_url``)
and reads its devices and license over GraphQL - the same queries the
matrix-lab VS Code extension uses. Nothing on the room is changed.
"""

from __future__ import annotations

import concurrent.futures
import shlex
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

from .config import AppConfig, Room
from .deployer import Deployer, DeploymentCredentials
from .ssh_client import SSHError, connect, port_is_open, run_command
from . import tls_pins

# Room NMS uses a self-signed certificate: it's pinned instead (tls_pins.py).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

NMS_TIMEOUT = 20

STATUS_QUERY = """
query MatrixDeployLabStatus {
  devices {
    id type deviceClass deviceState licensed firmwareVersion upgradeRequired
    videoSinks { id friendlyName powerState connected }
    videoSources { id friendlyName connectorType connected }
  }
  license { features { featureName expired inGracePeriod permanent countTotal } }
}
"""

# Each device's network ports (IP + MAC), so the Network tab can name the
# MACs it sees on switch ports. Separate in case an NMS lacks these fields.
NETWORK_QUERY = """
query MatrixDeployLabNetwork {
  devices { id serialNumber networkInterfaces { name ip macAddress } }
}
"""

# Separate so an NMS without diagnostics still returns devices and license.
DIAGNOSTICS_QUERY = """
query MatrixDeployLabDiagnostics {
  devicesDiagnosticsV2 {
    deviceId
    system {
      cpuTemperature fpgaTemperature
      alerts { code category }
      fans { name speed pwm alerts { code category } }
    }
  }
}
"""


class NmsError(Exception):
    pass


class NmsLoginError(NmsError):
    pass


# Per-process caches keyed by NMS base URL, so a re-check skips the sudo
# act-mfg-eeprom read and the NMS login and can query the NMS while SSH runs.
# Memory only; never logged or written to disk.
_NMS_PASSWORDS: Dict[str, str] = {}
_NMS_TOKENS: Dict[str, str] = {}


def nms_base_url(config: AppConfig, room: Room) -> str:
    conn = config.connection
    return (room.nms_api_base_url or f"https://{conn.router_ip}:{room.nms_external_port(conn.nms_port_base)}").rstrip("/")


def _nms_login(config: AppConfig, base_url: str, password: str, http: Any = requests) -> str:
    conn = config.connection
    auth = http.post(
        f"{base_url}/api/authentication",
        json={"username": conn.nms_username, "password": password},
        verify=False, timeout=NMS_TIMEOUT,
    )
    if auth.status_code != 200:
        raise NmsLoginError(f"NMS login returned HTTP {auth.status_code}")
    token = auth.headers.get("Authorization", "")
    if token.lower().startswith("bearer "):
        token = token[7:]
    if not token:
        try:
            body = auth.json()
        except ValueError:
            body = auth.text
        token = body.get("token", "") if isinstance(body, dict) else str(body or "")
    if not token:
        raise NmsLoginError("NMS login did not return a token")
    return token


def _nms_query(config: AppConfig, base_url: str, token: str, query: str, http: Any = requests) -> Dict[str, Any]:
    resp = http.post(
        f"{base_url}{config.connection.nms_graphql_path}",
        json={"query": query},
        headers={"Authorization": f"Bearer {token}"},
        verify=False, timeout=NMS_TIMEOUT,
    )
    if resp.status_code != 200:
        raise NmsError(f"NMS query returned HTTP {resp.status_code}")
    result = resp.json()
    if result.get("errors"):
        raise NmsError("NMS query failed: " + "; ".join(e.get("message", "?") for e in result["errors"]))
    return result.get("data") or {}


def _nms_fetch(config: AppConfig, base_url: str, password: str) -> Tuple[Dict[str, Any], Any, Dict[str, Any]]:
    """Log in (reusing a cached token) and run the three queries side by side.
    Returns (status data, diagnostics list or the error text, network info by device id)."""
    http = tls_pins.pinned_session(base_url, NMS_TIMEOUT)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    try:
        token = _NMS_TOKENS.get(base_url)
        for fresh in ((False, True) if token else (True,)):
            if fresh:
                token = _nms_login(config, base_url, password, http)
                _NMS_TOKENS[base_url] = token
            status_f, diag_f, net_f = (pool.submit(_nms_query, config, base_url, token, q, http)
                                       for q in (STATUS_QUERY, DIAGNOSTICS_QUERY, NETWORK_QUERY))
            try:
                data = status_f.result()
                break
            except NmsError:
                if fresh:
                    raise
                # Cached token probably expired: log in again once.
        try:
            diags: Any = diag_f.result().get("devicesDiagnosticsV2") or []
        except Exception as exc:  # noqa: BLE001 - devices/license are still useful
            diags = str(exc)
        try:
            net = {x.get("id"): x for x in net_f.result().get("devices") or []}
        except Exception:  # noqa: BLE001 - only used to name devices on the Network tab
            net = {}
        return data, diags, net
    finally:
        # Don't wait for leftover queries after a failure.
        pool.shutdown(wait=False)


def _license_summary(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    active = [f for f in features if not f.get("expired")]
    grace = [f for f in active if f.get("inGracePeriod")]
    if not features:
        state = "none"
    elif not active:
        state = "expired"
    elif grace:
        state = "grace"
    else:
        state = "active"
    return {
        "state": state,
        "permanent": bool(active) and all(f.get("permanent") for f in active),
        "active_count": len(active),
        "total_count": len(features),
        "features": [
            {
                "name": f.get("featureName"),
                "expired": bool(f.get("expired")),
                "grace": bool(f.get("inGracePeriod")),
                "permanent": bool(f.get("permanent")),
                "count": f.get("countTotal"),
            }
            for f in features
        ],
    }


def _alert_text(code: str) -> str:
    """``CPU_TEMP_TOO_HIGH`` -> ``CPU temp too high``, ``FAN_SPEED_TOO_LOW`` -> ``Fan speed too low``."""
    text = " ".join(w if w in ("CPU", "FPGA", "SFP", "PSU") else w.lower() for w in str(code).split("_"))
    return text[:1].upper() + text[1:]


def _diagnostics_summary(diag: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    system = (diag or {}).get("system")
    if not system:
        return None
    fans = [
        {"name": f.get("name"), "rpm": f.get("speed"), "pwm": f.get("pwm"),
         "alerts": [_alert_text(a.get("code")) for a in f.get("alerts") or []]}
        for f in system.get("fans") or []
    ]
    return {
        "cpu_temp": system.get("cpuTemperature"),
        "fpga_temp": system.get("fpgaTemperature"),
        "fans": fans,
        "alerts": [_alert_text(a.get("code")) for a in system.get("alerts") or []]
                  + [f"{f['name']}: {a}" for f in fans for a in f["alerts"]],
    }


def _device_summary(d: Dict[str, Any], diag: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "id": d.get("id"),
        "model": d.get("type"),
        "kind": str(d.get("deviceClass") or "").upper() or "OTHER",
        "state": str(d.get("deviceState") or "UNKNOWN").upper(),
        "licensed": d.get("licensed"),
        "firmware": d.get("firmwareVersion"),
        "upgrade_required": bool(d.get("upgradeRequired")),
        "serial": d.get("serialNumber"),
        # Used by the Network tab to put names on MACs seen on switch ports.
        "interfaces": [
            {"name": i.get("name"), "ip": i.get("ip"), "mac": str(i.get("macAddress") or "").lower()}
            for i in d.get("networkInterfaces") or [] if i.get("macAddress")
        ],
        "inputs": [
            {"name": s.get("friendlyName") or s.get("id"), "label": s.get("friendlyName"),
             "connector": s.get("connectorType"), "connected": bool(s.get("connected"))}
            for s in d.get("videoSources") or []
        ],
        "outputs": [
            {"name": s.get("friendlyName") or s.get("id"), "label": s.get("friendlyName"),
             "power": s.get("powerState"), "connected": bool(s.get("connected"))}
            for s in d.get("videoSinks") or []
        ],
        "health": _diagnostics_summary(diag),
    }


def collect_room_status(config: AppConfig, creds: DeploymentCredentials, room: Room) -> Dict[str, Any]:
    """Read-only health snapshot for one room. Never raises; problems are
    reported in ``issues`` and reflected in ``state``
    (healthy / warning / error / unreachable)."""
    conn = config.connection
    status: Dict[str, Any] = {
        "number": room.number,
        "name": room.name,
        "room_id": room.room_id,
        "secondary": room.secondary,
        "state": "unreachable",
        "issues": [],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ssh": {"ok": False},
        "services": {},
        "nms": {"ok": False, "url": nms_base_url(config, room)},
        "license": None,
        "devices": [],
    }
    issues: List[str] = status["issues"]
    deployer = Deployer(config, creds, log=lambda m, lvl: None, progress=lambda s, t: None, is_cancelled=lambda: False)
    base = status["nms"]["url"]
    want_nms = not room.secondary and bool(creds.sudo_password)

    # Known NMS password: query the NMS now, alongside the SSH checks.
    nms_password = _NMS_PASSWORDS.get(base) if want_nms else None
    nms_future: Optional[concurrent.futures.Future] = None
    if nms_password:
        bg = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        nms_future = bg.submit(_nms_fetch, config, base, nms_password)
        bg.shutdown(wait=False)

    try:
        client = connect(deployer._target(room))
    except SSHError as exc:
        issues.append(f"Can't SSH in: {exc}")
        return status

    try:
        lines: List[str] = []
        run_command(client, "; ".join([
            "printf 'HOSTNAME='; hostname",
            "printf 'UPTIME='; uptime -p",
            f"printf 'API='; systemctl is-active {shlex.quote(conn.service_name)}",
            f"printf 'NMS='; systemctl is-active {shlex.quote(conn.nms_service_name)}",
            # Installed SWU: "2.0.0" and its build ("860-0108-07-0_2.0.0_MatrixG2-2.0-15").
            'echo "SWVER=$(cat /etc/software-version 2>/dev/null)"',
            "echo \"BUILD=$(sed -n 's/^Local Revision: *//p' /etc/arthrex-version 2>/dev/null)\"",
            "echo \"UPSECS=$(cut -d' ' -f1 /proc/uptime)\"",
        ]), on_line=lines.append)
        info = {k: v.strip() for k, v in (line.split("=", 1) for line in lines if "=" in line)}
        status["ssh"] = {"ok": True, "hostname": info.get("HOSTNAME", ""), "uptime": info.get("UPTIME", "")}
        try:
            # When the room last booted, so the UI can tell it rebooted between checks.
            status["ssh"]["booted_at"] = round(time.time() - float(info.get("UPSECS", "")))
        except ValueError:
            pass
        status["services"] = {"matrix_api": info.get("API") or "unknown", "nms": info.get("NMS") or "unknown"}
        status["software"] = {"version": info.get("SWVER") or None, "build": info.get("BUILD") or None}
        # Read the password over SSH if it isn't cached or was rejected
        # (e.g. the room was reimaged).
        if want_nms and (nms_future is None or isinstance(nms_future.exception(), NmsLoginError)):
            nms_future = None
            nms_password = deployer.fetch_nms_password(room, client)
    except Exception as exc:  # noqa: BLE001
        issues.append(f"SSH checks failed: {exc}")
    finally:
        client.close()

    for key, label, unit in (("matrix_api", "Matrix API", conn.service_name), ("nms", "Barco NMS", conn.nms_service_name)):
        state = status["services"].get(key)
        if state and state != "active":
            issues.append(f"{label} service ({unit}) is {state}")

    if room.secondary:
        status["nms"]["error"] = "Secondary room - no local NMS"
    elif not creds.sudo_password:
        status["nms"]["error"] = "Needs the sudo password (to read the NMS login)"
        issues.append(status["nms"]["error"])
    elif not nms_password:
        status["nms"]["error"] = "Couldn't read the NMS password (act-mfg-eeprom)"
        issues.append(status["nms"]["error"])
    else:
        try:
            data, diags, net = nms_future.result() if nms_future else _nms_fetch(config, base, nms_password)
            _NMS_PASSWORDS[base] = nms_password
            status["nms"]["ok"] = True
            status["license"] = _license_summary((data.get("license") or {}).get("features") or [])
            if isinstance(diags, str):
                status["nms"]["diagnostics_error"], diags = diags, []
            by_id = {x.get("deviceId"): x for x in diags}
            status["devices"] = [_device_summary({**d, **net.get(d.get("id"), {})}, by_id.get(d.get("id")))
                                 for d in data.get("devices") or []]
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, NmsLoginError):
                _NMS_PASSWORDS.pop(base, None)
                _NMS_TOKENS.pop(base, None)
            msg = str(exc)
            if isinstance(exc, requests.exceptions.SSLError):
                msg = tls_pins.describe_error(exc, base)
            elif isinstance(exc, requests.ConnectionError):
                host, _, port = base.split("//", 1)[-1].partition(":")
                reachable = port.isdigit() and port_is_open(host, int(port))
                msg = (f"NMS not answering at {base}" if reachable
                       else f"NMS port {port or 443} isn't reachable through the router (see Router Ports)")
            elif isinstance(exc, requests.Timeout):
                msg = f"NMS timed out at {base}"
            status["nms"]["error"] = msg
            issues.append(msg)

    lic = status["license"]
    if lic:
        if lic["state"] == "none":
            issues.append("No NMS license installed")
        elif lic["state"] == "expired":
            issues.append("NMS license expired")
        elif lic["state"] == "grace":
            issues.append("NMS license is in its grace period")
    for d in status["devices"]:
        label = f"{d['kind'].title()} {d['model'] or ''} ({d['id']})".replace("  ", " ")
        if d["state"] != "ONLINE":
            issues.append(f"{label} is {d['state'].lower()}")
        if d["licensed"] is False:
            issues.append(f"{label} is not licensed")
        if d["upgrade_required"]:
            issues.append(f"{label} needs a firmware upgrade")
        for alert in (d["health"] or {}).get("alerts", []):
            issues.append(f"{label}: {alert}")

    if not status["ssh"]["ok"]:
        status["state"] = "error"
    else:
        status["state"] = "warning" if issues else "healthy"
    return status
