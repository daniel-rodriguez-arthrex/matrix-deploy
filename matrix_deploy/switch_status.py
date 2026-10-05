"""Live, read-only view of what's plugged into each lab switch (Extreme EXOS).

Per switch this runs ``show ports configuration no-refresh`` (link up/down +
speed; same data as ``show ports no-refresh`` but ~0.7 s instead of ~4.4 s on
QALAB-Switch1),
``show iparp`` (IP + MAC per port: the switch is every room VLAN's gateway, so
its ARP table lists the room's devices), ``show fdb`` (MACs on ports with no
IP yet) and ``show lldp neighbors`` (named neighbours, e.g. other switches).
The fiber test adds ``show ports transceiver information detail`` (SFP light
levels with each module's own warn/alarm thresholds) and the rx/tx error
counters. Nothing on the switch is changed (counters aren't cleared either).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .ssh_client import SSHError, SSHTarget, connect, run_cli_show

PORTS_COMMAND = "show ports configuration no-refresh"
COMMANDS = (PORTS_COMMAND, "show iparp", "show fdb", "show lldp neighbors")
FIBER_COMMANDS = ("show ports transceiver information detail", "show ports rxerrors no-refresh",
                  "show ports txerrors no-refresh")

# MAC vendor prefixes seen in the Matrix labs (first 3 bytes).
VENDORS = {"00:04:a5": "Barco", "00:04:96": "Extreme", "00:05:a6": "Extron"}

_MAC = r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}"


def parse_ports(text: str) -> Dict[int, Dict[str, Any]]:
    """``show ports configuration no-refresh``: port state (E/D), link state
    (A = active, R = ready / no link, NP = not present, D = disabled) and the
    actual speed, as ``"1000"`` / ``"10G"`` / ``"40G"`` like ``show ports``.
    Columns: port, virtual router, port state, link state, auto-neg, configured
    speed, actual speed (only when the link is up), ..."""
    ports: Dict[int, Dict[str, Any]] = {}
    for line in text.splitlines():
        m = re.match(r"^(\d+)\s+\S+\s+(E|D)\s+(A|R|NP|D|L)\s+(?:ON|OFF)\s+\S+(?:\s+(\d+))?", line)
        if m:
            n, port_state, link, actual = m.groups()
            speed = None
            if link == "A" and actual:
                speed = f"{int(actual) // 1000}G" if int(actual) >= 10000 else actual
            ports[int(n)] = {"enabled": port_state == "E", "link": link == "A", "speed": speed}
    return ports


def parse_iparp(text: str) -> List[Dict[str, Any]]:
    out = []
    for line in text.splitlines():
        m = re.match(rf"^\S+\s+(\d+\.\d+\.\d+\.\d+)\s+({_MAC})\s+\d+\s+\S+\s+(\S+)\s+\d+\s+(\d+)\s*$", line.strip())
        if m:
            ip, mac, vlan, port = m.groups()
            out.append({"ip": ip, "mac": mac.lower(), "vlan": vlan, "port": int(port)})
    return out


def parse_fdb(text: str) -> List[Dict[str, Any]]:
    out = []
    for line in text.splitlines():
        m = re.match(rf"^({_MAC})\s+(\S+?)\(\d+\)\s+\d+\s+.*?\s(\d+)\s*$", line.strip())
        if m:
            mac, vlan, port = m.groups()
            out.append({"mac": mac.lower(), "vlan": vlan, "port": int(port)})
    return out


def parse_lldp(text: str) -> Dict[int, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(rf"^(\d+)\s+({_MAC})\s+\S+\s+\d+\s+\d+\s+(\S.*)$", line.strip())
        if m:
            out[int(m.group(1))] = m.group(3).strip()
    return out


def vendor(mac: str) -> str:
    return VENDORS.get(mac[:8].lower(), "")


def _num(s: str) -> Optional[float]:
    try:
        return float(s)
    except ValueError:
        return None  # "N/A", "-inf"


_DDM_KEYS = {"Temp": "temp", "Voltage": "voltage", "Tx Power": "tx_power", "Rx Power": "rx_power", "Tx Bias": "tx_bias"}
_INFO_KEYS = {"Media Type": "media", "Vendor Name": "vendor", "Part Number": "part", "Serial Number": "serial",
              "Wavelength": "wavelength"}


def _severity(status: str) -> int:
    s = status.lower()
    return 2 if "alarm" in s else 1 if "warn" in s else 0


def parse_transceivers(text: str) -> Dict[int, Dict[str, Any]]:
    """``show ports transceiver information detail``: per port the module
    (media, vendor, part, serial, wavelength) and its DDMI readings
    (``ddm: {rx_power: {value, status, low_warn, high_warn, low_alarm,
    high_alarm}, ...}``) as judged by the module's own thresholds. Ports
    without a module get ``present: False``; copper/DAC modules ``ddmi: False``."""
    out: Dict[int, Dict[str, Any]] = {}
    cur: Optional[Dict[str, Any]] = None
    metric: Optional[Dict[str, Any]] = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^Port\s*:\s*(\d+)\b", line)
        if m:
            cur = out.setdefault(int(m.group(1)), {"present": True, "ddmi": False})
            metric = None
            continue
        if cur is None:
            continue
        m = re.match(r"^Error:\s*(.*?)\s*$", line)
        if m:
            if "not present" in m.group(1):
                cur["present"] = False
            else:
                media = re.search(r"on this (\S+) transceiver", m.group(1))
                cur["media"] = media.group(1) if media else None
            continue
        m = re.match(r"^(Media Type|Vendor Name|Part Number|Serial Number|Wavelength)\s*:\s*(.*?)\s*$", line)
        if m:
            value = m.group(2)
            if m.group(1) == "Media Type":
                cur["supported"] = "(Unsupported)" not in value
                value = value.replace("(Unsupported)", "").strip()
            cur[_INFO_KEYS[m.group(1)]] = value
            continue
        m = re.match(r"^(Temp|Voltage|Tx Power|Rx Power|Tx Bias)[^:]*:\s*(\S+)\s+Status\s*:\s*(.*?)\s*$", line)
        if m:
            key = _DDM_KEYS[m.group(1)]
            new = {"value": _num(m.group(2)), "status": m.group(3)}
            ddm = cur.setdefault("ddm", {})
            # Multi-lane modules list a reading per lane: keep the worst one.
            if key not in ddm or _severity(new["status"]) > _severity(ddm[key]["status"]):
                ddm[key] = new
                metric = new
            else:
                metric = None
            cur["ddmi"] = True
            continue
        m = re.match(r"^Low (Warn|Alarm) Threshold\s*:\s*(\S+)\s+High (?:Warn|Alarm) Threshold\s*:\s*(\S+)", line)
        if m and metric is not None:
            kind = m.group(1).lower()
            metric[f"low_{kind}"] = _num(m.group(2))
            metric[f"high_{kind}"] = _num(m.group(3))
    return out


_RX_ERRORS = ("crc", "over", "under", "frag", "jabber", "align", "lost")
_TX_ERRORS = ("coll", "late_coll", "deferred", "errors", "lost", "parity")


def parse_error_counters(text: str, names: tuple) -> Dict[int, Dict[str, int]]:
    """``show ports rxerrors|txerrors no-refresh``: cumulative counters per
    port (since the switch booted or the counters were last cleared)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\d+)\s+(?:A|R|NP|D|L)\s+((?:\d+\s+){%d}\d+)\s*$" % (len(names) - 1), line.strip())
        if m:
            out[int(m.group(1))] = dict(zip(names, map(int, m.group(2).split())))
    return out


def read_switch(host: str, port: int, username: str, password: str, fiber: bool = False) -> Dict[str, Any]:
    """One switch's live port table: ``{"ports": {n: {link, speed,
    devices: [{ip, mac, vendor}], neighbor}}}``. With ``fiber`` each port also
    gets ``optic`` (module + light levels) and ``errors`` (``{rx, tx}``)."""
    # EXOS only offers SHA-1 ssh-rsa host keys.
    client = connect(SSHTarget(host=host, port=port, username=username, password=password), try_keys=False, sha1="allow")
    try:
        out = run_cli_show(client, list(COMMANDS) + (list(FIBER_COMMANDS) if fiber else []), setup=("disable clipaging",))
    finally:
        client.close()
    ports = parse_ports(out[PORTS_COMMAND])
    if not ports:
        raise SSHError("The switch answered, but its port list couldn't be read.")
    for p in ports.values():
        p["devices"] = []
    seen = set()
    for e in parse_iparp(out["show iparp"]):
        if e["port"] in ports:
            ports[e["port"]]["devices"].append({"ip": e["ip"], "mac": e["mac"], "vendor": vendor(e["mac"])})
            seen.add(e["mac"])
    for e in parse_fdb(out["show fdb"]):
        if e["port"] in ports and e["mac"] not in seen:
            ports[e["port"]]["devices"].append({"ip": None, "mac": e["mac"], "vendor": vendor(e["mac"])})
    for n, name in parse_lldp(out["show lldp neighbors"]).items():
        if n in ports:
            ports[n]["neighbor"] = name
    if fiber:
        optics = parse_transceivers(out["show ports transceiver information detail"])
        rx = parse_error_counters(out["show ports rxerrors no-refresh"], _RX_ERRORS)
        tx = parse_error_counters(out["show ports txerrors no-refresh"], _TX_ERRORS)
        if not optics:
            raise SSHError("The switch answered, but its transceiver list couldn't be read.")
        for n, p in ports.items():
            if n in optics:
                p["optic"] = optics[n]
            if n in rx or n in tx:
                p["errors"] = {"rx": rx.get(n, {}), "tx": tx.get(n, {})}
    return {"ports": {str(n): p for n, p in sorted(ports.items())}}
