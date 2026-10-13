"""The FastAPI app: endpoints, background jobs (read over the WebSocket),
the local-only guard and sign-in. Rooms, router and switches are faked."""

import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

from matrix_deploy import facility, qa_lock, router_changes
from matrix_deploy.artifactory import ArtifactoryError
from matrix_deploy.config import AppConfig, Room
from matrix_deploy.deployer import Deployer
from matrix_deploy.env_settings import load_merged_env, parse_env_file, profile_env_path, save_env_values
from matrix_deploy.jenkins import BuildResult, JenkinsError
from matrix_deploy.ssh_client import SSHError
from matrix_deploy.web import server
from matrix_deploy.web.tunnel import Tunnel

from conftest import facility_zip, write_profile
from fakes import FakeChannel, FakeSSHClient

BASE = "http://127.0.0.1:8420"


@pytest.fixture
def fac():
    return facility.parse_facility_zip(facility_zip())["facility"]


@pytest.fixture
def lab(isolated, fac):
    """A planner-imported lab (2 rooms, 2 switches) with its passwords saved."""
    path = write_profile(isolated.config, facility=fac, router_wan_interface="Gi0/0/0")
    save_env_values(profile_env_path(path), {"ssh_password": "pw", "sudo_password": "sudo", "router_admin_password": "rpw"})
    return path


class FakeTunnels:
    instances = []

    def __init__(self):
        self.opened, self.closed, self.urls = [], [], {}
        FakeTunnels.instances.append(self)

    def open_port(self, room_number, port, label, client):
        if room_number == 99:
            raise OSError("no transport")
        t = Tunnel(id=f"t{len(self.opened)}", room_number=room_number, target_key="devtools", label=label,
                   local_port=5555, url="http://127.0.0.1:5555/tok", base="http://127.0.0.1:5555/tok")
        self.opened.append(t)
        return t

    def set_url(self, tid, url):
        self.urls[tid] = url

    def close(self, tid):
        self.closed.append(tid)
        return True

    def list(self):
        return self.opened

    def close_all(self):
        self.closed.append("all")


def make_client(monkeypatch, config_path=None, config=None, token=None):
    monkeypatch.setattr(server, "cleanup_temp_files", lambda: None)
    monkeypatch.setattr(server, "TunnelManager", FakeTunnels)
    FakeTunnels.instances.clear()
    cfg = config or AppConfig.load(config_path)
    return TestClient(server.create_app(cfg, api_token=token), base_url=BASE)


@pytest.fixture
def client(monkeypatch, lab):
    with make_client(monkeypatch, lab) as c:
        yield c


def events(client, job_id, timeout=10):
    out = []
    # The test client sends "Host: testserver" on WebSockets unless told otherwise.
    with client.websocket_connect(f"/ws/jobs/{job_id}", headers={"host": "127.0.0.1:8420"}) as ws:
        while True:
            ev = ws.receive_json()
            out.append(ev)
            if ev["type"] == "all_done":
                return out


def logs(evs, level=None):
    return "\n".join(e["message"] for e in evs if e["type"] == "log" and (level is None or e["level"] == level))


def run_job(client, url, body):
    r = client.post(url, json=body)
    assert r.status_code == 200, r.text
    return events(client, r.json()["job_id"])


# -- basics ----------------------------------------------------------------------

def test_rooms_connection_health(client):
    rooms = client.get("/api/rooms").json()
    assert rooms[0]["web_app_url"] == "https://172.16.0.5:10001/app/" and rooms[1]["secondary"] is False
    assert client.get("/api/connection").json()["ssh_username"] == "arthrex"
    assert client.get("/api/health").json() == {"app": "matrix-deploy"}
    assert client.post("/api/presence?id=a").json() == {"active_jobs": 0}
    assert client.post("/api/presence/bye?id=a").json() == {"ok": True}
    assert client.get("/api/preflight?network=false").json()["ok"] in (True, False)


def test_security_headers_and_pages(client):
    r = client.get("/api/rooms")
    assert r.headers["Cache-Control"] == "no-store" and "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    page = client.get("/")
    assert page.status_code == 200 and page.headers["Cache-Control"] == "no-cache"
    assert client.get("/favicon.ico").headers["content-type"] == "image/x-icon"
    assert client.get("/static/app.js").status_code == 200


def test_rooms_without_qa_forwards(monkeypatch, isolated):
    path = write_profile(isolated.config, qa_port_forwards=False)
    with make_client(monkeypatch, path) as c:
        assert c.get("/api/rooms").json()[0]["web_app_url"] is None
        plan = c.get("/api/router-ports").json()
        assert set(plan["rooms"][0]["forwards"]) == {"ssh", "nms"} and plan["facility"] is None
        assert plan["policy"]["source"] == {"kind": "standard", "facility": False}


# -- guard / sign-in ---------------------------------------------------------------

def test_local_only_guard(client):
    assert client.get("/api/rooms", headers={"host": "evil.example"}).status_code == 403
    r = client.post("/api/qa/lock", headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and "cross-origin" in r.text
    assert client.post("/api/qa/lock", headers={"origin": "http://127.0.0.1:8420"}).status_code == 200
    assert client.get("/api/rooms", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get("/api/health", headers={"sec-fetch-site": "cross-site"}).status_code == 200
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/jobs/x", headers={"host": "127.0.0.1:8420", "origin": "http://evil.example"}):
            pass
    assert exc.value.code == 1008


def test_sign_in_token(monkeypatch, lab):
    with make_client(monkeypatch, lab, token="T0KEN") as c:
        assert c.get("/api/rooms").status_code == 403
        assert c.get("/api/health").status_code == 200
        assert c.get("/").status_code == 401 and c.get("/?t=wrong").status_code == 401
        page = c.get("/?t=T0KEN")
        assert page.status_code == 200 and 'http-equiv="refresh"' in page.text and "/static/app-icon" in page.text
        assert c.cookies.get("md_session_8420") == "T0KEN"
        assert c.get("/api/rooms").status_code == 200 and c.get("/").status_code == 200
        c.cookies.set("md_session_8420", "nope")
        assert c.get("/api/rooms").status_code == 403
        assert c.get("/api/rooms", headers={"cookie": "bad\x00cookie"}).status_code == 403
    assert server.session_cookie_name("localhost") == "md_session_80"


# -- setup / credentials / team / QA lock ---------------------------------------------

def test_setup_and_save(client, lab, isolated):
    s = client.get("/api/setup").json()
    assert s["saved"]["ssh_password"] and s["saved"]["router_admin_password"] and not s["saved"]["switch_password"]
    assert s["facility"]["switch_count"] == 2 and s["qa"] == {"lock": False, "unlocked": True}
    assert "pw" not in json.dumps(s["prefill"])
    r = client.post("/api/setup/save", json={"sudo_password": "new", "artifactory_email": "me@x", "jenkins_token": "jt",
                                             "switch_password": "sw"})
    assert r.status_code == 200
    env, secrets = load_merged_env(str(lab))
    assert secrets["sudo_password"] == "new" and secrets["ssh_password"] == "pw" and env["artifactory_email"] == "me@x"
    assert parse_env_file(isolated.env).get("JENKINS_TOKEN") == "jt" or secrets["jenkins_token"] == "jt"
    assert client.get("/api/setup").json()["saved"]["switch_password"]
    assert client.post("/api/setup/save", json={"ssh_password": "a\nb"}).status_code == 400


def test_save_paths(client, isolated, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    swu = tmp_path / "x.swu"
    swu.write_text("x")
    (tmp_path / "afile").write_text("x")
    r = client.post("/api/setup/save-paths", json={
        "swu_file": f'"{swu}"', "backend_repo": str(repo), "web_repo": str(tmp_path / "nope"),
        "swu_download_dir": str(tmp_path / "afile"), "webapp_dist": "",
    }).json()
    assert any("no package.json" in w for w in r["warnings"]) and any("Folder not found" in w for w in r["warnings"])
    assert any("is a file" in w for w in r["warnings"])
    assert client.get("/api/setup").json()["prefill"]["swu_file"] == str(swu)
    r = client.post("/api/setup/save-paths", json={"swu_file": str(tmp_path / "gone.swu")}).json()
    assert r["warnings"] == [f"SWU file not found: {tmp_path / 'gone.swu'}"]
    assert client.post("/api/setup/save-paths", json={"swu_file": "a\nb"}).status_code == 400


def test_team_and_qa_lock(client, isolated, monkeypatch):
    monkeypatch.setattr(qa_lock.time, "sleep", lambda s: None)
    assert client.post("/api/setup/team", json={"team": "integration"}).json()["team"] == "integration"
    assert client.post("/api/qa/password", json={"password": "short"}).status_code == 400
    r = client.post("/api/qa/password", json={"password": "a long qa password"})
    assert r.json()["qa"] == {"lock": True, "unlocked": True}
    # Choosing Integration locks QA again on this computer.
    assert client.post("/api/setup/team", json={"team": "integration"}).json()["qa"] == {"lock": True, "unlocked": False}
    assert client.post("/api/setup/team", json={"team": "qa"}).status_code == 403
    assert client.post("/api/jobs/deploy", json={"room_numbers": [1], "swu_file": "x"}).status_code == 403
    assert client.post("/api/qa/unlock", json={"password": "wrong"}).status_code == 403
    assert client.post("/api/qa/unlock", json={"password": "a long qa password", "remember": True}).json()["qa"]["unlocked"]
    assert qa_lock.remembered()
    assert client.post("/api/setup/team", json={"team": "qa"}).status_code == 200
    assert not client.post("/api/qa/lock").json()["qa"]["unlocked"]
    assert client.post("/api/qa/remove").status_code == 403
    client.post("/api/qa/unlock", json={"password": "a long qa password"})
    assert client.post("/api/qa/remove").json()["qa"] == {"lock": False, "unlocked": True}


def test_qa_lock_errors(client, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(qa_lock, "save_token", boom)
    assert client.post("/api/qa/lock").status_code == 500
    assert client.post("/api/qa/remove").status_code == 500
    real_set, real_remove = qa_lock.set_password, qa_lock.remove_lock
    monkeypatch.setattr(qa_lock, "set_password", lambda p: (_ for _ in ()).throw(ValueError("built in")))
    assert client.post("/api/qa/password", json={"password": "a long qa password"}).status_code == 400
    monkeypatch.setattr(qa_lock, "remove_lock", lambda: (_ for _ in ()).throw(ValueError("built in")))
    assert client.post("/api/qa/remove").status_code == 400
    monkeypatch.setattr(qa_lock, "set_password", real_set)
    assert client.post("/api/qa/password", json={"password": "a long qa password"}).status_code == 500
    monkeypatch.setattr(qa_lock, "is_configured", lambda: True)
    monkeypatch.setattr(qa_lock, "check_password", lambda p: True)
    monkeypatch.setattr(qa_lock, "remember_token", lambda p: "tok")
    assert client.post("/api/qa/unlock", json={"password": "x", "remember": True}).status_code == 500
    monkeypatch.setattr(server, "save_env_values", boom)
    assert client.post("/api/setup/team", json={"team": "integration"}).status_code == 500
    assert client.post("/api/setup/save", json={"ssh_password": "x"}).status_code == 500
    assert client.post("/api/setup/save-paths", json={"swu_file": ""}).status_code == 500


# -- profiles / files / artifacts ------------------------------------------------------

def test_profiles(client, isolated, lab):
    other = write_profile(isolated.config, "qa2lab", name="QA Lab 2")
    p = client.get("/api/profiles").json()
    assert p["active"] == str(lab) and {x["name"] for x in p["profiles"]} == {"QA Lab 1", "QA Lab 2"}
    assert client.post("/api/profiles/select", json={"path": "C:/elsewhere.json"}).status_code == 400
    assert client.post("/api/profiles/select", json={"path": str(other)}).json()["active_name"] == "QA Lab 2"
    assert client.get("/api/profiles").json()["active"] == str(other)
    data = json.loads(other.read_text())
    data["rooms"] = [{"number": 1, "bogus": True}]
    other.write_text(json.dumps(data))
    assert client.post("/api/profiles/select", json={"path": str(other)}).status_code == 502


def test_fs_list(client, tmp_path, monkeypatch):
    d = tmp_path / "builds"
    (d / "sub").mkdir(parents=True)
    (d / "b.swu").write_bytes(b"12")
    (d / "notes.txt").write_text("x")
    r = client.get("/api/fs/list", params={"path": str(d)}).json()
    assert [(e["name"], e["is_dir"]) for e in r["entries"]] == [("sub", True), ("b.swu", False)]
    assert r["entries"][1]["size"] == 2 and r["parent"] == str(d.parent)
    assert client.get("/api/fs/list", params={"path": str(d / "b.swu")}).json()["dir"] == str(d)
    assert client.get("/api/fs/list", params={"path": str(tmp_path / "gone")}).json()["dir"] == str(Path.home())
    assert client.get("/api/fs/list").json()["dir"] == str(Path.home())  # default folder doesn't exist
    monkeypatch.setattr(Path, "iterdir", lambda self: (_ for _ in ()).throw(PermissionError("denied")))
    assert client.get("/api/fs/list", params={"path": str(d)}).status_code == 400


def test_artifacts(client, tmp_path):
    server.ARTIFACTS_DIR.mkdir(parents=True)
    (server.ARTIFACTS_DIR / "or1-logs.txt").write_text("log")
    assert client.get("/api/artifacts/or1-logs.txt").text == "log"
    assert client.get("/api/artifacts/missing.txt").status_code == 404
    assert client.get("/api/artifacts/..%2F..%2Fsecret").status_code == 404


def test_health_check_script(client, isolated):
    assert client.get("/api/tools/health-check.ps1").status_code == 404
    p = isolated.app / "tools" / "health-check" / "MatrixHealthCheck.ps1"
    p.parent.mkdir(parents=True)
    p.write_text("Write-Host hi")
    r = client.get("/api/tools/health-check.ps1")
    assert r.status_code == 200 and r.text == "Write-Host hi"


def test_cleanup_temp_files(monkeypatch, tmp_path):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    old, new = arts / "old.zip", arts / "new.zip"
    old.write_text("x")
    new.write_text("x")
    import os
    os.utime(old, (0, 0))
    (arts / "dir").mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    legacy, keep = temp / "or3-edited.json", temp / "or3-other.json"
    legacy.write_text("{}")
    keep.write_text("{}")
    monkeypatch.setattr(server, "ARTIFACTS_DIR", arts)
    monkeypatch.setattr(server, "tempfile", SimpleNamespace(gettempdir=lambda: str(temp)))
    server.cleanup_temp_files()
    assert not old.exists() and new.exists() and not legacy.exists() and keep.exists()
    monkeypatch.setattr(server, "ARTIFACTS_DIR", tmp_path / "none")
    server.cleanup_temp_files()


# -- room reads ------------------------------------------------------------------------

def test_room_reads(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(server, "collect_room_status", lambda cfg, creds, room: seen.update(status=creds) or {"n": room.number})
    monkeypatch.setattr(server, "collect_room_sfp", lambda cfg, creds, room: {"sfp": room.number})
    assert client.post("/api/lab-status/room", json={"room_number": 1}).json() == {"n": 1}
    assert seen["status"].ssh_password == "pw" and seen["status"].sudo_password == "sudo"  # saved ones
    assert client.post("/api/fiber/room", json={"room_number": 2, "ssh_password": "typed"}).json() == {"sfp": 2}
    assert client.post("/api/lab-status/room", json={"room_number": 9}).status_code == 400
    monkeypatch.setattr(Deployer, "fetch_nms_password", lambda self, room: "nmspw")
    assert client.post("/api/room/nms-password", json={"room_number": 1}).json() == {"room_number": 1, "password": "nmspw"}
    monkeypatch.setattr(Deployer, "fetch_nms_password", lambda self, room: None)
    assert client.post("/api/room/nms-password", json={"room_number": 1}).status_code == 502
    monkeypatch.setattr(Deployer, "read_matrix_config_text", lambda self, room: '{"a": 1}')
    assert client.post("/api/config/load", json={"room_number": 1}).json()["content"] == '{"a": 1}'
    monkeypatch.setattr(Deployer, "read_matrix_config_text", lambda self, room: None)
    assert client.post("/api/config/load", json={"room_number": 1}).status_code == 502


def test_health_check_endpoint(client, monkeypatch, isolated, lab):
    calls = []
    monkeypatch.setattr(server, "collect_device_health", lambda t, label, services, port: calls.append((t, label)) or {"ok": 1})
    assert client.post("/api/health-check", json={"room_number": 1}).json() == {"ok": 1}
    assert calls[-1][0].port == 201 and calls[-1][1] == "OR 1"
    bad = [({"host": "bad host;rm"}, "IP address"), ({"host": "10.0.0.9"}, "username"),
           ({"host": "10.0.0.9", "username": "-x"}, "username"), ({"host": "10.0.0.9", "username": "u"}, "SSH password")]
    for body, msg in bad:
        r = client.post("/api/health-check", json=body)
        assert r.status_code == 400 and msg in r.json()["detail"]
    client.post("/api/health-check", json={"host": "10.0.0.9", "username": "u", "ssh_password": "p", "port": 2222})
    t, label = calls[-1]
    assert (t.host, t.port, t.sudo_password, label) == ("10.0.0.9", 2222, "p", "u@10.0.0.9:2222")
    profile_env_path(lab).write_text("")
    r = client.post("/api/health-check", json={"room_number": 1})
    assert r.status_code == 400 and "SSH password" in r.json()["detail"]


# -- jobs ---------------------------------------------------------------------------------

def test_deploy_job(client, monkeypatch):
    assert client.post("/api/jobs/deploy", json={"room_numbers": [1]}).status_code == 400
    assert client.post("/api/jobs/deploy", json={"room_numbers": [], "swu_file": "x"}).status_code == 400
    assert client.post("/api/jobs/deploy", json={"room_numbers": [7], "swu_file": "x"}).status_code == 400

    def deploy(self, req):
        if req.room.number == 2:
            raise RuntimeError("kaboom")
        self.log("deployed", "success")
        self.milestone(1, 1)
        self.progress(5, 10)
        return True

    monkeypatch.setattr(Deployer, "deploy", deploy)
    evs = run_job(client, "/api/jobs/deploy", {"room_numbers": [1, 2], "swu_file": "x.swu", "sequential": False,
                                              "max_concurrency": 1})
    done = {e["room"]: e["ok"] for e in evs if e["type"] == "room_done"}
    assert done == {1: True, 2: False} and "Unexpected error on OR 2: kaboom" in logs(evs, "error")
    assert any(e["type"] == "milestone" for e in evs) and any(e["type"] == "progress" for e in evs)


def test_system_action_jobs(client, monkeypatch, tmp_path):
    assert client.post("/api/jobs/system-action", json={"room_numbers": [1], "action": "nope"}).status_code == 400
    monkeypatch.setattr(Deployer, "check_uptime", lambda self, room: self.log(f"up {room.number}", "detail") or True)
    evs = run_job(client, "/api/jobs/system-action", {"room_numbers": [1, 2], "action": "check_uptime", "sequential": False})
    assert "up 1" in logs(evs) and "up 2" in logs(evs)

    def logs_fetch(self, room, dest, since_hours=None):
        p = Path(dest) / f"or{room.number}-logs.txt"
        p.write_text("x")
        return p

    monkeypatch.setattr(Deployer, "fetch_service_logs", logs_fetch)
    evs = run_job(client, "/api/jobs/system-action", {"room_numbers": [1], "action": "get_logs", "since_hours": 2})
    art = next(e for e in evs if e["type"] == "artifact")
    assert art["url"] == "/api/artifacts/or1-logs.txt" and client.get(art["url"]).text == "x"
    monkeypatch.setattr(Deployer, "reboot", lambda self, room: (_ for _ in ()).throw(RuntimeError("dead")))
    evs = run_job(client, "/api/jobs/system-action", {"room_numbers": [1], "action": "reboot"})
    assert "Unexpected error on OR 1: dead" in logs(evs)


def test_cancel_and_presence_counts(client, monkeypatch):
    started = []

    def slow(self, room):
        started.append(1)
        while not self.is_cancelled():
            time.sleep(0.01)
        return False

    monkeypatch.setattr(Deployer, "check_specs", slow)
    job_id = client.post("/api/jobs/system-action", json={"room_numbers": [1, 2], "action": "check_specs"}).json()["job_id"]
    while not started:
        time.sleep(0.01)
    assert client.post("/api/presence?id=w").json()["active_jobs"] == 1
    assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelled": True}
    evs = events(client, job_id)
    assert {"type": "room_status", "room": 2, "status": "cancelled"} in evs
    assert client.post("/api/jobs/unknown/cancel").status_code == 404


def test_dispatch_action_covers_every_action(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARTIFACTS_DIR", tmp_path)
    room = Room(number=1, room_id="x", name="OR 1")
    job = server.Job("j", [room])
    d = MagicMock()
    for action in sorted(server.SIMPLE_ACTIONS - {"add_trusted_endpoint"}):
        assert server._dispatch_action(d, room, action, {"custom_command": "ls", "since_hours": None}, job)
    assert not server._dispatch_action(d, room, "add_trusted_endpoint", {"trusted_endpoint": " "}, job)
    assert server._dispatch_action(d, room, "add_trusted_endpoint", {"trusted_endpoint": "https://x"}, job)
    d.add_trusted_endpoint.assert_called_with(room, "https://x")
    d.set_admin_app_debugging.assert_any_call(room, True)
    d.deploy_golden_nms_config.assert_called_with(room, "MAX")
    d.check_system_errors.assert_called_with(room, 3)
    for action, method in (("get_logs", "fetch_service_logs"), ("get_full_journal", "fetch_full_journal"),
                           ("export_bundle", "export_support_bundle")):
        getattr(d, method).return_value = tmp_path / f"{action}.txt"
        assert server._dispatch_action(d, room, action, {}, job)
        getattr(d, method).return_value = None
        assert not server._dispatch_action(d, room, action, {}, job)
    assert not server._dispatch_action(d, room, "made_up", {}, job)


def test_config_jobs(client, monkeypatch):
    monkeypatch.setattr(Deployer, "deploy_matrix_config_text", lambda self, room, content: content == "{}")
    evs = run_job(client, "/api/config/deploy", {"room_number": 1, "content": "{}"})
    assert {"type": "room_done", "room": 1, "ok": True} in evs
    monkeypatch.setattr(Deployer, "deploy_matrix_config_text", lambda self, room, content: 1 / 0)
    assert "Unexpected error" in logs(run_job(client, "/api/config/deploy", {"room_number": 1, "content": "{}"}))
    got = []
    monkeypatch.setattr(Deployer, "patch_matrix_config", lambda self, room, ch: got.append(ch) or True)
    run_job(client, "/api/config/apply-changes", {"room_numbers": [1, 2], "sequential": False,
                                                  "changes": [{"op": "set", "path": ["a", 0], "value": 1}]})
    assert got[0] == [{"op": "set", "path": ["a", 0], "value": 1}] and len(got) == 2
    monkeypatch.setattr(Deployer, "patch_matrix_config", lambda self, room, ch: 1 / 0)
    assert "Unexpected error" in logs(run_job(client, "/api/config/apply-changes", {
        "room_numbers": [1], "changes": [{"op": "delete", "path": ["a"]}]}))
    assert client.post("/api/config/apply-changes", json={"room_numbers": [1], "changes": []}).status_code == 422


def test_webapp_deploy_job(client, monkeypatch, tmp_path):
    dist, web = tmp_path / "api" / "dist", tmp_path / "web" / "dist"
    (web / "arthrex-synergy-matrix").mkdir(parents=True)
    (web / "arthrex-synergy-matrix" / "index.html").write_text("")
    dist.mkdir(parents=True)
    body = {"room_numbers": [1, 2], "local_dist": str(dist), "local_web": str(web)}
    evs = run_job(client, "/api/jobs/webapp-deploy", {**body, "build_only": True})
    assert "has no server.js" in logs(evs, "error") and "using" in logs(evs, "warning")
    (dist / "server.js").write_text("")
    evs = run_job(client, "/api/jobs/webapp-deploy", {**body, "build_only": True})
    assert "Build only - skipping deploy." in logs(evs, "success")
    deployed = []
    monkeypatch.setattr(Deployer, "deploy_web_app", lambda self, room, d, w: deployed.append(room.number) or room.number == 1)
    evs = run_job(client, "/api/jobs/webapp-deploy", {**body, "sequential": False, "max_concurrency": 2})
    assert sorted(deployed) == [1, 2]
    monkeypatch.setattr(Deployer, "deploy_web_app", lambda self, room, d, w: 1 / 0)
    assert "Unexpected error" in logs(run_job(client, "/api/jobs/webapp-deploy", {**body, "room_numbers": [1]}))
    assert "required to build" in logs(run_job(client, "/api/jobs/webapp-deploy", {"room_numbers": [1], "do_build": True}))
    assert "paths are required" in logs(run_job(client, "/api/jobs/webapp-deploy", {"room_numbers": [1]}))
    built = []
    monkeypatch.setattr(server.webapp_builder, "build_repo", lambda repo, log, c: built.append(repo.name) or repo.name == "api")
    evs = run_job(client, "/api/jobs/webapp-deploy", {"room_numbers": [1], "do_build": True,
                                                       "backend_repo": str(tmp_path / "api"), "web_repo": str(tmp_path / "web")})
    assert built == ["api", "web"]
    monkeypatch.setattr(server.webapp_builder, "build_repo", lambda repo, log, c: True)
    evs = run_job(client, "/api/jobs/webapp-deploy", {"room_numbers": [1], "do_build": True, "build_only": True,
                                                       "backend_repo": str(tmp_path / "api"), "web_repo": str(tmp_path / "web")})
    assert "Build only" in logs(evs)


def test_download_and_build_jobs(client, monkeypatch, isolated, lab):
    assert client.post("/api/jobs/download-latest", json={}).status_code == 400
    assert client.post("/api/jobs/trigger-build", json={}).status_code == 400
    assert client.get("/api/artifactory/branches").json() == [
        {"label": "wrynose", "build_path": "builds", "branch_filter": "wrynose"}]
    seen = {}

    class Arti:
        def __init__(self, cfg, creds):
            seen["creds"] = creds

        def download_latest(self, cache_dir, log, progress, is_cancelled, build_path, branch_filter):
            seen.update(cache=cache_dir, build_path=build_path)
            progress(1, 2)
            if build_path == "fail":
                raise ArtifactoryError("nope")
            if build_path == "boom":
                raise RuntimeError("bad")
            return cache_dir / "x.swu"

    monkeypatch.setattr(server, "ArtifactoryClient", Arti)
    body = {"artifactory_email": "me@x", "artifactory_token": "t"}
    assert client.post("/api/jobs/download-latest", json={**body, "branch": "nope"}).status_code == 400
    evs = run_job(client, "/api/jobs/download-latest", {**body, "branch": "wrynose"})
    assert seen["cache"].name == "latest-matrix-wrynose" and any(e["type"] == "swu_downloaded" for e in evs)
    run_job(client, "/api/jobs/download-latest", {**body, "cache_dir": str(isolated.tmp / "c")})
    assert seen["cache"] == isolated.tmp / "c"
    save_env_values(isolated.env, {"swu_download_dir": str(isolated.tmp / "saved")})
    run_job(client, "/api/jobs/download-latest", body)
    assert seen["cache"] == isolated.tmp / "saved"
    raw = json.loads(lab.read_text())
    raw["artifactory"]["branches"] = [{"label": "f", "build_path": "fail", "branch_filter": ""},
                                      {"label": "b", "build_path": "boom", "branch_filter": ""}]
    lab.write_text(json.dumps(raw))
    client.post("/api/profiles/select", json={"path": str(lab)})
    assert "nope" in logs(run_job(client, "/api/jobs/download-latest", {**body, "branch": "f"}), "error")
    assert "Unexpected download error" in logs(run_job(client, "/api/jobs/download-latest", {**body, "branch": "b"}))

    results = iter([BuildResult("q", 7, "https://j/7"), BuildResult("q", None, None), JenkinsError("denied"), RuntimeError("x")])

    class Jenk:
        def __init__(self, creds, job_name):
            pass

        def trigger_build(self, log):
            r = next(results)
            if isinstance(r, Exception):
                raise r
            return r

    monkeypatch.setattr(server, "JenkinsClient", Jenk)
    jb = {"jenkins_username": "me", "jenkins_token": "t"}
    assert "Build #7 started" in logs(run_job(client, "/api/jobs/trigger-build", jb))
    assert "number not yet known" in logs(run_job(client, "/api/jobs/trigger-build", jb))
    assert "denied" in logs(run_job(client, "/api/jobs/trigger-build", jb), "error")
    assert "Unexpected error triggering build" in logs(run_job(client, "/api/jobs/trigger-build", jb))


def test_stream_logs_job(client, monkeypatch):
    class Chan(FakeChannel):
        def __init__(self):
            super().__init__()
            self.chunks = [socket.timeout(), b"line one\r\nsudo\r\npart", b"ial\n", b""]

        def recv(self, n):
            c = self.chunks.pop(0)
            if isinstance(c, Exception):
                raise c
            return c

    class Client(FakeSSHClient):
        def exec_command(self, command, get_pty=False):
            self.commands.append(command)
            stdin, out, err = super().exec_command(command)
            out.channel = Chan()
            return stdin, out, err

    c = Client(sudo_password="sudo")
    monkeypatch.setattr(server, "connect", lambda t: c)
    evs = run_job(client, "/api/jobs/stream-logs", {"room_number": 1, "service": "nms"})
    text = logs(evs)
    assert "line one" in text and "partial" in text and "Stopped watching barco-nms" in text
    assert "sudo\n" not in text and "journalctl -fu barco-nms" in c.commands[0] and c.closed

    def down(t):
        raise SSHError("No answer")

    monkeypatch.setattr(server, "connect", down)
    assert "No answer" in logs(run_job(client, "/api/jobs/stream-logs", {"room_number": 1, "service": "custom.service"}))
    monkeypatch.setattr(server, "connect", lambda t: FakeSSHClient(lambda cmd: RuntimeError("chan"), sudo_password="sudo"))
    assert "Log stream error: chan" in logs(run_job(client, "/api/jobs/stream-logs", {"room_number": 1, "service": "matrix"}))


# -- router -------------------------------------------------------------------------------

LIVE_CONFIG = """\
Building configuration...
Current configuration : 1234 bytes
hostname QA1-RTR
no cdp run
ip nat inside source static tcp 10.1.1.13 22 interface Gi0/0/0 201
ip nat inside source static tcp 10.1.1.50 8443 interface Gi0/0/0 11001
ip nat inside source static tcp 10.1.1.1 22 interface Gi0/0/0 21022
ip nat inside source static tcp 10.1.2.77 9222 interface Gi0/0/0 30000
ip nat inside source static tcp 10.1.2.13 443 interface Gi0/0/0 10002
ip nat inside source list NMS-NAT-OUT interface Gi0/0/0 overload
"""


@pytest.fixture
def router(monkeypatch):
    state = SimpleNamespace(shows=[], configs=[], show_out=[LIVE_CONFIG], connects=[])

    def connect(target, **kw):
        state.connects.append((target, kw))
        return FakeSSHClient()

    def show(client, commands, enable_password=None):
        state.shows.append((commands, enable_password))
        out = state.show_out.pop(0) if len(state.show_out) > 1 else state.show_out[0]
        if isinstance(out, BaseException):
            raise out
        return out

    def config(client, commands, enable_password=None, log=None):
        state.configs.append(commands)
        log("Router(config)#", "detail")
        return ""

    monkeypatch.setattr(server, "connect", connect)
    monkeypatch.setattr(server, "run_cisco_show", show)
    monkeypatch.setattr(server, "run_cisco_config_commands", config)
    return state


def test_router_ports_plan(client):
    plan = client.get("/api/router-ports").json()
    assert plan["qa_forwards_shown"] and plan["facility"]["site_code"] == "QA1"
    fw = plan["rooms"][0]["forwards"]
    assert {k: v["outside_port"] for k, v in fw.items()} == {"ssh": 201, "swu": 301, "api": 10001, "nms": 11001}
    assert "ip nat inside source static tcp 10.1.1.13 22 interface Gi0/0/0 201" in plan["nat_script"]
    assert plan["policy"]["source"]["kind"] == "plan" and not plan["policy"]["checked"]
    assert plan["facility"]["rooms"]["1"]["extron"] == ["10.1.1.50"]


def test_router_live_nat(client, router):
    r = client.post("/api/router/live-nat", json={}).json()
    assert r["status"]["201"] == "ok" and r["status"]["11001"] == "conflict" and r["status"]["202"] == "missing"
    assert r["status"]["10002"] == "ok"
    assert r["conflicts"]["11001"]["label"] == "OR 1 Extron NMS"
    other = {o["outside_port"]: o["label"] for o in r["other"]}
    assert other == {21022: "QALAB-Switch1 SSH", 30000: "OR 2 network device DevTools (Matrix App)"}
    assert r["policy"]["checked"] and router.connects[0][1] == {"try_keys": False, "sha1": "fallback"}
    assert router.connects[0][0].username == "admin" and router.connects[0][0].password == "rpw"


def test_router_live_nat_hides_qa_forwards_for_integration(client, router, isolated):
    save_env_values(isolated.env, {"team": "integration"})
    r = client.post("/api/router/live-nat", json={}).json()
    assert "10002" not in r["status"] and 10002 not in {o["outside_port"] for o in r["other"]}
    assert r["rule_count"] == 4
    assert not client.get("/api/router-ports").json()["qa_forwards_shown"]


def test_router_read_errors(client, router, lab, monkeypatch):
    router.show_out = ["no config here"]
    assert client.post("/api/router/live-nat", json={}).status_code == 502
    router.show_out = [SSHError("enable password")]
    assert client.post("/api/router/live-nat", json={}).json()["detail"] == "enable password"
    router.show_out = [EOFError()]
    assert "closed the SSH session" in client.post("/api/router/live-nat", json={}).json()["detail"]
    monkeypatch.setattr(server, "connect", lambda t, **k: (_ for _ in ()).throw(SSHError("auth failed")))
    r = client.post("/api/router/live-nat", json={})
    assert r.status_code == 502 and "Couldn't log in to the router" in r.json()["detail"]
    profile_env_path(lab).write_text("")
    assert client.post("/api/router/live-nat", json={}).status_code == 400


ADD = "ip nat inside source static tcp 10.1.2.13 22 interface Gi0/0/0 202"


def test_apply_nat_and_undo(client, router):
    assert client.post("/api/router/apply-nat", json={"commands": ["hostname evil"]}).status_code == 400
    assert client.post("/api/router/apply-nat", json={"commands": ["conf t", "! c", "", "end"]}).status_code == 400
    router.show_out = ["before", ADD]
    evs = run_job(client, "/api/router/apply-nat", {"commands": ["configure terminal", ADD, "write memory"]})
    assert router.configs == [[ADD]] and "applied and saved" in logs(evs, "success")
    assert {"type": "router_changes", "added": 1, "removed": 0} in evs
    changes = client.get("/api/router/changes").json()
    assert changes["added"] == [ADD] and changes["commands"] == [f"no {ADD}"]
    assert client.post("/api/router/changes/forget").json() == {"added": [], "removed": [], "commands": []}
    router.show_out = ["x"]
    evs = run_job(client, "/api/router/apply-nat", {})
    assert len(router.configs[-1]) == 8  # the whole plan (4 forwards x 2 rooms)


def test_apply_nat_failures(client, router, monkeypatch, lab):
    router.show_out = [SSHError("timed out reading")]
    evs = run_job(client, "/api/router/apply-nat", {"commands": [ADD]})
    assert "won't be undoable" in logs(evs, "warning") and "applied and saved" in logs(evs, "success")
    router.show_out = ["before", SSHError("dropped")]
    evs = run_job(client, "/api/router/apply-nat", {"commands": [ADD]})
    assert "Couldn't record the change for undo: dropped" in logs(evs, "warning")
    router.show_out = [SSHError("SSH auth failed for admin")]
    evs = run_job(client, "/api/router/apply-nat", {"commands": [ADD]})
    assert "Failed to apply router config: SSH auth failed" in logs(evs, "error")
    router.show_out = ["x"]
    monkeypatch.setattr(server, "run_cisco_config_commands", lambda *a, **k: (_ for _ in ()).throw(EOFError()))
    evs = run_job(client, "/api/router/apply-nat", {"commands": [ADD]})
    assert "the router closed the SSH session (EOFError)" in logs(evs, "error")
    monkeypatch.setattr(server, "run_cisco_config_commands", lambda *a, **k: 1 / 0)
    assert "Unexpected error applying router config: ZeroDivisionError" in logs(
        run_job(client, "/api/router/apply-nat", {"commands": [ADD]}))
    monkeypatch.setattr(router_changes, "forget", lambda k: (_ for _ in ()).throw(OSError("locked")))
    assert client.post("/api/router/changes/forget").status_code == 500
    profile_env_path(lab).write_text("")
    assert client.post("/api/router/apply-nat", json={"commands": [ADD]}).status_code == 400


def test_validate_nat_commands():
    cmds, rejected = server.validate_nat_commands([
        "  ip  nat inside source static tcp 1.2.3.4 22 interface Gi0 201 ", f"no {ADD}",
        "ip nat inside source static udp 1.2.3.4 5 9.9.9.9 6 extendable", "wr", "ip route 0.0.0.0 0.0.0.0 1.1.1.1"])
    assert len(cmds) == 3 and rejected == ["ip route 0.0.0.0 0.0.0.0 1.1.1.1"]


def test_switches_live(client, router, monkeypatch, lab):
    assert client.post("/api/switches/live", json={}).json()["switches"][0]["error"].startswith("No switch password")
    with open(profile_env_path(lab), "a") as f:
        f.write("MATRIX_SWITCH_0_PASSWORD=swpw\n")
    calls = []

    def read(host, port, user, password, fiber=False):
        calls.append((port, password, fiber))
        return {"ports": {"1": {"devices": [{"ip": "10.1.1.13"}, {"ip": "10.1.1.50"}, {"ip": "10.0.0.1"},
                                            {"ip": None}, {"ip": "8.8.8.8"}]}}}

    monkeypatch.setattr(server.switch_status, "read_switch", read)
    r = client.post("/api/switches/live", json={"fiber": True}).json()
    sw1, sw2 = r["switches"]
    assert r["fiber"] and calls == [(21022, "swpw", True)] and sw1["ssh_port"] == 21022
    assert [d["label"] for d in sw1["ports"]["1"]["devices"]] == ["OR 1 PC", "OR 1 Extron", "Router", "", ""]
    assert sw2["error"] == "No SSH forward to this switch on the router."
    monkeypatch.setattr(server.switch_status, "read_switch",
                        lambda *a, **k: (_ for _ in ()).throw(SSHError("Refused: its SSH host key changed")))
    msg = client.post("/api/switches/live").json()["switches"][0]["error"]
    assert "QALAB-Switch1's SSH host key changed" in msg and "[172.16.0.5]:21022" in msg
    monkeypatch.setattr(server.switch_status, "read_switch", lambda *a, **k: (_ for _ in ()).throw(SSHError("timeout")))
    assert client.post("/api/switches/live").json()["switches"][0]["error"] == "timeout"


def test_switches_live_needs_facility(monkeypatch, isolated):
    with make_client(monkeypatch, write_profile(isolated.config, "plain")) as c:
        assert c.post("/api/switches/live", json={}).status_code == 400


def test_device_labels_secondary_pc(monkeypatch, isolated, router, fac):
    path = write_profile(isolated.config, "sec", facility=fac, rooms=[{"number": 1, "room_id": "10.1.1.20", "name": "OR 1"}])
    save_env_values(profile_env_path(path), {"router_admin_password": "r", "switch_password": "s"})
    monkeypatch.setattr(server.switch_status, "read_switch",
                        lambda *a, **k: {"ports": {"1": {"devices": [{"ip": "10.1.1.13"}]}}})
    with make_client(monkeypatch, path) as c:
        sw1 = c.post("/api/switches/live", json={}).json()["switches"][0]
        assert sw1["ports"]["1"]["devices"][0]["label"] == "OR 1 Secondary PC"


# -- facility import ------------------------------------------------------------------------

def test_facility_import_first_lab_from_standalone(monkeypatch, isolated):
    """A fresh install (no lab yet) imports its first planner zip."""
    with make_client(monkeypatch, config=AppConfig.standalone()) as c:
        prev = c.post("/api/facility/preview", content=facility_zip()).json()
        r = c.post("/api/facility/import", json={"upload_id": prev["upload_id"]})
        assert r.status_code == 200, r.text
        assert r.json()["active_name"] == "QA Lab (QA1)" and c.get("/api/rooms").json()[0]["room_id"] == "10.1.1.13"
        assert c.get("/api/artifactory/branches").json() == [{"label": "wrynose", "build_path": "", "branch_filter": "wrynose"}]
    assert AppConfig.load(isolated.config / "qa1.json").artifactory.url == ""


def test_facility_import_new_lab(monkeypatch, isolated):
    existing = write_profile(isolated.config, "otherlab", name="Other", router_ip="10.9.9.9")
    with make_client(monkeypatch, existing) as c:
        assert c.post("/api/facility/preview", content=b"junk").status_code == 400
        assert c.post("/api/facility/import", json={"upload_id": "x"}).status_code == 400
        prev = c.post("/api/facility/preview", content=facility_zip(), headers={"x-filename": "QA1-version7.zip"}).json()
        assert prev["new_lab_file"] == "qa1.json" and prev["update"] is None and prev["credentials"] == [
            "nms_password", "site_api_key", "windows_password"]
        assert [s["rooms"] for s in prev["switches"]] == [[1], [2]] and prev["router"]["wan_ip"] == "172.16.0.5"
        r = c.post("/api/facility/import", json={"upload_id": prev["upload_id"], "qa_port_forwards": False}).json()
        assert r["active"].endswith("qa1.json") and r["active_name"] == "QA Lab (QA1)"
        secrets = load_merged_env(r["active"])[1]
        assert secrets["ssh_password"] == "win-pw" and secrets["nms_password"] == "nms-pw"
        assert "win-pw" not in Path(r["active"]).read_text()
        assert c.post("/api/facility/import", json={"upload_id": prev["upload_id"]}).status_code == 400  # used up


def test_facility_import_update(client, isolated, lab):
    prev = client.post("/api/facility/preview", content=facility_zip()).json()
    assert prev["update"]["path"] == str(lab) and prev["update"]["changes"] == []
    other = isolated.config / "qa1.json"
    other.write_text("{}")
    assert client.post("/api/facility/import", json={"upload_id": prev["upload_id"]}).status_code == 409
    assert client.post("/api/facility/import", json={"upload_id": prev["upload_id"], "target": "C:/x.json"}).status_code == 400
    r = client.post("/api/facility/import", json={"upload_id": prev["upload_id"], "target": str(lab)}).json()
    assert r["active"] == str(lab)
    assert load_merged_env(str(lab))[1]["ssh_password"] == "pw"  # existing lab password kept


def test_facility_import_same_router_and_failures(monkeypatch, isolated):
    path = write_profile(isolated.config, "oldlab")
    with make_client(monkeypatch, path) as c:
        (isolated.config / "broken.json").write_text("{bad")
        prev = c.post("/api/facility/preview", content=facility_zip()).json()
        assert prev["same_router"] == [{"path": str(path), "name": "QA Lab 1"}] and prev["update"] is None
        monkeypatch.setattr(server.AppConfig, "load", classmethod(lambda cls, p=None: (_ for _ in ()).throw(KeyError("x"))))
        r = c.post("/api/facility/import", json={"upload_id": prev["upload_id"], "target": "new"})
        assert r.status_code == 500 and "Import failed" in r.json()["detail"]


# -- DevTools -----------------------------------------------------------------------------------

TARGETS = [{"type": "page", "title": "Matrix", "url": "file:///app", "webSocketDebuggerUrl": "ws://127.0.0.1:5555/tok/devtools/page/1"}]


@pytest.fixture
def devtools(monkeypatch):
    state = SimpleNamespace(targets=[TARGETS], client=FakeSSHClient(), navigated=[])

    class R:
        def __init__(self, data, status=200):
            self.data, self.status_code = data, status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError("bad", response=self)

        def json(self):
            return self.data

    def get(url, timeout):
        t = state.targets.pop(0) if len(state.targets) > 1 else state.targets[0]
        if isinstance(t, int):
            return R(None, t)
        if isinstance(t, Exception):
            raise t
        return R(t)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(server, "connect", lambda t: state.client)
    monkeypatch.setattr(server, "largest_page_first", lambda t: t)
    monkeypatch.setattr(server, "cdp_call", lambda t, m, p: state.navigated.append(p["url"]))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(Deployer, "wake_display", lambda self, room, client: True)
    monkeypatch.setattr(Deployer, "show_admin_app", lambda self, room, client: True)
    return state


def test_inspect(client, devtools):
    r = client.post("/api/matrix-app/inspect", json={"room_number": 1}).json()
    assert r["url"] == "http://127.0.0.1:5555/tok/devtools/inspector.html?ws=127.0.0.1:5555/tok/devtools/page/1"
    assert r["title"] == "Matrix" and not r["repaired"]
    tunnels = FakeTunnels.instances[-1]
    assert tunnels.urls == {"t0": r["url"]}
    assert client.get("/api/tunnel/list").json()[0]["room_number"] == 1
    assert client.post("/api/tunnel/t0/close").json() == {"closed": True}


def test_inspect_admin_repairs_page(client, devtools):
    broken = [{**TARGETS[0], "url": "https://wrong"}]
    devtools.targets = [broken, TARGETS]
    r = client.post("/api/matrix-app/inspect", json={"room_number": 1, "app": "admin"}).json()
    assert r["repaired"] and devtools.navigated[0].startswith("file:///usr/share/matrix-admin-app")


def test_inspect_failures(client, devtools, monkeypatch):
    devtools.targets = [502]
    r = client.post("/api/matrix-app/inspect", json={"room_number": 1})
    assert r.status_code == 502 and "DevTools is off" in r.json()["detail"]
    devtools.targets = [requests.ConnectionError("x")]
    assert "didn't answer through the tunnel" in client.post("/api/matrix-app/inspect", json={"room_number": 1}).json()["detail"]
    devtools.targets = [[{"type": "page"}]]
    assert "No Matrix App page" in client.post("/api/matrix-app/inspect", json={"room_number": 1}).json()["detail"]
    monkeypatch.setattr(Deployer, "show_admin_app", lambda self, room, client: self.log("no session", "error") and False)
    r = client.post("/api/matrix-app/inspect", json={"room_number": 1, "app": "admin"})
    assert r.status_code == 502 and r.json()["detail"] == "no session" and devtools.client.closed
    monkeypatch.setattr(server, "connect", lambda t: (_ for _ in ()).throw(SSHError("down")))
    assert client.post("/api/matrix-app/inspect", json={"room_number": 1}).json()["detail"] == "down"


def test_inspect_tunnel_open_fails(monkeypatch, isolated, devtools):
    path = write_profile(isolated.config, "r99", rooms=[{"number": 99, "room_id": "10.1.99.13", "name": "OR 99"}])
    with make_client(monkeypatch, path) as c:
        r = c.post("/api/matrix-app/inspect", json={"room_number": 99})
        assert r.status_code == 502 and "Failed to open tunnel" in r.json()["detail"]


def test_screenshot(client, devtools, monkeypatch):
    monkeypatch.setattr(server, "capture_cdp_screenshot", lambda t: b"\x89PNG")
    r = client.post("/api/matrix-app/screenshot", json={"room_number": 1, "app": "admin"}).json()
    assert r["name"].startswith("or1-matrix-admin-app-") and client.get(r["url"]).content == b"\x89PNG"
    monkeypatch.setattr(server, "capture_cdp_screenshot", lambda t: 1 / 0)
    assert client.post("/api/matrix-app/screenshot", json={"room_number": 1}).status_code == 502


def test_shutdown_closes_tunnels(monkeypatch, lab):
    with make_client(monkeypatch, lab):
        pass
    assert FakeTunnels.instances[-1].closed == ["all"]


def test_presence_idle(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    p = server.Presence()
    assert p.idle_seconds(0) is None  # waiting for the first window
    p.beat("a")
    assert p.idle_seconds(0) is None
    p.bye("a")
    assert p.idle_seconds(0) == 0
    now[0] += 5
    assert p.idle_seconds(0) == 5 and p.idle_seconds(1) is None
    p.beat("b")
    now[0] += server.Presence.CLIENT_TIMEOUT + 1
    assert p.idle_seconds(0) == 0


def test_create_app_survives_unwritable_env(monkeypatch, lab, capsys):
    monkeypatch.setattr(server, "protect_env_files", lambda paths: (_ for _ in ()).throw(OSError("locked")))
    with make_client(monkeypatch, lab):
        pass
    assert "couldn't encrypt saved passwords" in capsys.readouterr().out


def test_create_app_loads_default_profile(monkeypatch, lab):
    monkeypatch.setattr(server, "cleanup_temp_files", lambda: None)
    app = server.create_app()
    with TestClient(app, base_url=BASE) as c:
        assert c.get("/api/profiles").json()["active"] == str(lab)
    assert app.state.idle_seconds() is None
