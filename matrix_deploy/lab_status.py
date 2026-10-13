"""Lab Status: one read-only health snapshot per operating room.

For each room this SSHes in (services, uptime, NMS password from
act-mfg-eeprom), then logs into the room's Barco NMS through the router's
forwarded NMS port (11000 + room number, or the room's ``nms_api_base_url``)
and reads its devices and license over GraphQL - the same queries the
matrix-lab VS Code extension uses. Nothing on the room is changed.
"""

from __future__ import annotations

import concurrent.futures
import re
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
    networkInterfaces { name rxPower txPower }
  }
}
"""

# Barco's limits for MNA devices (MTBF metrics): (warn, critical).
# Temperatures (hottest of CPU/FPGA, °C) by model family: warn above, critical above.
TEMP_LIMITS = {"2": (75, 85, "MNA-240"), "4": (90, 95, "MNA-4x0")}
# Fan rpm against PWM x 100 (PWM is 0-100 %): warn below 75 %, critical below 65 %.
FAN_LIMITS = (0.75, 0.65)
# SFP Rx/Tx power (dBm): 0 to -3.99 ok, -4 to -6.99 warn, -7 or lower critical.
# Below -26 dBm the port has no light at all (unused / unplugged), not a weak link.
SFP_LIMITS = (-4.0, -7.0)
SFP_DARK = -26.0
_SEVERITY = {"ok": 0, "warn": 1, "bad": 2}

# Fiber Test: the device end of every fiber link (each NMS device's SFP light
# levels and receive errors per network port). Matched to switch ports by MAC.
SFP_QUERY = """
query MatrixDeployFiber {
  devicesDiagnosticsV2 {
    deviceId
    networkInterfaces { name rxPower txPower rxErrors sfpVendor sfpTemperature ipAddress macAddress alerts { code category } }
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
        # No redirects: a 307 would resend the password outside the pinned host.
        verify=False, timeout=NMS_TIMEOUT, allow_redirects=False,
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
        verify=False, timeout=NMS_TIMEOUT, allow_redirects=False,
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


def _grade_temp(model: str, t: Optional[float]) -> Optional[Dict[str, Any]]:
    m = re.search(r"MNA-?(\d)", model or "", re.I)
    limits = TEMP_LIMITS.get(m.group(1)) if m else None
    if t is None or not limits:
        return None
    warn, crit, family = limits
    state = "bad" if t > crit else "warn" if t > warn else "ok"
    return {"state": state, "limit": f"{family}: warning above {warn}\u00b0C, critical above {crit}\u00b0C",
            "rule": f"above {crit}\u00b0C" if state == "bad" else f"above {warn}\u00b0C"}


def _grade_fan(rpm: Optional[float], pwm: Optional[float]) -> Optional[Dict[str, Any]]:
    # PWM 0 = the fan isn't PWM-controlled (MNA-240 encoders report full speed at PWM 0).
    if rpm is None or not pwm:
        return None
    expected = pwm * 100
    ratio = rpm / expected
    warn, crit = FAN_LIMITS
    state = "bad" if ratio < crit else "warn" if ratio < warn else "ok"
    return {"state": state, "expected": round(expected), "percent": round(ratio * 100),
            "limit": f"PWM {pwm:g} % -> expected {expected:.0f} rpm; warning below {warn:.0%}, critical below {crit:.0%}",
            "rule": f"below {crit:.0%} of PWM x 100" if state == "bad" else f"below {warn:.0%} of PWM x 100"}


def _grade_sfp(v: Optional[float]) -> Optional[Dict[str, Any]]:
    if v is None:
        return None
    if v < SFP_DARK:
        return {"state": "dark"}
    v = round(v, 2)
    warn, crit = SFP_LIMITS
    state = "bad" if v <= crit else "warn" if v <= warn else "ok"
    return {"state": state, "rule": f"{crit:g} dBm or lower" if state == "bad" else f"{warn:g} to {crit + 0.01:g} dBm"}


def _diagnostics_summary(diag: Optional[Dict[str, Any]], model: str = "") -> Optional[Dict[str, Any]]:
    """Readings + NMS alerts, each graded against Barco's MNA limits.
    ``findings`` are the warn/critical ones ({state, text}); ``state`` is the worst."""
    system = (diag or {}).get("system")
    if not system:
        return None
    findings: List[Dict[str, str]] = []

    def find(state: str, text: str) -> None:
        if state in ("warn", "bad"):
            findings.append({"state": state, "text": f"{text} ({'critical' if state == 'bad' else 'warning'})"})

    temps = {"cpu": _number(system.get("cpuTemperature")), "fpga": _number(system.get("fpgaTemperature"))}
    grades = {k: _grade_temp(model, t) for k, t in temps.items()}
    for k, g in grades.items():
        if g:
            find(g["state"], f"{k.upper()} temperature {g['rule']}")
    fans = []
    for f in system.get("fans") or []:
        rpm, pwm = _number(f.get("speed")), _number(f.get("pwm"))
        g = _grade_fan(rpm, pwm)
        fan = {"name": f.get("name"), "rpm": f.get("speed"), "pwm": f.get("pwm"), "grade": g,
               "alerts": [_alert_text(a.get("code")) for a in f.get("alerts") or []]}
        fans.append(fan)
        if g:
            find(g["state"], f"{fan['name']} speed {g['rule']}")
    sfp = []
    for i in diag.get("networkInterfaces") or []:
        rx, tx = _number(i.get("rxPower")), _number(i.get("txPower"))
        if rx is None and tx is None:
            continue
        port = {"name": str(i.get("name") or ""), "rx": rx, "tx": tx, "rx_grade": _grade_sfp(rx), "tx_grade": _grade_sfp(tx)}
        sfp.append(port)
        for key in ("rx", "tx"):
            g = port[f"{key}_grade"]
            if g and g["state"] != "dark":
                find(g["state"], f"{port['name']} {key.capitalize()} power {g['rule']}")
    system_alerts = [_alert_text(a.get("code")) for a in system.get("alerts") or []]
    alerts = system_alerts + [f"{f['name']}: {a}" for f in fans for a in f["alerts"]]
    # NMS alerts on a reading Barco's limits already grade (temperature, fan
    # speed) are covered by that grade; any other NMS alert is a warning.
    graded_temp = {k.upper() for k, g in grades.items() if g}
    for a in system_alerts:
        if not any(a.startswith(f"{k} temp") for k in graded_temp):
            find("warn", a)
    for f in fans:
        if not f["grade"]:
            for a in f["alerts"]:
                find("warn", f"{f['name']}: {a}")
    state = max((x["state"] for x in findings), key=_SEVERITY.get, default="ok")
    return {
        "cpu_temp": system.get("cpuTemperature"),
        "fpga_temp": system.get("fpgaTemperature"),
        "temp_grades": grades,
        "fans": fans,
        "sfp": sfp,
        "alerts": alerts,
        "findings": findings,
        "state": state,
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
        "health": _diagnostics_summary(diag, str(d.get("type") or "")),
    }


def collect_room_sfp(config: AppConfig, creds: DeploymentCredentials, room: Room) -> Dict[str, Any]:
    """Fiber Test, read-only: each NMS device's SFP readings for one room
    (``devices: [{id, interfaces: [{name, ip, mac, rx, tx, rx_errors, vendor,
    temp, alerts}]}]``). Reuses the NMS password/token cached by the Facility
    check; reads the password over SSH (sudo) only when it isn't cached."""
    base = nms_base_url(config, room)
    out: Dict[str, Any] = {"number": room.number, "devices": []}
    if room.secondary:
        out["error"] = "Secondary room - no local NMS"
        return out
    password = _NMS_PASSWORDS.get(base)
    try:
        if not password:
            if not creds.sudo_password:
                out["error"] = "Needs the sudo password (to read the NMS login)"
                return out
            errors: List[str] = []
            deployer = Deployer(config, creds, log=lambda m, lvl: errors.append(m) if lvl == "error" else None,
                                progress=lambda s, t: None, is_cancelled=lambda: False)
            password = deployer.fetch_nms_password(room)
            if not password:
                # e.g. "Can't SSH in: ..." when the room is off or its host key changed.
                out["error"] = errors[0] if errors else "Couldn't read the NMS password (act-mfg-eeprom)"
                return out
        http = tls_pins.pinned_session(base, NMS_TIMEOUT)
        token = _NMS_TOKENS.get(base)
        for fresh in ((False, True) if token else (True,)):
            if fresh:
                token = _nms_login(config, base, password, http)
                _NMS_TOKENS[base] = token
            try:
                data = _nms_query(config, base, token, SFP_QUERY, http)
                break
            except NmsError:
                if fresh:
                    raise
        _NMS_PASSWORDS[base] = password
    except Exception as exc:  # noqa: BLE001
        out["error"] = _nms_error(exc, base)
        return out
    for d in data.get("devicesDiagnosticsV2") or []:
        out["devices"].append({"id": str(d.get("deviceId") or ""), "interfaces": [
            {"name": str(i.get("name") or ""), "ip": str(i.get("ipAddress") or ""), "mac": str(i.get("macAddress")).lower(),
             "rx": _number(i.get("rxPower")), "tx": _number(i.get("txPower")), "rx_errors": _number(i.get("rxErrors")),
             "vendor": str(i.get("sfpVendor") or ""), "temp": _number(i.get("sfpTemperature")),
             "alerts": [_alert_text(a.get("code")) for a in i.get("alerts") or []]}
            for i in d.get("networkInterfaces") or [] if i.get("macAddress")
        ]})
    return out


def _number(v: Any) -> Optional[float]:
    """NMS readings as numbers (or None), whatever type the NMS sends."""
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


NMS_NOT_FORWARDED = "isn't forwarded through the router"


def _nms_error(exc: Exception, base: str) -> str:
    """A readable NMS error. A changed certificate must stay visible as such
    (requests reports it as an SSLError, a kind of ConnectionError). A
    rejected login drops the cached password/token so the next check re-reads it."""
    if isinstance(exc, NmsLoginError):
        _NMS_PASSWORDS.pop(base, None)
        _NMS_TOKENS.pop(base, None)
    if isinstance(exc, requests.exceptions.SSLError):
        return tls_pins.describe_error(exc, base)
    if isinstance(exc, requests.ConnectionError):
        host, _, port = base.split("//", 1)[-1].partition(":")
        reachable = port.isdigit() and port_is_open(host, int(port))
        return (f"NMS not answering at {base}" if reachable
                else f"NMS port {port or 443} {NMS_NOT_FORWARDED}, so device details weren't read (see Network tab)")
    if isinstance(exc, requests.Timeout):
        return f"NMS timed out at {base}"
    return str(exc)


def collect_room_status(config: AppConfig, creds: DeploymentCredentials, room: Room) -> Dict[str, Any]:
    """Read-only health snapshot for one room. Never raises; problems are
    reported in ``issues`` and reflected in ``state``
    (healthy / warning / critical (a device past Barco's critical limit) /
    error / unreachable)."""
    conn = config.connection
    status: Dict[str, Any] = {
        "number": room.number,
        "name": room.name,
        "room_id": room.room_id,
        "secondary": room.secondary,
        "state": "unreachable",
        "issues": [],
        "notes": [],
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
            msg = _nms_error(exc, base)
            status["nms"]["error"] = msg
            (status["notes"] if NMS_NOT_FORWARDED in msg else issues).append(msg)

    lic = status["license"]
    if lic:
        if lic["state"] == "none":
            issues.append("No NMS license installed")
        elif lic["state"] == "expired":
            issues.append("NMS license expired")
        elif lic["state"] == "grace":
            issues.append("NMS license is in its grace period")
    critical = False
    for d in status["devices"]:
        label = f"{d['kind'].title()} {d['model'] or ''} ({d['id']})".replace("  ", " ")
        if d["state"] != "ONLINE":
            issues.append(f"{label} is {d['state'].lower()}")
        if d["licensed"] is False:
            issues.append(f"{label} is not licensed")
        if d["upgrade_required"]:
            issues.append(f"{label} needs a firmware upgrade")
        # Text without live readings, so "Changed since" doesn't churn on every check.
        for f in (d["health"] or {}).get("findings", []):
            issues.append(f"{label}: {f['text']}")
            critical = critical or f["state"] == "bad"

    if not status["ssh"]["ok"]:
        status["state"] = "error"
    else:
        status["state"] = "critical" if critical else "warning" if issues else "healthy"
    return status
