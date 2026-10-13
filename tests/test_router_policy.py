from matrix_deploy import router_policy as rp

PLAN = """\
hostname R
no cdp run
ip ssh version 2
object-group network external-apps
 description Ext
 host 1.1.1.1
 host 2.2.2.2
 !
object-group network only-plan
 description P
 host 3.3.3.3
ip access-list extended NMS-NAT-OUT
 ! - Bomgar
 10 permit tcp any host 205.167.79.90 eq 443
 remark Datadog
 permit tcp any object-group external-apps eq www
 deny ip any any
access-list 10 remark Mgmt
access-list 10 permit 10.0.0.0 0.0.0.255
interface Gi0/0/0
 description Customer
 ip address 172.16.0.5 255.255.255.0
 ip nat outside
interface Gi0/0/1.10
 encapsulation dot1Q 10
 ip address 10.0.0.1 255.255.255.252
 ip nat inside
interface Gi0/0/2
 ip address dhcp
 shutdown
interface Gi0
 vrf forwarding Mgmt-intf
 ip address 192.168.0.1 255.255.255.0
interface Gi0/0/3
 no ip address
ip nat inside source list NMS-NAT-OUT interface Gi0/0/0 overload
ip route 0.0.0.0 0.0.0.0 172.16.0.1
ip route 10.1.1.0 255.255.255.0 10.0.0.2
ip route 10.1.1.0 255.255.255.0 10.0.0.6 250
line vty 0 4
 exec-timeout 30 0
 transport input ssh
"""

LIVE = """\
hostname R\r
no cdp run
object-group network external-apps
 host 1.1.1.1
object-group network live-only
 host 9.9.9.9
ip access-list extended NMS-NAT-OUT
 10 permit tcp any host 205.167.79.90 eq 443
 20 permit tcp any any eq 22
ip access-list extended LIVE-ACL
 permit ip any any
interface Gi0/0/0
 ip address 172.16.0.9 255.255.255.0
 shutdown
interface Gi0/0/1.10
 ip address 10.0.0.1 255.255.255.252
 ip nat inside
interface Gi0/0/9
 ip address 5.5.5.5 255.255.255.0
 ip nat inside
ip route 0.0.0.0 0.0.0.0 172.16.0.1
ip route 9.9.9.0 255.255.255.0 10.0.0.2
line vty 0 4
 transport input ssh
line vty 5 15
 transport input ssh
"""


def by_name(items, name, key="name"):
    return next(i for i in items if i[key] == name)


def test_parse():
    p = rp.parse(PLAN)
    assert [g["name"] for g in p["groups"]] == ["external-apps", "only-plan"]
    assert by_name(p["groups"], "external-apps")["entries"] == ["host 1.1.1.1", "host 2.2.2.2"]
    acl = by_name(p["acls"], "NMS-NAT-OUT")
    assert [(e["text"], e["section"]) for e in acl["entries"]] == [
        ("permit tcp any host 205.167.79.90 eq 443", "Bomgar"),
        ("permit tcp any object-group external-apps eq 80", "Datadog"),
        ("deny ip any any", "Datadog"),
    ]
    assert by_name(p["acls"], "10")["entries"] == [{"text": "permit 10.0.0.0 0.0.0.255", "section": "Mgmt"}]
    assert p["nat"] == [{"acl": "NMS-NAT-OUT", "interface": "Gi0/0/0", "overload": True,
                         "text": "ip nat inside source list NMS-NAT-OUT interface Gi0/0/0 overload"}]
    assert [r["distance"] for r in p["routes"]] == [None, None, 250]
    gi2 = by_name(p["interfaces"], "Gi0/0/2")
    assert gi2["ip"] == "dhcp" and gi2["shutdown"]
    assert by_name(p["interfaces"], "Gi0")["vrf"] == "Mgmt-intf"
    assert by_name(p["interfaces"], "Gi0/0/1.10")["vlan"] == 10
    assert p["hardening"] == ["no cdp run", "ip ssh version 2", "vty-ssh", "vty-timeout"] and p["has_vty"]
    assert not rp.parse("hostname x")["has_vty"]


def test_standard_plan_unchecked():
    plan = rp.standard_plan("Gi0/0/0")
    assert plan["standard"] and plan["nat"][0]["interface"] == "Gi0/0/0"
    assert rp.standard_plan(None)["nat"] == []
    res = rp.compare(plan, None)
    assert not res["checked"] and res["standard"] and res["fix_script"] == ""
    assert by_name(res["groups"], "external-apps")["site_specific"]
    assert {h["status"] for h in res["hardening"]} == {"unchecked"}
    assert res["counts"] == {"ok": 0, "missing": 0, "extra": 0, "warn": 0}


def test_standard_plan_vs_live():
    plan = rp.standard_plan("Gi0/0/0")
    res = rp.compare(plan, rp.parse(LIVE))
    ext = by_name(res["groups"], "external-apps")
    # Site-specific hosts are listed, not judged.
    assert ext["entries"] == [{"text": "host 1.1.1.1", "status": "info"}] and ext["status"] == "ok"
    assert by_name(res["groups"], "act-ddproxy-dev")["status"] == "missing"
    assert all(r["status"] == "info" for r in res["routes"])
    assert all(i["status"] == "info" for i in res["interfaces"])
    assert "object-group network act-ddproxy-dev" in res["fix_script"]


def test_compare_plan_vs_live():
    rooms = [{"name": "OR 1", "room_id": "10.1.1.13"}, {"name": "OR 9"}]
    res = rp.compare(rp.parse(PLAN), rp.parse(LIVE), rooms=rooms, uplinks={"10.0.0.2": "Switch1 uplink"})
    assert res["checked"] and not res["standard"]
    ext = by_name(res["groups"], "external-apps")
    assert ext["status"] == "different"
    assert [e["status"] for e in ext["entries"]] == ["ok", "missing"]
    assert by_name(res["groups"], "only-plan")["status"] == "missing"
    assert by_name(res["groups"], "live-only")["status"] == "extra"

    acl = by_name(res["acls"], "NMS-NAT-OUT")
    assert acl["status"] == "different" and acl["nat"]
    sections = {s["name"]: s["entries"] for s in acl["sections"]}
    assert sections["Bomgar"][0]["status"] == "ok"
    assert [e["status"] for e in sections["Datadog"]] == ["missing", "missing"]
    assert sections["On the router, not in the plan"][0]["plain"] == "Allow TCP to anywhere port 22"
    assert by_name(res["acls"], "10")["status"] == "missing"
    live_acl = by_name(res["acls"], "LIVE-ACL")
    assert live_acl["status"] == "extra" and live_acl["sections"][0]["name"] == "Rules"

    assert res["nat"][0]["status"] == "missing" and "(sharing its address)" in res["nat"][0]["plain"]
    routes = {(r["prefix"], r["next_hop"]): r for r in res["routes"]}
    assert routes[("0.0.0.0", "172.16.0.1")]["status"] == "ok"
    assert routes[("10.1.1.0", "10.0.0.2")]["label"] == "OR 1 subnet - main path via Switch1 uplink"
    assert routes[("10.1.1.0", "10.0.0.6")]["label"] == "OR 1 subnet - backup path"
    assert routes[("9.9.9.0", "10.0.0.2")]["status"] == "extra"

    ifaces = {i["name"]: i for i in res["interfaces"]}
    wan = ifaces["Gi0/0/0"]
    # The role is what the live router has (no NAT role here).
    assert wan["status"] == "warn" and wan["role"] == "Other"
    assert wan["notes"] == ["NAT role is none, planned outside", "IP is 172.16.0.9, planned 172.16.0.5",
                            "shut down on the router"]
    assert ifaces["Gi0/0/1.10"]["status"] == "ok" and ifaces["Gi0/0/1.10"]["role"] == "Switch uplink (NAT inside)"
    assert ifaces["Gi0/0/2"]["status"] == "warn" and "not on the router" in ifaces["Gi0/0/2"]["notes"][0]
    assert "Gi0" not in ifaces and "Gi0/0/3" not in ifaces
    assert ifaces["Gi0/0/9"]["status"] == "info"

    hard = {h["line"]: h["status"] for h in res["hardening"]}
    assert hard == {"no cdp run": "ok", "ip ssh version 2": "warn", "transport input ssh (line vty)": "ok",
                    "exec-timeout (line vty)": "warn"}
    script = res["fix_script"]
    assert script.startswith("configure terminal\n") and script.endswith("end\nwrite memory\n")
    for line in ("object-group network only-plan", " description P", " host 2.2.2.2", "ip access-list extended 10",
                 " deny ip any any", "ip nat inside source list NMS-NAT-OUT interface Gi0/0/0 overload",
                 "ip route 10.1.1.0 255.255.255.0 10.0.0.2", "ip ssh version 2"):
        assert line in script.splitlines()
    assert "exec-timeout" not in script
    assert res["counts"]["missing"] > 0 and res["counts"]["extra"] > 0 and res["counts"]["warn"] == 4


def test_compare_unchecked_plan_interfaces():
    res = rp.compare(rp.parse(PLAN), None)
    assert {i["status"] for i in res["interfaces"]} == {"unchecked"}
    assert {r["status"] for r in res["routes"]} == {"unchecked"}
    assert by_name(res["groups"], "only-plan")["status"] == "unchecked"


def test_acl_plain():
    assert rp.acl_plain("permit tcp") == "permit tcp"
    assert rp.acl_plain("permit tcp any host 1.2.3.4 eq 443") == "Allow TCP to 1.2.3.4 port 443 (HTTPS)"
    assert rp.acl_plain("permit tcp any object-group g eq 8081") == "Allow TCP to the g group port 8081"
    assert rp.acl_plain("deny ip any any") == "Block IP to anywhere"
    assert rp.acl_plain("permit udp any 10.0.0.0 0.0.0.255 range 1 9") == "Allow UDP to 10.0.0.0 (wildcard 0.0.0.255) ports 1-9"
    assert rp.acl_plain("permit tcp any any established") == "Allow TCP to anywhere established"


def test_route_label_edges():
    assert rp._route_label({"prefix": "0.0.0.0", "mask": "0.0.0.0"}, [], {}) == "Default route (customer network gateway)"
    assert rp._route_label({"prefix": "10.0.0.0", "mask": "255.0.255.0", "next_hop": "x"}, [], {}) == ""
    assert rp._route_label({"prefix": "10.5.0.0", "mask": "255.255.0.0", "next_hop": "x"}, [], {}) == "main path"
