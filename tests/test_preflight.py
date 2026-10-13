import sys
import tempfile
from pathlib import Path

from matrix_deploy import preflight
from matrix_deploy.config import AppConfig
from matrix_deploy.env_settings import profile_env_path, save_env_values

from conftest import write_profile


def checks_by_id(checks):
    return {c.id: c for c in checks}


def test_no_config(isolated):
    checks = checks_by_id(preflight.run_preflight(None, "Site profile is invalid (KeyError)."))
    assert checks["location"].status == "ok" and checks["bundle"].status == "ok"
    assert checks["profiles"].status == "error" and checks["profile"].detail.startswith("Site profile is invalid")


def test_standalone(isolated):
    checks = checks_by_id(preflight.run_preflight(AppConfig.standalone()))
    assert checks["profiles"].status == "warn" and checks["profile"].status == "warn"
    assert "network" not in checks


def test_unfinished_profile_and_missing_creds(isolated, monkeypatch):
    path = write_profile(isolated.config, rooms=[], router_ip="10.0.0.1", ssh_username="")
    cfg = AppConfig.load(path)
    checks = checks_by_id(preflight.run_preflight(cfg))
    assert checks["profiles"].status == "ok"
    p = checks["profile"]
    assert p.status == "error" and "no rooms defined" in p.detail and "router_ip is '10.0.0.1'" in p.detail
    assert checks["ssh_password"].status == "error" and checks["ssh_password"].action == "creds"
    assert checks["sudo_password"].status == "warn"
    assert checks["router_password"].status == "error" and checks["router_password"].action == "creds-router"
    assert checks["artifactory"].status == "warn" and "username/email and token" in checks["artifactory"].detail
    assert "switch_password" not in checks and "network" not in checks and "folders" not in checks


def test_ready_lab(isolated, monkeypatch, tmp_path):
    path = write_profile(isolated.config, facility={"switches": [{"name": "s1"}, {"name": "s2"}]})
    cfg = AppConfig.load(path)
    save_env_values(profile_env_path(path), {"ssh_password": "a", "sudo_password": "b", "router_admin_password": "c"})
    repo = tmp_path / "repo"
    repo.mkdir()
    save_env_values(isolated.env, {"artifactory_email": "your.email@example.com", "artifactory_token": "t",
                                   "jenkins_username": "me", "jenkins_token": "jt",
                                   "backend_repo": str(repo), "swu_file": str(tmp_path / "gone.swu"),
                                   "swu_download_dir": str(tmp_path / "afile")})
    (tmp_path / "afile").write_text("x")
    monkeypatch.setattr(preflight.shutil, "which", lambda t: None)
    calls = []
    monkeypatch.setattr(preflight, "_tcp_reachable", lambda h, p, t: calls.append((h, p)) or None)
    checks = checks_by_id(preflight.run_preflight(cfg))
    assert checks["profile"].status == "ok" and "2 rooms via 172.16.0.5" in checks["profile"].detail
    assert checks["ssh_password"].status == checks["sudo_password"].status == checks["router_password"].status == "ok"
    assert checks["switch_password"].status == "error" and "2 switches" in checks["switch_password"].detail
    assert checks["artifactory"].status == "warn" and "username/email" in checks["artifactory"].detail
    assert checks["jenkins"].status == "ok"
    assert checks["build_tools"].status == "warn" and "git and npm" in checks["build_tools"].detail
    folders = checks["folders"]
    assert folders.status == "warn" and "gone.swu" in folders.detail and "is a file" in folders.detail
    assert "no package.json" in folders.detail
    assert checks["network"].status == "ok" and sorted(calls) == [("172.16.0.5", 22), ("172.16.0.5", 201)]

    save_env_values(profile_env_path(path), {"switch_password": "s"})
    (repo / "package.json").write_text("{}")
    save_env_values(isolated.env, {"swu_file": "", "swu_download_dir": ""}, allow_clear=True)
    monkeypatch.setattr(preflight.shutil, "which", lambda t: "/usr/bin/" + t)
    checks = checks_by_id(preflight.run_preflight(cfg, network=False))
    assert checks["switch_password"].status == "ok" and checks["build_tools"].status == "ok"
    assert checks["folders"].status == "ok" and "network" not in checks


def test_network_states(isolated, monkeypatch, app_config):
    def run(router_err, room_err):
        monkeypatch.setattr(preflight, "_tcp_reachable", lambda h, p, t: router_err if p == 22 else room_err)
        return checks_by_id(preflight.run_preflight(app_config))["network"]

    both = run("timed out", "timed out")
    assert both.status == "error" and both.action == "network" and "qa1lab.json" in both.fix
    assert run("refused", None).status == "warn"
    room = run(None, "refused")
    assert room.status == "warn" and "OR 1 (port 201)" in room.detail


def test_location_problems(isolated, monkeypatch):
    monkeypatch.setattr(preflight, "_writable", lambda d: False)
    assert checks_by_id(preflight.run_preflight(None))["location"].status == "error"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    zipdir = Path(tempfile.gettempdir()) / "Temp1_MatrixDeploy.zip" / "MatrixDeploy"
    monkeypatch.setattr(preflight, "app_dir", lambda: zipdir)
    loc = checks_by_id(preflight.run_preflight(None))["location"]
    assert loc.status == "error" and "inside a .zip" in loc.detail


def test_missing_bundle_files(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "STATIC_DIR", tmp_path / "nostatic")
    b = checks_by_id(preflight.run_preflight(None))["bundle"]
    assert b.status == "error" and "index.html" in b.detail


def test_helpers(tmp_path):
    assert preflight._writable(tmp_path / "new")
    f = tmp_path / "file"
    f.write_text("x")
    assert not preflight._writable(f / "sub")
    assert preflight._tcp_reachable("127.0.0.1", 1, 0.5) is not None


def test_tcp_reachable_ok():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        assert preflight._tcp_reachable("127.0.0.1", srv.getsockname()[1], 1) is None
    finally:
        srv.close()


def test_summarize_and_report():
    C = preflight.Check
    ok = [C("a", "A", "ok", "fine")]
    assert preflight.summarize(ok)["ok"] and "All checks passed." in preflight.format_report(ok)
    warn = ok + [C("b", "B", "warn", "meh", "do x")]
    report = preflight.format_report(warn)
    assert "[WARN] B: meh" in report and "-> do x" in report and "1 optional item(s)" in report
    bad = warn + [C("c", "C", "error", "broken")]
    s = preflight.summarize(bad)
    assert not s["ok"] and s["errors"] == 1 and s["warnings"] == 1 and s["checks"][2]["id"] == "c"
    assert "1 problem(s) must be fixed" in preflight.format_report(bad)
