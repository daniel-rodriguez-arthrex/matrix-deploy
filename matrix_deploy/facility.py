"""Import a facility planner export (``<SITE>-version<N>.zip``) as a lab.

The planner zip holds everything that defines a site:

* ``metadata.json``                       facility name, site code, version
* ``puppet/Room<N>/matrix_configuration.json``  room -> switch / VLAN, secondary PC
* ``puppet/Room<N>/common.json``          room IPs (``api_service::cluster``) + secrets
* ``switches/<name>.xsf``                 Extreme EXOS config: VLANs, ports, IPs
* ``<SITE>-router-version<N>.txt``        Cisco router config: interfaces, NAT
* ``credentials/*.json``                  ``WindowsPassword``

Each zip becomes its own lab: a ``config/<site>.json`` profile (no secrets;
the switch/router layout goes under ``facility``) plus the matching
``config/<site>.env`` for the passwords. Nothing is extracted to disk and the
zip itself (incl. the NMS CA private key) is never stored.
"""

from __future__ import annotations

import io
import ipaddress
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_ZIP_BYTES = 50 * 1024 * 1024
# Uncompressed, across all entries: a small zip can't expand to gigabytes.
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_ENTRIES = 5000
FRONT_PORTS = 48  # Extreme x670G2-48x-4q: 48 SFP+ front ports + QSFP+ 49/53/57/61

_NAT_RE = re.compile(
    r"^ip nat inside source static (tcp|udp) (\S+) (\d+) (?:interface (\S+)|(\S+)) (\d+)"
)


class FacilityError(ValueError):
    """The upload isn't a usable facility planner export."""


def _ports(spec: str) -> List[int]:
    out: List[int] = []
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        if a.isdigit() and (not b or b.isdigit()):
            out.extend(range(int(a), int(b or a) + 1))
    return out


def _vlan_kind(name: str) -> Tuple[str, Optional[int]]:
    m = re.fullmatch(r"Room(\d+)", name)
    if m:
        return "room", int(m.group(1))
    if name.lower().startswith("router_uplink"):
        return "uplink", None
    if name.lower().startswith("backbone"):
        return "backbone", None
    return "other", None


def parse_switch_xsf(text: str) -> Dict[str, Any]:
    """VLANs, port membership and IPs from an EXOS ``.xsf`` script."""
    name, mgmt_ip, gateway = None, None, None
    vlans: Dict[str, Dict[str, Any]] = {}

    def vlan(n: str) -> Dict[str, Any]:
        kind, room = _vlan_kind(n)
        return vlans.setdefault(n, {"name": n, "kind": kind, "room": room, "tag": None, "ports": [],
                                    "tagged": False, "ip": None, "prefix": None, "description": None})

    for raw in text.splitlines():
        line = raw.strip()
        if m := re.match(r'configure snmp sysName "([^"]+)"', line):
            name = m.group(1)
        elif m := re.match(r'create vlan "?([^"\s]+)"?', line):
            vlan(m.group(1))
        elif m := re.match(r"configure iproute add default (\S+)", line):
            gateway = gateway or m.group(1)
        elif m := re.match(r'configure vlan "?([^"\s]+)"? (.*)$', line):
            vname, rest = m.groups()
            if vname.lower() == "mgmt":
                if mm := re.match(r"ipaddress (\S+)", rest):
                    mgmt_ip = mm.group(1)
                continue
            if vname.lower() == "default":
                continue
            v = vlan(vname)
            if mm := re.match(r"tag (\d+)", rest):
                v["tag"] = int(mm.group(1))
            elif mm := re.match(r"add ports (\S+)\s*(tagged|untagged)?", rest):
                v["ports"] = sorted(set(v["ports"]) | set(_ports(mm.group(1))))
                v["tagged"] = mm.group(2) == "tagged"
            elif mm := re.match(r"ipaddress (\S+) (\S+)", rest):
                v["ip"] = mm.group(1)
                v["prefix"] = ipaddress.IPv4Network(f"0.0.0.0/{mm.group(2)}").prefixlen
            elif mm := re.match(r'description "([^"]*)"', rest):
                v["description"] = mm.group(1)
    number = None
    if name and (m := re.search(r"(\d+)$", name)):
        number = int(m.group(1))
    return {
        "name": name,
        "number": number,
        "mgmt_ip": mgmt_ip,
        "default_gateway": gateway,
        "vlans": [v for v in vlans.values() if v["ports"] or v["kind"] != "other"],
    }


def parse_router_config(text: str) -> Dict[str, Any]:
    """Hostname, WAN/inside interfaces and static NAT rules from a Cisco config."""
    hostname = None
    interfaces: List[Dict[str, Any]] = []
    nat: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw in text.splitlines():
        if m := re.match(r"hostname (\S+)", raw):
            hostname = m.group(1)
        elif m := re.match(r"interface (\S+)$", raw):
            current = {"name": m.group(1), "ip": None, "nat": None}
            interfaces.append(current)
        elif raw.startswith(" ") and current is not None:
            line = raw.strip()
            if m := re.match(r"ip address (\d+\.\d+\.\d+\.\d+) ", line):
                current["ip"] = m.group(1)
            elif m := re.match(r"ip nat (inside|outside)$", line):
                current["nat"] = m.group(1)
        else:
            current = None
            if rule := parse_nat_line(raw.strip()):
                nat.append(rule)
    wan = next((i for i in interfaces if i["nat"] == "outside"), None)
    return {
        "hostname": hostname,
        "wan_interface": wan["name"] if wan else None,
        "wan_ip": wan["ip"] if wan else None,
        "inside_interfaces": [{"name": i["name"], "ip": i["ip"]} for i in interfaces if i["nat"] == "inside"],
        "nat": nat,
    }


def parse_nat_line(line: str) -> Optional[Dict[str, Any]]:
    m = _NAT_RE.match(line)
    if not m:
        return None
    proto, inside_ip, inside_port, iface, outside_ip, outside_port = m.groups()
    return {"proto": proto, "inside_ip": inside_ip, "inside_port": int(inside_port),
            "outside": iface or outside_ip, "outside_port": int(outside_port)}


def _read_json(z: zipfile.ZipFile, name: str) -> Any:
    try:
        return json.loads(z.read(name).decode("utf-8-sig"))
    except (KeyError, ValueError) as exc:
        raise FacilityError(f"{name} is missing or not valid JSON") from exc


def parse_facility_zip(data: bytes, filename: str = "") -> Dict[str, Any]:
    """Parse a planner export. Returns ``{"facility", "rooms", "secrets",
    "warnings"}``; ``secrets`` must only ever be written to the lab ``.env``."""
    if len(data) > MAX_ZIP_BYTES:
        raise FacilityError("File is too large to be a facility config export.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FacilityError("That file isn't a .zip.") from exc
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise FacilityError("The zip unpacks to too much data to be a facility config export.")
    names = z.namelist()
    if "metadata.json" not in names:
        raise FacilityError("No metadata.json - this doesn't look like a facility config export.")
    meta = _read_json(z, "metadata.json")
    site_code = str(meta.get("metadata::site_code") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", site_code):
        raise FacilityError("metadata.json has no usable site_code.")
    warnings: List[str] = []

    switches = []
    for n in sorted(names):
        if re.fullmatch(r"switches/[^/]+\.xsf", n):
            switches.append(parse_switch_xsf(z.read(n).decode("utf-8", "replace")))
    switches.sort(key=lambda s: (s["number"] is None, s["number"] or 0))

    router_files = [n for n in names if re.fullmatch(r"[^/]+-router-version\d+\.txt", n)]
    router = parse_router_config(z.read(router_files[0]).decode("utf-8", "replace")) if router_files else None
    if not router or not router["wan_ip"]:
        raise FacilityError("No router config with a WAN (ip nat outside) interface in the zip.")

    room_nets: Dict[int, ipaddress.IPv4Network] = {}
    for sw in switches:
        for v in sw["vlans"]:
            if v["kind"] == "room" and v["ip"] and v["prefix"]:
                room_nets[v["room"]] = ipaddress.IPv4Network(f"{v['ip']}/{v['prefix']}", strict=False)

    rooms: List[Dict[str, Any]] = []
    room_meta: Dict[str, Dict[str, Any]] = {}
    secrets: Dict[str, str] = {}
    room_dirs = sorted({int(m.group(1)) for n in names if (m := re.fullmatch(r"puppet/Room(\d+)/matrix_configuration\.json", n))})
    for number in room_dirs:
        cfg = _read_json(z, f"puppet/Room{number}/matrix_configuration.json")
        common = _read_json(z, f"puppet/Room{number}/common.json") if f"puppet/Room{number}/common.json" in names else {}
        cluster = common.get("mg2puppet::api_service::cluster") or []
        net = room_nets.get(int(cfg.get("vlan_number") or number))
        ip = next((c for c in cluster if net and ipaddress.IPv4Address(c) in net), None)
        if not ip:
            warnings.append(f"Room {number}: couldn't find its NMS IP (no cluster address in its VLAN subnet) - skipped.")
            continue
        secondary = bool(cfg.get("secondary_pc"))
        rooms.append({"number": number, "room_id": ip, "name": f"OR {number}" + (" Secondary" if secondary else ""),
                      **({"secondary": True} if secondary else {})})
        room_meta[str(number)] = {
            "switch": cfg.get("switch_number"),
            "vlan": cfg.get("vlan_number"),
            "switch_model": cfg.get("switch_model"),
            "extron": common.get("mg2puppet::api_service::extrondevices") or [],
        }
        for env_field, key in (("nms_password", "nmspassword"), ("extron_password", "extronpassword"),
                               ("site_api_key", "apikey"), ("ic_key", "ickey")):
            if common.get(f"mg2puppet::api_service::{key}"):
                secrets.setdefault(env_field, str(common[f"mg2puppet::api_service::{key}"]))
    if not rooms:
        raise FacilityError("No rooms found under puppet/Room<N>/.")

    models = {m["switch"]: m["switch_model"] for m in room_meta.values() if m["switch"]}
    for sw in switches:
        sw["model"] = models.get(sw["number"])

    for n in names:
        if re.fullmatch(r"credentials/[^/]+\.json", n):
            creds = _read_json(z, n)
            if isinstance(creds, dict) and creds.get("WindowsPassword"):
                secrets["windows_password"] = str(creds["WindowsPassword"])

    external_apps = []
    if "externalapps.json" in names:
        external_apps = [{"name": a.get("name"), "url": a.get("url")}
                         for a in _read_json(z, "externalapps.json") if isinstance(a, dict) and a.get("url")]

    facility = {
        "site_code": site_code,
        "facility_name": meta.get("metadata::FacilityName") or site_code,
        "version": str(meta.get("metadata::VersionNumber") or ""),
        "source_file": Path(filename).name if filename else None,
        "router": router,
        "switches": switches,
        "rooms": room_meta,
        "external_apps": external_apps,
    }
    return {"facility": facility, "rooms": rooms, "secrets": secrets, "warnings": warnings}


def profile_path_for(site_code: str, config_dir: Path) -> Path:
    return config_dir / f"{site_code.lower()}.json"


def build_profile(parsed: Dict[str, Any], base: Optional[Dict[str, Any]], artifactory: Dict[str, Any],
                  qa_port_forwards: bool) -> Dict[str, Any]:
    """The profile JSON for a parsed export. ``base`` is the existing profile
    being updated (keeps its name, connection settings and per-room URL
    overrides), or ``None`` for a new lab."""
    fac, router = parsed["facility"], parsed["facility"]["router"]
    old_rooms = {r.get("number"): r for r in (base or {}).get("rooms", [])}
    rooms = []
    for r in parsed["rooms"]:
        keep = {k: v for k, v in old_rooms.get(r["number"], {}).items() if k in ("nms_ui_url", "nms_api_base_url")}
        rooms.append({**r, **keep})
    if base:
        profile = dict(base)
        connection = dict(base.get("connection", {}))
    else:
        profile = {"site": {"name": f"{fac['facility_name']} ({fac['site_code']})"}, "artifactory": artifactory}
        connection = {"ssh_username": "arthrex", "ssh_port_base": 200, "same_physical_host": False}
    connection.update({"router_ip": router["wan_ip"], "router_wan_interface": router["wan_interface"],
                       "qa_port_forwards": qa_port_forwards})
    profile.update({"connection": connection, "rooms": rooms, "facility": fac})
    return profile


def diff_summary(old_fac: Optional[Dict[str, Any]], old_rooms: List[Dict[str, Any]], parsed: Dict[str, Any]) -> List[str]:
    """Human-readable changes vs. the profile being updated."""
    new_fac = parsed["facility"]
    changes: List[str] = []
    if old_fac and old_fac.get("version") != new_fac["version"]:
        changes.append(f"Version {old_fac.get('version') or '?'} -> {new_fac['version']}")
    old = {r["number"]: r["room_id"] for r in old_rooms}
    new = {r["number"]: r["room_id"] for r in parsed["rooms"]}
    for n in sorted(set(new) - set(old)):
        changes.append(f"Room {n} added ({new[n]})")
    for n in sorted(set(old) - set(new)):
        changes.append(f"Room {n} removed")
    for n in sorted(set(old) & set(new)):
        if old[n] != new[n]:
            changes.append(f"Room {n} IP {old[n]} -> {new[n]}")
    if old_fac:
        layout = lambda f: [(s["name"], [(v["name"], v["ports"]) for v in s["vlans"]]) for s in f.get("switches", [])]
        if layout(old_fac) != layout(new_fac):
            changes.append("Switch VLAN / port layout changed")
        nat = lambda f: sorted((r["inside_ip"], r["inside_port"], r["outside_port"]) for r in f.get("router", {}).get("nat", []))
        if nat(old_fac) != nat(new_fac):
            changes.append("Router NAT rules in the planner changed")
    return changes
