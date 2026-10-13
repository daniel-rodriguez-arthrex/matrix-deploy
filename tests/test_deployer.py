"""Deployer room actions against a scripted room (no SSH)."""

import io
import json
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from matrix_deploy import deployer as dep
from matrix_deploy.config import Room
from matrix_deploy.deployer import Deployer, DeploymentCredentials, DeploymentRequest
from matrix_deploy.ssh_client import SSHError

from fakes import FakeSSHClient, FakeTransport, rules

CREDS = DeploymentCredentials(ssh_password="pw", sudo_password="sudo")
DEVTOOLS_ON = '{"Browser": "Chrome", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}'
CONFIG = {"apiServer": {"trustedEndPoints": ["https://a"], "appFolder": "/old"},
          "logConfig": {"streams": [{"level": "info"}, "odd"]}}


def seq(*answers):
    """Successive answers for one command (the last one repeats)."""
    items = list(answers)
    return lambda: items.pop(0) if len(items) > 1 else items[0]


@pytest.fixture
def room(app_config):
    return app_config.room(1)


@pytest.fixture
def ssh(monkeypatch):
    """``ssh(responder)`` makes the room answer with ``responder``;
    ``ssh(fail=True)`` makes it unreachable."""
    def use(responder=None, fail=False, sudo="sudo"):
        state = SimpleNamespace(clients=[], targets=[])

        def connect(target, **kw):
            state.targets.append(target)
            if fail:
                raise SSHError("No answer from room")
            client = FakeSSHClient(responder, sudo_password=sudo)
            state.clients.append(client)
            return client

        monkeypatch.setattr(dep, "connect", connect)
        return state

    return use


def make(app_config, log, creds=CREDS, cancelled=lambda: False, **kw):
    progress, milestones = [], []
    d = Deployer(app_config, creds, log, lambda s, t: progress.append((s, t)), cancelled,
                 milestone=lambda done, total: milestones.append((done, total)), **kw)
    d.progress_calls, d.milestones = progress, milestones
    return d


def cmds(state):
    return [c for client in state.clients for c in client.commands]


def test_target_and_names(app_config, room, log, tmp_path):
    d = make(app_config, log)
    t = d._target(room)
    assert (t.host, t.port, t.username, t.password, t.sudo_password) == ("172.16.0.5", 201, "arthrex", "pw", "sudo")
    assert d._remote_swu_name(room, tmp_path / "x.swu") == "update-or1-x.swu"
    assert d._plan_steps(DeploymentRequest(room, do_swu=True)) == 4
    assert Deployer._since_label(48) == "2d" and Deployer._since_label(5) == "5h"
    assert Deployer.format_config_path(["apiServer", "list", 2, "x"]) == "apiServer.list[2].x"
    assert make(app_config, log, DeploymentCredentials())._sudo_prefix() == "sudo"


# -- connection failures are logged, never raised ------------------------------

@pytest.mark.parametrize("call", [
    lambda d, r: d.deploy(DeploymentRequest(r, do_swu=False)),
    lambda d, r: d.check_disk_space(r), lambda d, r: d.check_uptime(r), lambda d, r: d.check_specs(r),
    lambda d, r: d.restart_service(r), lambda d, r: d.stop_service(r), lambda d, r: d.matrix_api_status(r),
    lambda d, r: d.fetch_service_logs(r, "x"), lambda d, r: d.fetch_full_journal(r, "x"),
    lambda d, r: d.enable_matrix_app_debugging(r), lambda d, r: d.disable_matrix_app_debugging(r),
    lambda d, r: d.show_admin_app(r), lambda d, r: d.set_admin_app_debugging(r, True), lambda d, r: d.wake_display(r),
    lambda d, r: d.export_support_bundle(r, "x"), lambda d, r: d.check_system_errors(r),
    lambda d, r: d.run_custom_command(r, "ls"), lambda d, r: d.fetch_nms_password(r),
    lambda d, r: d.view_matrix_config(r), lambda d, r: d.read_matrix_config_text(r),
    lambda d, r: d.deploy_matrix_config_text(r, "{}"), lambda d, r: d.set_log_level(r),
    lambda d, r: d.patch_matrix_config(r, []), lambda d, r: d.add_trusted_endpoint(r, "x"),
    lambda d, r: d.configure_web_app(r), lambda d, r: d.reboot(r), lambda d, r: d.shutdown(r),
    lambda d, r: d.deploy_golden_nms_config(r, "MAX"), lambda d, r: d.deploy_nms_link_bandwidth(r, 1),
    lambda d, r: d.reset_web_app(r), lambda d, r: d.diagnose_web_app(r),
])
def test_unreachable_room(app_config, room, log, ssh, call):
    ssh(fail=True)
    assert not call(make(app_config, log), room)
    assert "No answer from room" in log.text("error")


# -- SWU ------------------------------------------------------------------------

DF_OK = "Filesystem 1024-blocks Used Available Capacity Mounted on\ntmpfs 100 1 99999999 1% /tmp\n"


def test_deploy_swu_success(app_config, room, log, ssh, fake_scp, tmp_path, monkeypatch, no_sleep):
    swu = tmp_path / "matrix.swu"
    swu.write_bytes(b"x" * 1024)
    state = ssh(rules(("df -Pk", DF_OK), ("swupdate-client", "Keeping file x\nERROR: soft\nstep\nSWUPDATE successful !\n\n")))
    monkeypatch.setattr(dep, "wait_for_reboot", lambda t, log, c: True)
    fake_scp.fail = [OSError(), None]  # first upload attempt drops
    d = make(app_config, log, swu_upload_semaphore=__import__("threading").Semaphore(1))
    assert d.deploy(DeploymentRequest(room, do_swu=True, swu_file=swu))
    assert "SWUPDATE successful" in log.text("success") and "Keeping file" not in log.text()
    assert "ERROR: soft" in log.text("error") and "Upload attempt 1 failed (OSError)" in log.text("warning")
    assert d.milestones[-1] == (4, 4) and fake_scp.instances[-1].puts[0][1] == "/home/arthrex/update-or1-matrix.swu"
    assert state.clients[0].closed


def test_deploy_swu_failures(app_config, room, log, ssh, fake_scp, tmp_path, monkeypatch, no_sleep):
    swu = tmp_path / "m.swu"
    d = make(app_config, log)
    state = ssh(rules(("df -Pk", DF_OK)))
    assert not d.deploy(DeploymentRequest(room, do_swu=True, swu_file=swu))
    assert "SWU file not found" in log.text("error")
    swu.write_bytes(b"x" * 4096)
    ssh(rules(("df -Pk", "Filesystem 1024-blocks Used Available\ntmpfs 10 9 1 90% /tmp\n")))
    assert not d.deploy(DeploymentRequest(room, do_swu=True, swu_file=swu)) and "Not enough free space" in log.text("error")
    ssh(rules(("df -Pk", ("", 1)), ("swupdate-client", "failed\n")))
    assert not d.deploy(DeploymentRequest(room, do_swu=True, swu_file=swu))
    assert "proceeding anyway" in log.text("warning") and "no success message" in log.text("error")
    fake_scp.fail = OSError("reset")
    assert not d.deploy(DeploymentRequest(room, do_swu=True, swu_file=swu))
    assert "Upload failed after 3 attempt(s): reset" in log.text("error")
    fake_scp.fail = None
    ssh(rules(("df -Pk", DF_OK), ("swupdate-client", "SWUPDATE successful\n")))
    monkeypatch.setattr(dep, "wait_for_reboot", lambda t, log, c: False)
    assert not d.deploy(DeploymentRequest(room, do_swu=True, swu_file=swu))
    assert "did not come back online" in log.text("error")
    assert not make(app_config, log, cancelled=lambda: True).deploy(DeploymentRequest(room, do_swu=True, swu_file=swu))
    assert "Cancelled before upload" in log.text("warning")
    flags = iter([False, True])
    assert not make(app_config, log, cancelled=lambda: next(flags)).deploy(DeploymentRequest(room, do_swu=True, swu_file=swu))
    assert "Cancelled before install" in log.text("warning")


def test_upload_reconnects_dead_transport(app_config, room, log, ssh, fake_scp, tmp_path, monkeypatch, no_sleep):
    swu = tmp_path / "m.swu"
    swu.write_bytes(b"x")
    state = ssh(rules(("df -Pk", DF_OK), ("swupdate-client", "SWUPDATE successful\n")))
    monkeypatch.setattr(dep, "wait_for_reboot", lambda t, log, c: True)
    d = make(app_config, log)
    real_connect = dep.connect

    def connect(target, **kw):
        c = real_connect(target)
        if len(state.clients) == 1:
            c.transport = FakeTransport(active=False)
        return c

    monkeypatch.setattr(dep, "connect", connect)
    assert d.deploy(DeploymentRequest(room, do_swu=True, swu_file=swu))
    assert "Reconnecting before upload retry" in log.text("detail") and len(state.clients) == 2


# -- NMS password ----------------------------------------------------------------

def test_fetch_nms_password(app_config, room, log, ssh):
    d = make(app_config, log)
    ssh(rules(("act-mfg-eeprom", ("serial = 1\nbarco_nms_password = s3cr3t\napi_key: k\n", "warn text", 0))))
    assert d.fetch_nms_password(room) == "s3cr3t"
    assert "s3cr3t" not in log.text() and "<hidden>" in log.text("detail") and "warn text" in log.text("warning")
    assert d.get_nms_password(room) and "NMS password read" in log.text("success")
    state = ssh(rules(("act-mfg-eeprom", "nothing\n")))
    assert not d.get_nms_password(room) and "Could not parse" in log.text("warning")
    assert state.clients[0].stdins[0].data == "sudo\n"
    assert make(app_config, log, DeploymentCredentials("pw")).fetch_nms_password(room, FakeSSHClient()) is None
    assert d._parse_eeprom_password("Admin: adm1n") == "adm1n"
    assert d._parse_eeprom_password("") is None


# -- simple reads -----------------------------------------------------------------

def test_checks(app_config, room, log, ssh):
    d = make(app_config, log)
    state = ssh(rules(("df -h", "Filesystem Size\ntmpfs 4G\n"), ("^uptime", " 10:00 up 2 days\n"), ("uname", "Linux\n")))
    assert d.check_disk_space(room) and "OR 1: tmpfs 4G" in log.text("detail")
    assert d.check_uptime(room) and "up 2 days" in log.text("detail")
    assert d.check_specs(room) and "--- Kernel/OS ---" in log.text("detail")
    assert d.check_system_errors(room, 48) and "no errors in the last 2d" in log.text("success")
    ssh(rules(("journalctl", "kernel: oops\n")))
    assert d.check_system_errors(room) and "--- Kernel ---" in log.text("detail")
    ssh(rules())
    assert not d.check_disk_space(room) and not d.check_uptime(room) and not d.check_specs(room)
    assert "Could not retrieve" in log.text("error")
    assert all(c.closed for c in state.clients)


def test_services(app_config, room, log, ssh):
    d = make(app_config, log)
    state = ssh(rules(("systemctl stop", ("inactive\n", 3)), ("status", "active (running)\n")))
    assert d.restart_service(room) and d.restart_nms_service(room)
    assert d.stop_service(room) and d.stop_nms_service(room)
    assert d.matrix_api_status(room) and d.nms_service_status(room)
    assert "Service restarted." in log.text("success") and "Service stopped." in log.text("success")
    sudo_cmds = [c for c in cmds(state) if "systemctl restart" in c]
    assert sudo_cmds and all(c.startswith("IFS= read -r MD_SUDO_PW;") for c in sudo_cmds)
    ssh(rules(("status", ("", 1))))
    assert not d.restart_service(room) and not d.matrix_api_status(room)
    plain = make(app_config, log, DeploymentCredentials("pw"))
    state = ssh(rules(("status", "x\n")), sudo=None)
    assert plain.matrix_api_status(room) and cmds(state)[0].startswith("systemctl")


def test_logs_and_journal(app_config, room, log, ssh, fake_scp, tmp_path):
    d = make(app_config, log)
    ssh(rules(("journalctl -u matrix-api", "api line\n"), ("journalctl -u barco-nms", "nms line\n")))
    path = d.fetch_service_logs(room, tmp_path / "out", lines=50)
    text = path.read_text()
    assert path.name.startswith("or1-logs-") and "api line" in text and "nms line" in text and "last 50 lines" in text
    state = ssh(rules())
    d.fetch_service_logs(room, tmp_path / "out", since_hours=24)
    assert "--since -24h" in cmds(state)[0]

    fake_scp.files["/home/arthrex/or1-full-journal.log"] = b"journal body\n"
    state = ssh(rules())
    path = d.fetch_full_journal(room, tmp_path / "j", since_hours=2)
    assert "journal body" in path.read_text() and "Window: last 2h" in path.read_text()
    assert not list((tmp_path / "j").glob(".*.tmp")) and "umask 077" in cmds(state)[0]
    assert cmds(state)[-1] == "rm -f /home/arthrex/or1-full-journal.log"
    fake_scp.fail = OSError("lost")
    assert d.fetch_full_journal(room, tmp_path / "j") is None and "Download failed: lost" in log.text("error")


# -- DevTools / sway ------------------------------------------------------------------

def test_enable_matrix_app_debugging(app_config, room, log, ssh, no_sleep):
    assert not make(app_config, log, DeploymentCredentials("pw")).enable_matrix_app_debugging(room)
    d = make(app_config, log)
    ssh(rules(("curl", DEVTOOLS_ON)))
    assert d.enable_matrix_app_debugging(room) and "already enabled" in log.text("success")
    curl = seq("", "", "", "", DEVTOOLS_ON)
    state = ssh(lambda c: curl() if "curl" in c else ("flags added", "", 0))
    assert d.enable_matrix_app_debugging(room) and "DevTools ready on port 9222" in log.text("success")
    script = state.clients[0].stdins[1].data
    assert script.startswith("sudo\n") and "--remote-debugging-port=9222" in script and "flags added" in log.text("detail")
    ssh(lambda c: "" if "curl" in c else ("", "Launcher not found", 2))
    assert not d.enable_matrix_app_debugging(room) and "Failed to enable" in log.text("error")
    ssh(lambda c: "" if "curl" in c else ("", 0))
    assert not d.enable_matrix_app_debugging(room) and "did not become ready" in log.text("error")
    flags = seq(False, True)
    assert not make(app_config, log, cancelled=flags).enable_matrix_app_debugging(room)


def test_disable_matrix_app_debugging(app_config, room, log, ssh, no_sleep):
    assert not make(app_config, log, DeploymentCredentials("pw")).disable_matrix_app_debugging(room)
    d = make(app_config, log)
    ssh(lambda c: "" if "curl" in c else ("removed", 0))
    assert d.disable_matrix_app_debugging(room) and "remote debugging is off" in log.text("success")
    ssh(lambda c: DEVTOOLS_ON if "curl" in c else ("", 0))
    assert not d.disable_matrix_app_debugging(room) and "still answering" in log.text("error")
    ssh(lambda c: ("", 5))
    assert not d.disable_matrix_app_debugging(room) and "Failed to disable" in log.text("error")
    ssh(lambda c: DEVTOOLS_ON if "curl" in c else ("", 0))
    assert not make(app_config, log, cancelled=lambda: True).disable_matrix_app_debugging(room)


def test_admin_app(app_config, room, log, ssh, no_sleep):
    nosudo = make(app_config, log, DeploymentCredentials("pw"))
    assert not nosudo.show_admin_app(room) and not nosudo.set_admin_app_debugging(room, True)
    assert not nosudo.wake_display(room)
    d = make(app_config, log)
    state = ssh(rules(("bash -s", ("", 0))))
    assert d.show_admin_app(room) and state.clients[0].closed
    given = FakeSSHClient(lambda c: ("", 6))
    assert not d.show_admin_app(room, given) and not given.closed and "admin app" in log.text("error")
    assert d.wake_display(room) and "display woken" in log.text("success")
    assert not d.wake_display(room, given) and "Failed to wake" in log.text("error")

    ssh(rules(("curl", DEVTOOLS_ON)))
    assert d.set_admin_app_debugging(room, True) and "already on" in log.text("success")
    curl = seq("", DEVTOOLS_ON)
    state = ssh(lambda c: curl() if "curl" in c else ("", 0))
    assert d.set_admin_app_debugging(room, True) and "DevTools ready (port 9223)" in log.text("success")
    assert "--remote-debugging-port=9223" in state.clients[0].stdins[1].data
    state = ssh(lambda c: "" if "curl" in c else ("", 0))
    assert d.set_admin_app_debugging(room, False) and "DevTools off" in log.text("success")
    assert "sed -i -E" in state.clients[0].stdins[0].data
    ssh(lambda c: ("", 1) if "bash -s" in c else "")
    assert not d.set_admin_app_debugging(room, False) and "Failed to relaunch" in log.text("error")
    ssh(lambda c: "" if "curl" in c else ("  123 electron matrix-admin-app.asar\n", 0))
    assert not d.set_admin_app_debugging(room, True) and "did not turn on" in log.text("error")
    assert "matrix-admin-app.asar" in log.text("detail")
    ssh(lambda c: "" if "curl" in c else ("", 0))
    assert not make(app_config, log, cancelled=lambda: True).set_admin_app_debugging(room, True)


# -- support bundle --------------------------------------------------------------------

def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("matrix/diag.txt", "x")
        z.writestr("collection_report.txt", "y")
    return buf.getvalue()


def test_export_support_bundle(app_config, room, log, ssh, fake_scp, tmp_path):
    assert make(app_config, log, DeploymentCredentials("pw")).export_support_bundle(room, tmp_path) is None
    d = make(app_config, log)
    remote = "/home/arthrex/matrix-deploy-support/SynergyLogs-1.zip"
    fake_scp.files[remote] = _zip_bytes()
    state = ssh(rules(("get-support", (f"collecting...\n\nWrote {remote}\n", 2))))
    path = d.export_support_bundle(room, tmp_path, skip_nms=True, skip_intel=True)
    assert path == tmp_path / "or1-SynergyLogs-1.zip" and "  - collection_report.txt" in log.text("detail")
    assert "missing artifacts (exit 2)" in log.text("warning")
    c = cmds(state)
    assert any("--skip-nms --skip-intel-diag" in x for x in c) and any("chown" in x for x in c)
    assert "rm -rf /home/arthrex/matrix-deploy-support" in c[-1]

    fake_scp.files[remote] = b"not a zip"
    ssh(rules(("ls -1t", remote + "\n"), ("get-support", "done\n")))
    assert d.export_support_bundle(room, tmp_path) and "Could not list bundle contents" in log.text("warning")
    ssh(rules())
    assert d.export_support_bundle(room, tmp_path) is None and "bundle path" in log.text("error")
    fake_scp.fail = OSError("dropped")
    ssh(rules(("get-support", remote + "\n")))
    assert d.export_support_bundle(room, tmp_path) is None and "Download failed: dropped" in log.text("error")


# -- custom command --------------------------------------------------------------------

def test_run_custom_command(app_config, room, log, ssh):
    d = make(app_config, log)
    assert not d.run_custom_command(room, "   ") and "No command entered" in log.text("error")
    state = ssh(rules(("dmesg", ("line1\n", 0)), ("false", ("", 1))))
    assert d.run_custom_command(room, "dmesg", use_sudo=True) and "OR 1: line1" in log.text("detail")
    assert "MD_SUDO_PW" in cmds(state)[0]
    assert not d.run_custom_command(room, "false") and "exited with status 1" in log.text("warning")


# -- matrix.api.config.json ------------------------------------------------------------

def cat_config(data=CONFIG, code=0):
    text = data if isinstance(data, str) else json.dumps(data, indent=2)
    return rules((r"cat .*matrix\.api\.config\.json", (text + "\n", code)), ("systemctl", "active\n"))


def uploaded_json(fake_scp):
    return json.loads(fake_scp.instances[-1].puts[0][0])


def test_view_and_read_config(app_config, room, log, ssh, tmp_path):
    d = make(app_config, log)
    ssh(cat_config())
    assert d.view_matrix_config(room, tmp_path / "v") and "Saved raw config copy" in log.text("success")
    assert json.loads(next((tmp_path / "v").glob("*.json")).read_text()) == CONFIG
    assert json.loads(d.read_matrix_config_text(room)) == CONFIG
    ssh(cat_config(code=1))
    assert not d.view_matrix_config(room) and d.read_matrix_config_text(room) is None
    ssh(cat_config(" "))
    assert d.read_matrix_config_text(room) is None and "no output" in log.text("error")


def test_deploy_config_text(app_config, room, log, ssh, fake_scp):
    d = make(app_config, log)
    assert not d.deploy_matrix_config_text(room, "{bad") and "invalid JSON" in log.text("error")
    state = ssh(cat_config())
    assert d.deploy_matrix_config_text(room, '{"a": 1}\n\n')
    assert fake_scp.instances[-1].puts[0] == ('{"a": 1}\n', "/home/arthrex/or1.json", "0600")
    assert any(c.endswith("rm -f /home/arthrex/or1.json") for c in cmds(state))
    ssh(rules(("systemctl", ("failed", 1))))
    assert not d.deploy_matrix_config_text(room, "{}") and "service restart failed" in log.text("error")
    fake_scp.fail = OSError("scp")
    assert not d.deploy_matrix_config_text(room, "{}") and "Config upload failed: scp" in log.text("error")


def test_set_log_level(app_config, room, log, ssh, fake_scp):
    d = make(app_config, log)
    ssh(cat_config())
    assert d.set_log_level(room, "debug")
    assert uploaded_json(fake_scp)["logConfig"]["streams"] == [{"level": "debug"}, "odd"]
    for bad, msg in ((cat_config(code=1), "Failed to read"), (cat_config(" "), "no output"),
                     (cat_config("x" * 600), "Could not parse"), (cat_config({"a": 1}), "no logConfig.streams")):
        ssh(bad)
        assert not d.set_log_level(room) and msg in log.text("error")
    assert "(truncated)" in log.text("detail")
    ssh(rules((r"cat ", json.dumps(CONFIG)), ("systemctl", ("", 1))))
    assert not d.set_log_level(room)
    fake_scp.fail = OSError("x")
    assert not d.set_log_level(room)


@pytest.mark.parametrize("data, change, result, after", [
    ({}, {"op": "set", "path": ["a", "b"], "value": 1}, True, {"a": {"b": 1}}),
    ({"a": 1}, {"op": "set", "path": ["a"], "value": 1}, False, {"a": 1}),
    ({}, {"op": "delete", "path": ["a", "b"]}, False, {}),
    ({"a": 1}, {"op": "delete", "path": ["a"]}, True, {}),
    ({"a": 1}, {"op": "delete", "path": ["b"]}, False, {"a": 1}),
    ({"l": [1, {"x": 1}]}, {"op": "set", "path": ["l", 1, "x"], "value": 2}, True, {"l": [1, {"x": 2}]}),
    ({"l": [1]}, {"op": "set", "path": ["l", 5, "x"], "value": 2}, None, {"l": [1]}),
    ({"l": [1, 2]}, {"op": "set", "path": ["l", 0], "value": 9}, True, {"l": [9, 2]}),
    ({"l": [1, 2]}, {"op": "set", "path": ["l", 0], "value": 1}, False, {"l": [1, 2]}),
    ({"l": [1, 2]}, {"op": "delete", "path": ["l", 0]}, None, {"l": [1, 2]}),
    ({"l": [1]}, {"op": "set", "path": ["l", 3], "value": 1}, None, {"l": [1]}),
    ({}, {"op": "add_item", "path": ["t"], "value": "x"}, True, {"t": ["x"]}),
    ({"t": ["x"]}, {"op": "add_item", "path": ["t"], "value": "x"}, False, {"t": ["x"]}),
    ({"t": ["x", "y"]}, {"op": "remove_item", "path": ["t"], "value": "x"}, True, {"t": ["y"]}),
    ({"t": ["y"]}, {"op": "remove_item", "path": ["t"], "value": "x"}, False, {"t": ["y"]}),
    ({}, {"op": "remove_item", "path": ["t"], "value": "x"}, False, {}),
    ({}, {"op": "remove_item", "path": ["a", "t"], "value": "x"}, False, {}),
    ({"t": "str"}, {"op": "add_item", "path": ["t"], "value": "x"}, None, {"t": "str"}),
    ({"l": [1]}, {"op": "add_item", "path": ["l", 0], "value": "x"}, None, {"l": [1]}),
])
def test_apply_config_change(data, change, result, after):
    assert Deployer._apply_config_change(data, change) is result
    assert data == after


def test_patch_matrix_config(app_config, room, log, ssh, fake_scp):
    d = make(app_config, log)
    ssh(cat_config())
    changes = [{"op": "set", "path": ["apiServer", "appFolder"], "value": "/new"},
               {"op": "add_item", "path": ["apiServer", "trustedEndPoints"], "value": "https://b"},
               {"op": "remove_item", "path": ["apiServer", "trustedEndPoints"], "value": "https://a"},
               {"op": "delete", "path": ["logConfig"]},
               {"op": "set", "path": ["apiServer", "appFolder"], "value": "/new"}]
    assert d.patch_matrix_config(room, changes)
    assert uploaded_json(fake_scp) == {"apiServer": {"trustedEndPoints": ["https://b"], "appFolder": "/new"}}
    text = log.text("detail")
    assert 'apiServer.appFolder = "/new"' in text and "+ \"https://b\"" in text and "logConfig removed" in text
    assert "already up to date" in text
    n = len(fake_scp.instances)
    assert d.patch_matrix_config(room, [{"op": "set", "path": ["apiServer", "appFolder"], "value": "/old"}])
    assert len(fake_scp.instances) == n and "no push, no restart" in log.text("success")
    assert not d.patch_matrix_config(room, [{"op": "set", "path": ["logConfig", "streams", 9, "x"], "value": 1}])
    assert "doesn't exist in the same shape" in log.text("error")
    ssh(cat_config(code=1))
    assert not d.patch_matrix_config(room, changes)
    ssh(rules((r"cat ", json.dumps(CONFIG)), ("systemctl", ("", 1))))
    assert not d.patch_matrix_config(room, changes)


def test_trusted_endpoints_and_web_app_config(app_config, room, log, ssh, fake_scp):
    d = make(app_config, log)
    ssh(cat_config())
    assert d.add_trusted_endpoint(room, "https://new")
    assert uploaded_json(fake_scp)["apiServer"]["trustedEndPoints"] == ["https://a", "https://new"]
    assert d.add_trusted_endpoint(room, "https://a") and "already trusted" in log.text("detail")
    ssh(cat_config({"apiServer": {"trustedEndPoints": "oops"}}))
    assert not d.add_trusted_endpoint(room, "x") and not d.configure_web_app(room)
    assert "is not a list" in log.text("error")
    ssh(cat_config(code=1))
    assert not d.add_trusted_endpoint(room, "x") and not d.configure_web_app(room)

    ssh(cat_config())
    assert d.configure_web_app(room)
    api = uploaded_json(fake_scp)["apiServer"]
    assert api["trustedEndPoints"] == ["https://a", "https://172.16.0.5:10001", "https://172.16.0.5:10002"]
    assert api["masterPairKey"] == "1234" and api["appFolder"].endswith("arthrex-synergy-matrix")
    ssh(cat_config({"apiServer": {"trustedEndPoints": ["https://172.16.0.5:10001", "https://172.16.0.5:10002"],
                                  **Deployer.WEB_APP_CONFIG_FIELDS}}))
    assert d.configure_web_app(room) and "already set to the desired values" in log.text("detail")
    ssh(rules((r"cat ", json.dumps(CONFIG)), ("systemctl", ("", 1))))
    assert not d.configure_web_app(room) and not d.add_trusted_endpoint(room, "x")


def test_configure_web_app_refused_on_facility_lab(isolated, room, log, ssh):
    from conftest import write_profile
    from matrix_deploy.config import AppConfig
    cfg = AppConfig.load(write_profile(isolated.config, "fac", qa_port_forwards=False))
    state = ssh(cat_config())
    assert not make(cfg, log).configure_web_app(room) and "Refused" in log.text("error") and not state.clients


# -- power ------------------------------------------------------------------------------

def test_reboot_and_shutdown(app_config, room, log, ssh, monkeypatch, no_sleep):
    d = make(app_config, log)
    state = ssh(rules())
    monkeypatch.setattr(dep, "wait_for_reboot", lambda t, log, c: True)
    assert d.reboot(room) and "back online" in log.text("success")
    monkeypatch.setattr(dep, "wait_for_reboot", lambda t, log, c: False)
    assert not d.reboot(room)
    assert d.shutdown(room) and "shutdown -h now" in cmds(state)[-1]


# -- NMS configs --------------------------------------------------------------------------

def test_golden_nms_config(app_config, room, log, ssh, fake_scp, monkeypatch, tmp_path):
    d = make(app_config, log)
    assert not d.deploy_golden_nms_config(room, "HUGE") and "Unknown bandwidth" in log.text("error")
    state = ssh(rules())
    assert d.deploy_golden_nms_config(room, "LIMITED") and "bandwidth set to LIMITED" in log.text("success")
    assert fake_scp.instances[-1].puts[0][0].endswith("nms-config.limited-bandwidth.json")
    assert any("rm -f /home/arthrex/nms-config-or1.json" in c for c in cmds(state))
    ssh(rules(("systemctl", ("", 1))))
    assert not d.deploy_golden_nms_config(room, "MAX")
    fake_scp.fail = OSError("x")
    assert not d.deploy_golden_nms_config(room, "MAX") and "Upload failed" in log.text("error")
    monkeypatch.setattr(dep, "golden_nms_config_path", lambda b: tmp_path / "gone.json")
    assert not d.deploy_golden_nms_config(room, "MAX") and "not found" in log.text("error")


def test_nms_bandwidth_and_overlay(app_config, room, log, ssh, fake_scp, monkeypatch):
    d = make(app_config, log)
    ssh(rules(("cat ", "    upload: 250000\n")))
    assert d.deploy_nms_link_bandwidth(room, 100000)
    text, remote, mode = fake_scp.instances[-1].puts[0]
    assert "upload: 100000" in text and mode == "0644" and remote == "/home/arthrex/application-user-or1.yml"
    assert d.deploy_nms_remove_overlay(room) and "video overlay removed" in log.text("success")
    text = fake_scp.instances[-1].puts[0][0]
    assert "upload: 250000" in text and "noVideoOverlayId" in text
    ssh(rules(("cat ", ("", 1)), ("systemctl", ("", 1))))
    assert not d.deploy_nms_remove_overlay(room) and "leaving it at 500000" in log.text("warning")
    ssh(rules(("cat ", "no bandwidth here\n")))
    assert d._read_current_nms_bandwidth(room) is None
    ssh(lambda c: RuntimeError("x"))
    assert d._read_current_nms_bandwidth(room) is None
    ssh(fail=True)
    assert d._read_current_nms_bandwidth(room) is None
    ssh(rules())
    fake_scp.fail = OSError("x")
    assert not d.deploy_nms_link_bandwidth(room, 1)
    monkeypatch.setattr(dep, "render_nms_user_config", lambda *a: 1 / 0)
    assert not d.deploy_nms_link_bandwidth(room, 1) and "Failed to render" in log.text("error")


# -- fingerprints ---------------------------------------------------------------------------

def test_remove_known_hosts_entry(app_config, room, log, monkeypatch):
    d = make(app_config, log)
    forgot = []
    monkeypatch.setattr(dep, "forget_host_key", lambda h, p: forgot.append((h, p)) or True)
    monkeypatch.setattr(dep.tls_pins, "forget", lambda h, p: forgot.append((h, p)) or True)
    monkeypatch.setattr(dep.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "Host removed\n", ""))
    assert d.remove_known_hosts_entry(room)
    assert forgot == [("172.16.0.5", 201), ("172.16.0.5", 11001)] and "pinned NMS certificate" in log.text("detail")
    monkeypatch.setattr(dep.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 255, None, "err\n"))
    assert not d.remove_known_hosts_entry(room) and "exited with code 255" in log.text("error")

    def raiser(exc):
        def run(*a, **k):
            raise exc
        return run

    monkeypatch.setattr(dep.subprocess, "run", raiser(FileNotFoundError()))
    assert not d.remove_known_hosts_entry(room) and "ssh-keygen not found" in log.text("error")
    monkeypatch.setattr(dep.subprocess, "run", raiser(subprocess.TimeoutExpired("ssh-keygen", 10)))
    assert not d.remove_known_hosts_entry(room) and "Failed to run ssh-keygen" in log.text("error")
    monkeypatch.setattr(dep, "forget_host_key", raiser(PermissionError(13, "denied", "known_hosts")))
    assert not d.remove_known_hosts_entry(room) and "Couldn't update known_hosts" in log.text("error")


# -- web app -----------------------------------------------------------------------------------

def test_deploy_web_app(app_config, room, log, ssh, fake_scp, tmp_path):
    d = make(app_config, log)
    dist, web = tmp_path / "dist", tmp_path / "web"
    assert not d.deploy_web_app(room, dist, web) and "backend dist not found" in log.text("error")
    dist.mkdir()
    assert not d.deploy_web_app(room, dist, web) and "web assets not found" in log.text("error")
    web.mkdir()
    state = ssh(rules(("mkdir -m 700", ("", 1))))
    assert not d.deploy_web_app(room, dist, web) and "private staging folder" in log.text("error")
    state = ssh(rules(("systemctl", "active\n")))
    assert d.deploy_web_app(room, dist, web) and "Test: https://172.16.0.5:10001/app/" in log.text("success")
    assert [p[1] for i in fake_scp.instances[-2:] for p in i.puts] == [
        "/home/arthrex/or1-webapp-upgrade/web", "/home/arthrex/or1-webapp-upgrade/dist"]
    assert cmds(state)[-1] == "rm -rf /home/arthrex/or1-webapp-upgrade"
    ssh(rules(("bash -lc", ("", 1))))
    assert not d.deploy_web_app(room, dist, web) and "Install/patch step failed" in log.text("error")
    ssh(rules(("systemctl restart", ("", 1))))
    assert not d.deploy_web_app(room, dist, web) and "Service restart failed" in log.text("error")


def test_reset_and_diagnose_web_app(app_config, room, log, ssh):
    d = make(app_config, log)
    ssh(rules(("CLEANUP_OK", "CLEANUP_OK\n"), ("SERVICE_RESET_OK", "SERVICE_RESET_OK\n")))
    assert d.reset_web_app(room) and "reset complete" in log.text("success") and not log.text("warning")
    ssh(rules())
    assert d.reset_web_app(room) and "Cleanup may have failed" in log.text("warning")
    assert "Systemd service reset may have failed" in log.text("warning")
    ssh(rules(("ls -la", "drwx index.html\n")))
    assert d.diagnose_web_app(room) and "--- AppArmor status ---" in log.text("detail")
