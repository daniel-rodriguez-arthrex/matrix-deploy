"""Live, read-only view of what's plugged into each lab switch (Extreme EXOS).

Per switch this runs ``show ports no-refresh`` (link up/down + speed),
``show iparp`` (IP + MAC per port: the switch is every room VLAN's gateway, so
its ARP table lists the room's devices), ``show fdb`` (MACs on ports with no
IP yet) and ``show lldp neighbors`` (named neighbours, e.g. other switches).
Nothing on the switch is changed.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from .ssh_client import SSHError, SSHTarget, connect, run_cli_show

COMMANDS = ("show ports no-refresh", "show iparp", "show fdb", "show lldp neighbors")

# MAC vendor prefixes seen in the Matrix labs (first 3 bytes).
VENDORS = {"00:04:a5": "Barco", "00:04:96": "Extreme", "00:05:a6": "Extron"}

_MAC = r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}"


def parse_ports(text: str) -> Dict[int, Dict[str, Any]]:
    """``show ports no-refresh``: link state (A = active, R = ready / no link,
    D = disabled) and actual speed per port."""
    ports: Dict[int, Dict[str, Any]] = {}
    for line in text.splitlines():
        m = re.match(r"^(\d+)\s+(?:\S.*?\s+)?(E|D)\s+(A|R|D|NP|L)\b\s*(\S+)?", line)
        if m:
            n, port_state, link, speed = m.groups()
            ports[int(n)] = {"enabled": port_state == "E", "link": link == "A",
                             "speed": speed if link == "A" and speed and speed[0].isdigit() else None}
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


def read_switch(host: str, port: int, username: str, password: str) -> Dict[str, Any]:
    """One switch's live port table: ``{"ports": {n: {link, speed,
    devices: [{ip, mac, vendor}], neighbor}}}``."""
    client = connect(SSHTarget(host=host, port=port, username=username, password=password), try_keys=False)
    try:
        out = run_cli_show(client, list(COMMANDS), setup=("disable clipaging",))
    finally:
        client.close()
    ports = parse_ports(out["show ports no-refresh"])
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
    return {"ports": {str(n): p for n, p in sorted(ports.items())}}
