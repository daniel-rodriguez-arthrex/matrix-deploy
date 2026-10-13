import io
import json
import zipfile

import pytest

from matrix_deploy import facility
from matrix_deploy.facility import FacilityError

from conftest import ROUTER, SWITCH1, facility_zip


def test_ports():
    assert facility._ports("1-3, 7,x,9-,4-y") == [1, 2, 3, 7, 9]


def test_parse_switch_xsf():
    sw = facility.parse_switch_xsf(SWITCH1)
    assert (sw["name"], sw["number"], sw["mgmt_ip"], sw["default_gateway"]) == ("QALAB-Switch1", 1, "192.168.1.10", "10.0.0.1")
    vlans = {v["name"]: v for v in sw["vlans"]}
    assert set(vlans) == {"Room1", "Router_Uplink1", "Backbone"}  # "Unused" has no ports
    room = vlans["Room1"]
    assert (room["kind"], room["room"], room["tag"], room["ports"], room["tagged"]) == ("room", 1, 101, [1, 2, 5], False)
    assert (room["ip"], room["prefix"], room["description"]) == ("10.1.1.1", 24, "Room 1")
    assert vlans["Router_Uplink1"]["kind"] == "uplink" and vlans["Router_Uplink1"]["tagged"]
    assert vlans["Backbone"]["kind"] == "backbone"
    assert facility.parse_switch_xsf('configure snmp sysName "core"')["number"] is None


def test_parse_router_config():
    r = facility.parse_router_config(ROUTER)
    assert (r["hostname"], r["wan_interface"], r["wan_ip"]) == ("QA1-RTR", "GigabitEthernet0/0/0", "172.16.0.5")
    assert r["inside_interfaces"] == [{"name": "GigabitEthernet0/0/1.10", "ip": "10.0.0.1"}]
    assert r["nat"] == [{"proto": "tcp", "inside_ip": "10.1.1.13", "inside_port": 22,
                         "outside": "GigabitEthernet0/0/0", "outside_port": 201}]
    assert r["policy"]["nat"][0]["acl"] == "NMS-NAT-OUT"
    assert facility.parse_router_config("hostname x\n")["wan_ip"] is None


def test_parse_nat_line():
    assert facility.parse_nat_line("ip nat inside source static udp 1.2.3.4 5 9.9.9.9 6")["outside"] == "9.9.9.9"
    assert facility.parse_nat_line("ip route 0.0.0.0 0.0.0.0 1.1.1.1") is None


def test_parse_facility_zip():
    parsed = facility.parse_facility_zip(facility_zip(), "C:/x/QA1-version7.zip")
    fac = parsed["facility"]
    assert (fac["site_code"], fac["facility_name"], fac["version"], fac["source_file"]) == ("QA1", "QA Lab", "7", "QA1-version7.zip")
    assert parsed["rooms"] == [
        {"number": 1, "room_id": "10.1.1.13", "name": "OR 1"},
        {"number": 2, "room_id": "10.1.2.13", "name": "OR 2 Secondary", "secondary": True},
    ]
    assert any("Room 3" in w for w in parsed["warnings"])
    assert parsed["secrets"] == {"nms_password": "nms-pw", "site_api_key": "api-key", "windows_password": "win-pw"}
    assert [s["name"] for s in fac["switches"]] == ["QALAB-Switch1", "QALAB-Switch2"]
    assert fac["switches"][0]["model"] == "x670G2" and fac["switches"][1]["model"] is None
    assert fac["rooms"]["1"]["extron"] == ["10.1.1.50"]
    assert fac["external_apps"] == [{"name": "PACS", "url": "https://pacs"}]
    assert facility.parse_facility_zip(facility_zip())["facility"]["source_file"] is None


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


@pytest.mark.parametrize("data, msg", [
    (b"not a zip", "isn't a .zip"),
    (_zip({"a.txt": "x"}), "No metadata.json"),
    (_zip({"metadata.json": "{bad"}), "not valid JSON"),
    (_zip({"metadata.json": json.dumps({"metadata::site_code": "../x"})}), "site_code"),
    (facility_zip(router=None), "No router config"),
    (facility_zip(rooms=False), "No rooms"),
])
def test_parse_facility_zip_errors(data, msg):
    with pytest.raises(FacilityError, match=msg):
        facility.parse_facility_zip(data)


def test_zip_size_limits(monkeypatch):
    monkeypatch.setattr(facility, "MAX_ZIP_BYTES", 10)
    with pytest.raises(FacilityError, match="too large"):
        facility.parse_facility_zip(facility_zip())
    monkeypatch.setattr(facility, "MAX_ZIP_BYTES", 10 ** 9)
    monkeypatch.setattr(facility, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(FacilityError, match="too much data"):
        facility.parse_facility_zip(facility_zip())


def test_build_profile(tmp_path):
    parsed = facility.parse_facility_zip(facility_zip())
    new = facility.build_profile(parsed, None, {"url": "u"}, qa_port_forwards=False)
    assert new["site"]["name"] == "QA Lab (QA1)" and new["artifactory"] == {"url": "u"}
    assert new["connection"] == {"ssh_username": "arthrex", "ssh_port_base": 200, "same_physical_host": False,
                                 "router_ip": "172.16.0.5", "router_wan_interface": "GigabitEthernet0/0/0",
                                 "qa_port_forwards": False}
    base = {"site": {"name": "Mine"}, "connection": {"ssh_username": "me", "router_ip": "old"},
            "rooms": [{"number": 1, "room_id": "old", "name": "x", "nms_ui_url": "https://ui", "other": 1}]}
    upd = facility.build_profile(parsed, base, {}, qa_port_forwards=True)
    assert upd["site"] == {"name": "Mine"} and upd["connection"]["ssh_username"] == "me"
    assert upd["connection"]["router_ip"] == "172.16.0.5"
    assert upd["rooms"][0] == {"number": 1, "room_id": "10.1.1.13", "name": "OR 1", "nms_ui_url": "https://ui"}
    assert facility.profile_path_for("QA1", tmp_path) == tmp_path / "qa1.json"


def test_diff_summary():
    parsed = facility.parse_facility_zip(facility_zip())
    assert facility.diff_summary(None, [], parsed) == ["Room 1 added (10.1.1.13)", "Room 2 added (10.1.2.13)"]
    old_fac = json.loads(json.dumps(parsed["facility"]))
    old_rooms = [{"number": 1, "room_id": "10.1.1.99"}, {"number": 4, "room_id": "x"}, {"number": 2, "room_id": "10.1.2.13"}]
    assert facility.diff_summary(old_fac, old_rooms, parsed) == ["Room 4 removed", "Room 1 IP 10.1.1.99 -> 10.1.1.13"]
    old_fac.update(version="6")
    old_fac["switches"][0]["vlans"][0]["ports"] = [1]
    old_fac["router"]["nat"] = []
    old_fac["router"]["policy"]["routes"] = []
    changes = facility.diff_summary(old_fac, [], parsed)
    assert changes[0] == "Version 6 -> 7"
    assert "Switch VLAN / port layout changed" in changes and "Router NAT rules in the planner changed" in changes
    assert any("hardening in the planner changed" in c for c in changes)
    old_fac["router"].pop("policy")
    assert any("Adds the planner's router groups" in c for c in facility.diff_summary(old_fac, [], parsed))
