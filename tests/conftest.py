"""Shared fixtures. Every test runs against a throwaway app folder, root
``.env`` and per-user data folder: the real ``config/`` (lab profiles and
passwords), ``.env`` and ``%APPDATA%\\MatrixFacilityManager`` are never read
or written."""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeSCP  # noqa: E402

from matrix_deploy import config, env_settings, preflight, qa_lock, router_changes, ssh_client, tls_pins  # noqa: E402
from matrix_deploy.web import server  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    appdata = tmp_path / "appdata"
    env = app / ".env"
    monkeypatch.setenv("APPDATA", str(appdata))
    for mod in (config, preflight, server):
        monkeypatch.setattr(mod, "app_dir", lambda: app)
    for mod in (env_settings, qa_lock, server):
        monkeypatch.setattr(mod, "default_env_path", lambda: env)
    monkeypatch.setattr(ssh_client, "KNOWN_HOSTS", appdata / "known_hosts")
    monkeypatch.setattr(tls_pins, "PINS_PATH", appdata / "tls_pins.json")
    monkeypatch.setattr(router_changes, "CHANGES_FILE", appdata / "router-changes.json")
    # The real work factor (600k PBKDF2 rounds) takes ~0.5 s per guess.
    monkeypatch.setattr(qa_lock, "ITERATIONS", 1000)
    monkeypatch.setattr(qa_lock._derive, "__defaults__", (1000,))
    monkeypatch.setattr(server, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(server, "DEFAULT_SWU_CACHE_DIR", tmp_path / "latest-matrix-wrynose")
    from matrix_deploy import lab_status
    lab_status._NMS_PASSWORDS.clear()
    lab_status._NMS_TOKENS.clear()
    yield SimpleNamespace(app=app, config=app / "config", env=env, appdata=appdata, tmp=tmp_path)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def fake_scp(monkeypatch):
    cls = type("SCP", (FakeSCP,), {"instances": [], "files": {}, "fail": None})
    monkeypatch.setattr(ssh_client, "SCPClient", cls)
    return cls


class Log(list):
    def __bool__(self):  # callers check "if log:" before logging
        return True

    def __call__(self, message, level):
        self.append((level, message))

    def text(self, level=None):
        return "\n".join(m for lvl, m in self if level is None or lvl == level)


@pytest.fixture
def log():
    return Log()


def profile_data(name="QA Lab 1", rooms=None, facility=None, **conn):
    rooms = rooms if rooms is not None else [
        {"number": 1, "room_id": "10.1.1.13", "name": "OR 1"},
        {"number": 2, "room_id": "10.1.2.13", "name": "OR 2"},
    ]
    data = {
        "site": {"name": name},
        "connection": {"router_ip": "172.16.0.5", "ssh_username": "arthrex", "same_physical_host": False, **conn},
        "artifactory": {"url": "https://af.example", "repo": "repo", "build_path": "builds", "branch_filter": "wrynose"},
        "rooms": rooms,
    }
    if facility is not None:
        data["facility"] = facility
    return data


def write_profile(directory: Path, stem="qa1lab", **kw) -> Path:
    path = Path(directory) / f"{stem}.json"
    path.write_text(json.dumps(profile_data(**kw)), encoding="utf-8")
    return path


@pytest.fixture
def profile(isolated):
    return write_profile(isolated.config)


@pytest.fixture
def app_config(profile):
    return config.AppConfig.load(profile)


SWITCH1 = """\
configure snmp sysName "QALAB-Switch1"
create vlan "Room1"
configure vlan Room1 tag 101
configure vlan Room1 add ports 1-2,5 untagged
configure vlan Room1 ipaddress 10.1.1.1 255.255.255.0
configure vlan Room1 description "Room 1"
create vlan "Router_Uplink1"
configure vlan Router_Uplink1 add ports 49 tagged
configure vlan Router_Uplink1 ipaddress 10.0.0.2 255.255.255.252
create vlan "Backbone"
create vlan "Unused"
configure vlan Mgmt ipaddress 192.168.1.10 255.255.255.0
configure vlan Default delete ports all
configure iproute add default 10.0.0.1
"""

SWITCH2 = """\
configure snmp sysName "QALAB-Switch2"
create vlan "Room2"
configure vlan Room2 add ports 3 untagged
configure vlan Room2 ipaddress 10.1.2.1 255.255.255.0
"""

ROUTER = """\
hostname QA1-RTR
!
interface GigabitEthernet0/0/0
 description Customer
 ip address 172.16.0.5 255.255.255.0
 ip nat outside
!
interface GigabitEthernet0/0/1.10
 encapsulation dot1Q 10
 ip address 10.0.0.1 255.255.255.252
 ip nat inside
!
ip nat inside source static tcp 10.1.1.13 22 interface GigabitEthernet0/0/0 201
ip nat inside source list NMS-NAT-OUT interface GigabitEthernet0/0/0 overload
ip route 0.0.0.0 0.0.0.0 172.16.0.1
ip route 10.1.1.0 255.255.255.0 10.0.0.2
no cdp run
"""


def facility_zip(site="QA1", rooms=True, router=ROUTER, extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("metadata.json", json.dumps({"metadata::site_code": site, "metadata::FacilityName": "QA Lab",
                                                "metadata::VersionNumber": 7}))
        z.writestr("switches/QALAB-Switch1.xsf", SWITCH1)
        z.writestr("switches/QALAB-Switch2.xsf", SWITCH2)
        if router is not None:
            z.writestr(f"{site}-router-version7.txt", router)
        if rooms:
            z.writestr("puppet/Room1/matrix_configuration.json",
                       json.dumps({"switch_number": 1, "vlan_number": 1, "switch_model": "x670G2"}))
            z.writestr("puppet/Room1/common.json", json.dumps({
                "mg2puppet::api_service::cluster": ["10.9.9.9", "10.1.1.13"],
                "mg2puppet::api_service::extrondevices": ["10.1.1.50"],
                "mg2puppet::api_service::nmspassword": "nms-pw",
                "mg2puppet::api_service::apikey": "api-key",
            }))
            z.writestr("puppet/Room2/matrix_configuration.json",
                       json.dumps({"switch_number": 2, "vlan_number": 2, "secondary_pc": True}))
            z.writestr("puppet/Room2/common.json", json.dumps({"mg2puppet::api_service::cluster": ["10.1.2.13"]}))
            # No VLAN for room 3: skipped with a warning.
            z.writestr("puppet/Room3/matrix_configuration.json", json.dumps({"vlan_number": 3}))
        z.writestr("credentials/site.json", json.dumps({"WindowsPassword": "win-pw"}))
        z.writestr("externalapps.json", json.dumps([{"name": "PACS", "url": "https://pacs"}, {"name": "x"}, "junk"]))
        for name, text in (extra or {}).items():
            z.writestr(name, text)
    return buf.getvalue()
