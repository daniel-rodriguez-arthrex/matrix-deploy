"""The router's groups, rules and policies (besides the static NAT forwards).

``parse`` reads a Cisco IOS config (the facility planner's router file, or the
live ``show running-config``) into what a Matrix site router normally has:

* object groups (Bomgar / Datadog proxies / external apps)
* the outbound access list the NAT overload uses (``NMS-NAT-OUT``)
* NAT policies (``ip nat inside source list ... overload``)
* static routes (default route, each room subnet via the switch uplinks)
* interfaces (customer network = NAT outside, switch uplink VLANs = inside)
* hardening lines (no CDP / HTTP server, SSH v2 only, ...)

Only those constructs are kept: account lines, secrets, keys and
certificates are never parsed, so nothing secret reaches the browser.
``compare`` checks the plan against the live router and builds copy-only
commands for what's missing (Apply to Router stays limited to static NAT).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Optional

# Used when the lab has no planner router config stored (imported before this
# was kept, or not created from a planner export): the parts every Matrix site
# router gets. Site-specific parts (routes, interfaces, external-apps hosts)
# aren't compared then.
STANDARD_CONFIG = """\
service timestamps debug datetime msec
service timestamps log datetime msec
service password-encryption
no cdp run
no service dhcp
no ip bootp server
no service pad
service tcp-keepalives-in
service tcp-keepalives-out
aaa authentication login default local
object-group network external-apps
 description External applications accessed from the PC
!
object-group network act-ddproxy-dev
 description Proxy hosts for logging and RUM
 host 34.199.15.219
 host 3.208.245.193
 host 3.88.121.120
 host 18.211.134.63
 host 18.235.207.51
 host 3.92.120.254
 host 52.200.141.245
 host 35.174.77.147
 host 34.236.180.142
!
object-group network act-ddproxy-prod
 description Proxy hosts for logging and RUM
 host 3.211.43.233
 host 3.93.125.251
 host 18.209.196.197
 host 3.209.140.226
 host 52.4.147.65
 host 54.156.106.29
 host 35.172.202.121
 host 18.233.61.1
 host 107.21.14.89
!
no ip http server
no ip http secure-server
ip ssh version 2
!
ip access-list extended NMS-NAT-OUT
 ! - Bomgar
 permit tcp any host 205.167.79.90 eq 443
 permit tcp any host 205.167.79.90 eq 8200
 permit tcp any host 10.106.124.90 eq 443
 permit tcp any host 10.106.124.90 eq 8200
 ! - Datadog
 permit tcp any object-group act-ddproxy-dev eq 443
 permit tcp any object-group act-ddproxy-prod eq 443
 ! - External Apps
 permit tcp any object-group external-apps eq www
 permit tcp any object-group external-apps eq 443
 permit tcp any object-group external-apps eq 8081
 permit tcp any object-group external-apps eq 8443
 permit tcp any object-group external-apps eq 8087
 permit tcp any object-group external-apps eq 1935
 permit tcp any object-group external-apps eq 10443
 permit tcp any object-group external-apps eq 1990
 permit udp any object-group external-apps eq 8000
!
line vty 0 4
 exec-timeout 30 0
 transport input ssh
line vty 5 15
 exec-timeout 30 0
 transport input ssh
"""

# Groups whose hosts are different at every site: in the standard template
# only the group itself is expected.
SITE_SPECIFIC_GROUPS = {"external-apps"}

# Global hardening lines a Matrix router config usually has, in plain English.
HARDENING = [
    ("service password-encryption", "Passwords in the config are stored encrypted"),
    ("no cdp run", "CDP (Cisco device discovery) is off"),
    ("no service dhcp", "The router doesn't hand out DHCP addresses"),
    ("no ip bootp server", "BOOTP server is off"),
    ("no service pad", "PAD service is off"),
    ("no ip http server", "Web admin page (HTTP) is off"),
    ("no ip http secure-server", "Web admin page (HTTPS) is off"),
    ("ip ssh version 2", "SSH version 2 only"),
    ("service tcp-keepalives-in", "Dead incoming sessions are cleaned up (TCP keepalives in)"),
    ("service tcp-keepalives-out", "Dead outgoing sessions are cleaned up (TCP keepalives out)"),
    ("aaa authentication login default local", "Logins use the router's own accounts"),
    ("no logging message-counter syslog", "No message counters in syslog"),
    ("no service sequence-numbers", "No sequence numbers in log messages"),
]
VTY_CHECKS = [
    ("vty-ssh", "transport input ssh", "Remote logins only over SSH (no Telnet)"),
    ("vty-timeout", "exec-timeout", "Idle remote sessions log out"),
]

# Well-known names IOS shows instead of port numbers ("eq www").
_PORT_NAMES = {"www": "80", "http": "80", "https": "443", "ftp": "21", "ssh": "22", "telnet": "23",
               "smtp": "25", "domain": "53", "ntp": "123", "snmp": "161", "syslog": "514", "bootps": "67",
               "bootpc": "68", "tftp": "69", "pop3": "110", "nntp": "119", "irc": "194"}
_SERVICE = {"80": "web (HTTP)", "443": "HTTPS", "8200": "Bomgar jump client", "8443": "HTTPS (8443)",
            "1935": "video streaming (RTMP)", "3478": "STUN/TURN (video calls)"}
_IP = r"\d+\.\d+\.\d+\.\d+"


def _norm(line: str) -> str:
    return " ".join(line.split())


def _norm_acl(text: str) -> str:
    """ACL entry text for comparing: no sequence number, port names as numbers."""
    words = _norm(text).split()
    if words and words[0].isdigit():
        words = words[1:]
    return " ".join(_PORT_NAMES.get(w, w) for w in words)


def parse(text: str) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = {}
    acls: Dict[str, Dict[str, Any]] = {}
    nat: List[Dict[str, Any]] = []
    routes: List[Dict[str, Any]] = []
    interfaces: List[Dict[str, Any]] = []
    globals_: set = set()
    vty = {"blocks": 0, **{k: 0 for k, _, _ in VTY_CHECKS}}
    block: Optional[Dict[str, Any]] = None
    kind = None
    section = None
    for raw in text.replace("\r", "").splitlines():
        if not raw.strip():
            continue
        indented = raw[:1] in (" ", "\t")
        line = _norm(raw)
        if indented and block is not None:
            if kind == "group":
                if line.startswith("description "):
                    block["description"] = line[len("description "):]
                elif not line.startswith("!"):
                    block["entries"].append(line)
            elif kind == "acl":
                if m := re.match(r"^!\s*-?\s*(.+)$", line):
                    section = m.group(1).strip()
                elif m := re.match(r"^(?:\d+ )?remark (.+)$", line):
                    section = m.group(1).strip()
                elif re.match(r"^(?:\d+ )?(permit|deny) ", line):
                    block["entries"].append({"text": _norm_acl(line), "section": section})
            elif kind == "interface":
                if line.startswith("description "):
                    block["description"] = line[len("description "):]
                elif m := re.match(rf"^ip address ({_IP}) ({_IP})", line):
                    block["ip"], block["mask"] = m.groups()
                elif line == "ip address dhcp":
                    block["ip"] = "dhcp"
                elif m := re.match(r"^ip nat (inside|outside)$", line):
                    block["nat"] = m.group(1)
                elif m := re.match(r"^encapsulation dot1Q (\d+)", line, re.I):
                    block["vlan"] = int(m.group(1))
                elif line == "shutdown":
                    block["shutdown"] = True
                elif m := re.match(r"^vrf forwarding (\S+)", line):
                    block["vrf"] = m.group(1)
            elif kind == "vty":
                for k, prefix, _ in VTY_CHECKS:
                    if line.startswith(prefix):
                        vty[k] += 1
            continue
        block, kind, section = None, None, None
        if line.startswith("!"):
            continue
        if m := re.match(r"^object-group network (\S+)$", line):
            block, kind = groups.setdefault(m.group(1), {"name": m.group(1), "description": None, "entries": []}), "group"
        elif m := re.match(r"^ip access-list extended (\S+)$", line):
            block, kind = acls.setdefault(m.group(1), {"name": m.group(1), "entries": []}), "acl"
        elif m := re.match(r"^interface (\S+)$", line):
            block, kind = {"name": m.group(1), "description": None, "ip": None, "mask": None, "nat": None,
                           "vlan": None, "shutdown": False, "vrf": None}, "interface"
            interfaces.append(block)
        elif line.startswith("line vty "):
            kind, block = "vty", {}
            vty["blocks"] += 1
        elif m := re.match(r"^ip nat inside source list (\S+) interface (\S+)( overload)?$", line):
            nat.append({"acl": m.group(1), "interface": m.group(2), "overload": bool(m.group(3)), "text": line})
        elif m := re.match(rf"^ip route ({_IP}) ({_IP}) ({_IP}|\S+)(?: (\d+))?", line):
            routes.append({"prefix": m.group(1), "mask": m.group(2), "next_hop": m.group(3),
                           "distance": int(m.group(4)) if m.group(4) else None, "text": line})
        elif m := re.match(r"^access-list (\d+) (.+)$", line):
            # Numbered ACL (older configs): same shape as a named one.
            acl = acls.setdefault(m.group(1), {"name": m.group(1), "entries": []})
            if rm := re.match(r"^remark (.+)$", m.group(2)):
                acl["_section"] = rm.group(1)
            else:
                acl["entries"].append({"text": _norm_acl(m.group(2)), "section": acl.get("_section")})
        else:
            globals_.add(line)
    for a in acls.values():
        a.pop("_section", None)
    hardening = [line for line, _ in HARDENING if line in globals_]
    if vty["blocks"]:
        hardening += [k for k, _, _ in VTY_CHECKS if vty[k] >= vty["blocks"]]
    return {"groups": list(groups.values()), "acls": list(acls.values()), "nat": nat, "routes": routes,
            "interfaces": interfaces, "hardening": hardening, "has_vty": bool(vty["blocks"])}


def standard_plan(wan_interface: Optional[str]) -> Dict[str, Any]:
    text = STANDARD_CONFIG
    if wan_interface:
        text += f"ip nat inside source list NMS-NAT-OUT interface {wan_interface} overload\n"
    plan = parse(text)
    plan["standard"] = True
    return plan


def acl_plain(text: str) -> str:
    """'permit tcp any object-group external-apps eq 443' -> plain English."""
    w = text.split()
    if len(w) < 4:
        return text
    action, proto, rest = w[0], w[1].upper(), w[2:]
    if rest[:1] == ["any"]:
        rest = rest[1:]
    dest, ports = "anywhere", ""
    if rest[:1] == ["host"] and len(rest) > 1:
        dest, rest = rest[1], rest[2:]
    elif rest[:1] == ["object-group"] and len(rest) > 1:
        dest, rest = f"the {rest[1]} group", rest[2:]
    elif rest[:1] == ["any"]:
        dest, rest = "anywhere", rest[1:]
    elif len(rest) > 1 and re.fullmatch(_IP, rest[0]):
        dest, rest = f"{rest[0]} (wildcard {rest[1]})", rest[2:]
    if rest[:1] == ["eq"] and len(rest) > 1:
        ports = f" port {' '.join(rest[1:])}" + (f" ({_SERVICE[rest[1]]})" if rest[1] in _SERVICE else "")
    elif rest[:1] == ["range"] and len(rest) > 2:
        ports = f" ports {rest[1]}-{rest[2]}"
    elif rest:
        ports = " " + " ".join(rest)
    verb = "Allow" if action == "permit" else "Block"
    return f"{verb} {proto} to {dest}{ports}"


def _route_label(r: Dict[str, Any], rooms: List[Dict[str, Any]], uplinks: Dict[str, str]) -> str:
    if r["prefix"] == "0.0.0.0" and r["mask"] == "0.0.0.0":
        return "Default route (customer network gateway)"
    try:
        net = ipaddress.IPv4Network(f"{r['prefix']}/{r['mask']}", strict=False)
    except ValueError:
        return ""
    who = next((room["name"] for room in rooms if room.get("room_id") and ipaddress.IPv4Address(room["room_id"]) in net), None)
    via = uplinks.get(r["next_hop"], "")
    path = "backup path" if r.get("distance") else "main path"
    return " - ".join(x for x in (f"{who} subnet" if who else None, f"{path}{f' via {via}' if via else ''}") if x)


def compare(plan: Dict[str, Any], live: Optional[Dict[str, Any]], rooms: Optional[List[Dict[str, Any]]] = None,
            uplinks: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Plan vs live router. Entry states: ok, missing (planned, not on the
    router), extra (on the router, not planned), unchecked (no live read),
    info (not compared). ``fix`` = config lines that add what's missing."""
    rooms, uplinks = rooms or [], uplinks or {}
    standard = bool(plan.get("standard"))
    checked = live is not None
    live = live or {}

    def state(in_plan: bool, in_live: bool) -> str:
        if not checked:
            return "unchecked" if in_plan else "info"
        return "ok" if in_plan and in_live else "missing" if in_plan else "extra"

    fix: List[str] = []
    counts = {"ok": 0, "missing": 0, "extra": 0, "warn": 0}

    def count(s: str) -> str:
        if s in counts:
            counts[s] += 1
        return s

    # Object groups
    live_groups = {g["name"]: g for g in live.get("groups", [])}
    plan_groups = {g["name"]: g for g in plan.get("groups", [])}
    groups = []
    for name in list(plan_groups) + [n for n in live_groups if n not in plan_groups]:
        p, l = plan_groups.get(name), live_groups.get(name)
        loose = standard and name in SITE_SPECIFIC_GROUPS
        if loose and p:
            entries = [{"text": e, "status": "info"} for e in (l or {}).get("entries", [])]
            missing = []
        else:
            pe, le = (p or {}).get("entries", []), (l or {}).get("entries", [])
            entries = [{"text": e, "status": count(state(True, e in le))} for e in pe] if p else []
            entries += [{"text": e, "status": count(state(False, True))} for e in le if e not in pe]
            missing = [e for e in pe if checked and e not in le]
        g_state = state(bool(p), bool(l))
        if g_state == "ok" and any(e["status"] == "missing" for e in entries):
            g_state = "different"
        if missing or (checked and p and not l):
            fix.append(f"object-group network {name}")
            if p and p.get("description") and not l:
                fix.append(f" description {p['description']}")
            fix += [f" {e}" for e in missing]
            fix.append("exit")
        groups.append({"name": name, "description": (p or l or {}).get("description"),
                       "status": "missing" if checked and p and not l else g_state,
                       "site_specific": loose, "entries": entries})

    # Access lists (+ which NAT policy uses them)
    live_acls = {a["name"]: a for a in live.get("acls", [])}
    plan_acls = {a["name"]: a for a in plan.get("acls", [])}
    nat_acls = {n["acl"] for n in plan.get("nat", []) + live.get("nat", [])}
    acls = []
    for name in list(plan_acls) + [n for n in live_acls if n not in plan_acls]:
        p, l = plan_acls.get(name), live_acls.get(name)
        lt = [e["text"] for e in (l or {}).get("entries", [])]
        pt = [e["text"] for e in (p or {}).get("entries", [])]
        sections: Dict[str, List[Dict[str, Any]]] = {}
        for e in (p or {}).get("entries", []):
            sections.setdefault(e["section"] or "Rules", []).append(
                {"text": e["text"], "plain": acl_plain(e["text"]), "status": count(state(True, e["text"] in lt))})
        for e in (l or {}).get("entries", []):
            if e["text"] not in pt:
                sections.setdefault("On the router, not in the plan" if p else (e["section"] or "Rules"), []).append(
                    {"text": e["text"], "plain": acl_plain(e["text"]), "status": count(state(False, True))})
        missing = [t for t in pt if checked and t not in lt]
        if missing:
            fix += [f"ip access-list extended {name}", *[f" {t}" for t in missing], "exit"]
        a_state = state(bool(p), bool(l))
        if a_state == "ok" and any(e["status"] == "missing" for s in sections.values() for e in s):
            a_state = "different"
        acls.append({"name": name, "status": "missing" if checked and p and not l else a_state,
                     "nat": name in nat_acls,
                     "sections": [{"name": k, "entries": v} for k, v in sections.items()]})

    # NAT policies (overload)
    live_nat = {n["text"] for n in live.get("nat", [])}
    plan_nat = {n["text"] for n in plan.get("nat", [])}
    nat_rows = []
    for n in plan.get("nat", []) + [n for n in live.get("nat", []) if n["text"] not in plan_nat]:
        s = count(state(n["text"] in plan_nat, n["text"] in live_nat))
        if s == "missing":
            fix.append(n["text"])
        nat_rows.append({**n, "status": s, "plain": (
            f"Rooms reach the customer network and the internet through {n['interface']}"
            f"{' (sharing its address)' if n['overload'] else ''}, only for traffic the {n['acl']} rules allow")})

    # Static routes (from the plan only; the standard template has none)
    route_key = lambda r: (r["prefix"], r["mask"], r["next_hop"], r.get("distance"))  # noqa: E731
    live_routes = {route_key(r) for r in live.get("routes", [])}
    plan_routes = {route_key(r) for r in plan.get("routes", [])}
    routes = []
    for r in plan.get("routes", []) + [r for r in live.get("routes", []) if route_key(r) not in plan_routes]:
        s = count(state(route_key(r) in plan_routes, route_key(r) in live_routes)) if not standard else ("info" if checked else "unchecked")
        if s == "missing":
            fix.append(r["text"])
        routes.append({**r, "status": s, "label": _route_label(r, rooms, uplinks)})

    # Interfaces: the planned NAT role / IP vs the live one
    live_if = {i["name"]: i for i in live.get("interfaces", [])}
    plan_if = {i["name"]: i for i in plan.get("interfaces", [])}
    ifaces = []
    names = list(plan_if) + [n for n in live_if if n not in plan_if]
    for name in names:
        p, l = plan_if.get(name), live_if.get(name)
        i = l or p
        if not (i.get("ip") or i.get("nat")) and not (p and (p.get("ip") or p.get("nat"))):
            continue  # bare parent ports / unused ports
        notes = []
        if standard or not p:
            s = "info" if checked else "unchecked"
        elif not checked:
            s = "unchecked"
        elif not l:
            if p.get("vrf"):
                continue  # management port: not every router model has one
            s, notes = "warn", ["not on the router (different port names on this router model?)"]
        else:
            if p.get("nat") != l.get("nat"):
                notes.append(f"NAT role is {l.get('nat') or 'none'}, planned {p.get('nat') or 'none'}")
            if p.get("ip") and l.get("ip") and p["ip"] != l["ip"]:
                notes.append(f"IP is {l['ip']}, planned {p['ip']}")
            if l.get("shutdown") and not p.get("shutdown"):
                notes.append("shut down on the router")
            s = "warn" if notes else "ok"
        count(s)
        role = ("Customer network (NAT outside)" if i.get("nat") == "outside" else
                f"Switch uplink{f' VLAN {i['vlan']}' if i.get('vlan') else ''} (NAT inside)" if i.get("nat") == "inside" else
                "Management port" if i.get("vrf") else "Other")
        ifaces.append({"name": name, "description": i.get("description"), "ip": i.get("ip"), "mask": i.get("mask"),
                       "nat": i.get("nat"), "vlan": i.get("vlan"), "shutdown": bool(i.get("shutdown")),
                       "role": role, "status": s, "notes": notes})

    # Hardening: only what the plan has. Some IOS versions hide defaults, so
    # a line that isn't shown is a warning, not an error.
    live_h, plan_h = set(live.get("hardening", [])), set(plan.get("hardening", []))
    labels = {**dict(HARDENING), **{k: label for k, _, label in VTY_CHECKS}}
    hardening = []
    for key, label in labels.items():
        if key not in plan_h:
            continue
        s = ("ok" if key in live_h else "warn") if checked else "unchecked"
        count(s)
        if s == "warn" and not key.startswith("vty-"):
            fix.append(key)
        hardening.append({"line": key if not key.startswith("vty-") else next(p for k, p, _ in VTY_CHECKS if k == key) + " (line vty)",
                          "label": label, "status": s})

    script = ("configure terminal\n" + "\n".join(fix) + "\nend\nwrite memory\n") if fix else ""
    return {"checked": checked, "standard": standard, "groups": groups, "acls": acls, "nat": nat_rows,
            "routes": routes, "interfaces": ifaces, "hardening": hardening, "counts": counts, "fix_script": script}
