import socket

import paramiko
import pytest

from matrix_deploy import ssh_client as sc
from matrix_deploy.ssh_client import SSHError, SSHTarget

from fakes import FakeChannel, FakeShell, FakeSSHClient, cisco_handler, rules


class Key:
    def get_base64(self):
        return "AAAA"

    def get_name(self):
        return "ssh-rsa"


def fake_paramiko(monkeypatch, outcomes):
    """``paramiko.SSHClient`` whose ``connect`` takes the next outcome
    (None = success, an exception = raised)."""
    made = []

    class Client:
        def __init__(self):
            self.kwargs = None
            self.loaded = None
            made.append(self)

        def load_host_keys(self, path):
            self.loaded = path

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            self.kwargs = kwargs
            exc = outcomes.pop(0)
            if exc:
                raise exc

    monkeypatch.setattr(sc.paramiko, "SSHClient", Client)
    return made


T = SSHTarget(host="10.0.0.1", port=201, username="arthrex", password="pw", sudo_password="sudo")


def test_connect_password_first(monkeypatch):
    made = fake_paramiko(monkeypatch, [None])
    sc.KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    sc.KNOWN_HOSTS.write_text("")
    client = sc.connect(T)
    kw = made[0].kwargs
    assert kw["password"] == "pw" and not kw["look_for_keys"] and kw["disabled_algorithms"] == sc._NO_SHA1
    assert client.md_sudo_password == "sudo" and made[0].loaded == str(sc.KNOWN_HOSTS)


def test_connect_sha1_modes(monkeypatch):
    made = fake_paramiko(monkeypatch, [None])
    sc.connect(T, sha1="allow")
    assert made[0].kwargs["disabled_algorithms"] is None
    made = fake_paramiko(monkeypatch, [paramiko.ssh_exception.IncompatiblePeer("old"), None])
    sc.connect(T, sha1="fallback")
    assert made[0].kwargs["disabled_algorithms"] == sc._NO_SHA1 and made[1].kwargs["disabled_algorithms"] is None
    fake_paramiko(monkeypatch, [paramiko.ssh_exception.IncompatiblePeer("old")])
    with pytest.raises(SSHError, match="outdated SHA-1"):
        sc.connect(T)


@pytest.mark.parametrize("exc, msg", [
    (paramiko.BadHostKeyException("10.0.0.1", Key(), Key()), "host key changed"),
    (socket.timeout("timed out"), "No answer from 10.0.0.1:201 \\(timed out\\)"),
    # A bare EOFError has no text of its own.
    (EOFError(), "No answer from 10.0.0.1:201 \\(connection closed\\)"),
])
def test_connect_hard_errors(monkeypatch, exc, msg):
    fake_paramiko(monkeypatch, [exc])
    with pytest.raises(SSHError, match=msg):
        sc.connect(T)


def test_connect_auth_fallbacks(monkeypatch):
    auth = paramiko.AuthenticationException("Authentication failed.")
    made = fake_paramiko(monkeypatch, [auth, None])
    sc.connect(T)
    assert made[1].kwargs["look_for_keys"] and made[1].kwargs["password"] is None
    fake_paramiko(monkeypatch, [auth])
    with pytest.raises(SSHError, match="auth failed for arthrex@10.0.0.1:201: Authentication failed"):
        sc.connect(T, try_keys=False)
    fake_paramiko(monkeypatch, [auth, paramiko.AuthenticationException("no keys")])
    with pytest.raises(SSHError, match="password: Authentication failed.; key: no keys"):
        sc.connect(T)
    nopw = SSHTarget(host="h", port=22, username="u")
    fake_paramiko(monkeypatch, [paramiko.AuthenticationException("no keys")])
    with pytest.raises(SSHError, match="key auth failed and no password was provided"):
        sc.connect(nopw)
    with pytest.raises(SSHError, match="no password provided"):
        sc.connect(nopw, try_keys=False)
    fake_paramiko(monkeypatch, [socket.timeout("x")])
    with pytest.raises(SSHError, match="No answer"):
        sc.connect(nopw)


def test_trust_on_first_use_and_forget():
    key = paramiko.RSAKey.generate(1024)
    client = paramiko.SSHClient()
    policy = sc._TrustOnFirstUse()
    policy.missing_host_key(client, "[10.0.0.1]:201", key)
    policy.missing_host_key(client, "10.0.0.2", key)
    assert "[10.0.0.1]:201" in client.get_host_keys()
    assert "[10.0.0.1]:201" in sc._known_hosts() and "10.0.0.2" in sc._known_hosts()
    assert sc.forget_host_key("10.0.0.1", 201)
    assert not sc.forget_host_key("10.0.0.1", 201)
    assert sc.forget_host_key("10.0.0.2", 22)


def test_sudo_prefix_and_exec_command():
    assert sc.sudo_prefix(None) == "sudo"
    pre = sc.sudo_prefix("pw")
    assert "MD_SUDO_PW" in pre and "pw" not in pre
    client = FakeSSHClient(sudo_password="pw")
    sc.exec_command(client, f"{pre} reboot")
    assert client.commands[0].startswith("IFS= read -r MD_SUDO_PW; ") and client.stdins[0].data == "pw\n"
    sc.exec_command(client, "uptime")
    assert client.commands[1] == "uptime" and client.stdins[1].data == ""
    with pytest.raises(SSHError, match="needs the sudo password"):
        sc.exec_command(FakeSSHClient(), f"{pre} reboot")


def test_redact_and_strip_ansi():
    client = FakeSSHClient(sudo_password="pw")
    assert sc.redact(client, "x pw y") == "x <hidden> y" and sc.redact(FakeSSHClient(), "pw") == "pw"
    assert sc.strip_ansi("\x1b[1;32mok\x1b[0m \x1b]8;;file://x\x1b\\link\x1b]8;;\x1b\\") == "ok link"


def test_run_command():
    client = FakeSSHClient(rules(
        ("pty", ("\x1b[31mred\x1b[0m\r\nsecret\r\nline\r\n", 0)),
        ("plain", ("out\n", "err text\n", 3)),
    ), sudo_password="secret")
    lines = []
    assert sc.run_command(client, "pty", get_pty=True, on_line=lines.append) == 0
    assert lines == ["red", "line"]  # the echoed password line is dropped
    lines.clear()
    assert sc.run_command(client, "plain", on_line=lines.append) == 3
    assert lines == ["out", "err text"]
    assert sc.run_command(client, "plain") == 3


def test_scp_helpers(fake_scp, tmp_path, log):
    client = FakeSSHClient()
    f = tmp_path / "a.swu"
    f.write_bytes(b"x" * 2048)
    progress = []
    sc.upload_file(client, str(f), "/home/a.swu", lambda s, t: progress.append((s, t)), log)
    assert fake_scp.instances[0].puts == [(str(f), "/home/a.swu", False)]
    assert progress == [(5, 10), (10, 10)] or progress[-1] == (10, 10)
    assert "Transferred" in log.text("detail")
    sc.upload_file(client, str(tmp_path / "missing"), "/x")  # size unknown, no log
    sc.upload_text(client, "{\"k\": 1}", "/home/or1.json")
    assert fake_scp.instances[-1].puts == [('{"k": 1}', "/home/or1.json", "0600")]
    sc.upload_dir(client, str(tmp_path), "/home/web", log)
    assert fake_scp.instances[-1].puts[0][2] is True and "Uploaded" in log.text("detail")
    fake_scp.files["/r/j.log"] = b"hello"
    dest = tmp_path / "j.log"
    sc.download_file(client, "/r/j.log", str(dest), lambda s, t: None, log)
    assert dest.read_bytes() == b"hello" and "Downloaded" in log.text("detail")
    sc.download_file(client, "/r/j.log", str(dest))


def test_progress_throttle(fake_scp, tmp_path, monkeypatch):
    ticks = iter([0.0, 0.0, 0.01, 0.01])
    monkeypatch.setattr(sc.time, "monotonic", lambda: next(ticks, 1.0))
    seen = []
    f = tmp_path / "a"
    f.write_bytes(b"1")
    sc.upload_file(FakeSSHClient(), str(f), "/x", lambda s, t: seen.append(s))
    assert seen == [5, 10] or seen == [10]


def test_get_disk_free_kb():
    def kb(out, code=0):
        return sc.get_disk_free_kb(FakeSSHClient(lambda c: (out, code)))

    assert kb("Filesystem 1024-blocks Used Available Capacity Mounted\ntmpfs 100 10 90 10% /tmp\n") == 90
    assert kb("", 0) is None and kb("x\ny", 1) is None and kb("only one line") is None
    client = FakeSSHClient(lambda c: ("", 1))
    sc.get_disk_free_kb(client, "/tmp; reboot")
    assert client.commands == ["df -Pk '/tmp; reboot'"]
    assert kb("h\na b c") is None and kb("h\na b c NaN e") is None


def test_port_is_open():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert sc.port_is_open("127.0.0.1", port, 1)
    finally:
        srv.close()
    assert not sc.port_is_open("127.0.0.1", port, 0.5)


def test_wait_for_reboot(monkeypatch, log, no_sleep):
    assert sc.wait_for_reboot(T, log, lambda: True) is False
    calls = iter([False, True, True])
    monkeypatch.setattr(sc, "port_is_open", lambda h, p: next(calls))
    attempts = iter([SSHError("not yet"), FakeSSHClient()])

    def fake_connect(target, timeout):
        r = next(attempts)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(sc, "connect", fake_connect)
    assert sc.wait_for_reboot(T, log, lambda: False, settle_seconds=1) is True
    assert "Still waiting" in log.text("detail")
    flags = iter([False, True])
    monkeypatch.setattr(sc, "port_is_open", lambda h, p: False)
    assert sc.wait_for_reboot(T, log, lambda: next(flags), settle_seconds=1) is False
    assert sc.wait_for_reboot(T, log, lambda: False, max_wait_minutes=1, settle_seconds=0) is None


def test_read_until_prompt_edges(monkeypatch):
    ch = FakeChannel(b"Password: ")
    assert sc._read_until_prompt(ch, 1, sc._PASSWORD_PROMPT_RE) == "Password: "
    closed = FakeChannel(b"partial")
    assert sc._read_until_prompt(closed, 1) == "partial"  # channel done, no prompt
    t = iter([0, 0, 100])
    monkeypatch.setattr(sc.time, "time", lambda: next(t))
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)

    class Silent(FakeChannel):
        def exit_status_ready(self):
            return False

    assert sc._read_until_prompt(Silent(), 5) == ""


def exos(cmd):
    return f"{cmd}\nport table for {cmd}\n* QALAB-Switch1.2 # "


def test_run_cli_show():
    client = FakeSSHClient(shell=lambda: FakeShell(exos, banner="banner\n* QALAB-Switch1.1 # "))
    out = client_out = sc.run_cli_show(client, ["show fdb", "show iparp"], setup=("disable clipaging",))
    assert set(out) == {"show fdb", "show iparp"} and "port table for show fdb" in client_out["show fdb"]
    assert client.shells[0].sent == ["disable clipaging", "show fdb", "show iparp"] and client.shells[0].closed


def test_cisco_show_with_enable_password():
    handler = cisco_handler({"enable": "Password: "})
    client = FakeSSHClient(shell=lambda: FakeShell(handler, banner="Router>"))
    out = sc.run_cisco_show(client, ["show running-config"], enable_password="en")
    assert "show running-config" in out
    assert client.shells[0].sent == ["enable", "en", "terminal length 0", "show running-config"]
    client = FakeSSHClient(shell=lambda: FakeShell(handler, banner="Router>"))
    with pytest.raises(SSHError, match="enable password"):
        sc.run_cisco_show(client, ["show run"])
    assert client.shells[0].closed


ADD = "ip nat inside source static tcp 10.1.1.13 22 interface Gi0 201"
REMOVE = "no ip nat inside source static tcp 10.1.1.13 8443 interface Gi0 11001"
BAD = "ip nat inside source static tcp 10.1.1.13 22 interface Gi9 999"


def test_cisco_config(no_sleep, log):
    handler = cisco_handler({
        REMOVE: "%Static entry in use, do you want to delete child entries? [no]: ",
        "yes": "Router(config)#",
        BAD: "% Invalid input detected at '^' marker.\nRouter(config)#",
        "end": "%SYS-5-CONFIG_I: Configured from console\nRouter#",
    })
    client = FakeSSHClient(shell=lambda: FakeShell(handler))
    with pytest.raises(SSHError, match=r"rejected 1 line\(s\): .*Gi9 999 \(Invalid input detected at '\^' marker\)"):
        sc.run_cisco_config_commands(client, [ADD, REMOVE, BAD], log=log)
    sent = client.shells[0].sent
    assert sent[:3] == ["enable", "terminal length 0", "configure terminal"]
    assert sent[sent.index(REMOVE) + 1] == "yes" and sent[-1] == "write memory"
    assert "answering 'yes'" in log.text("info") and "Router rejected" in log.text("error")

    client = FakeSSHClient(shell=lambda: FakeShell(cisco_handler()))
    assert "write memory" in sc.run_cisco_config_commands(client, [ADD])


def test_cisco_config_confirm_no_and_many_rejects(no_sleep):
    lines = [f"{BAD}{i}" for i in range(7)]
    handler = cisco_handler({**{l: "% Bad\nRouter(config)#" for l in lines},
                             ADD: "Proceed? [confirm]", "no": "Router(config)#"})
    client = FakeSSHClient(shell=lambda: FakeShell(handler))
    with pytest.raises(SSHError, match=r"rejected 7 line\(s\).* \.\.\."):
        sc.run_cisco_config_commands(client, [ADD, *lines])
    assert "no" in client.shells[0].sent


@pytest.mark.parametrize("kw, msg", [({"write_ok": False}, "write memory"), ({"enter_config": False}, "config mode")])
def test_cisco_config_failures(no_sleep, kw, msg, monkeypatch):
    t = iter(range(0, 10_000, 30))
    monkeypatch.setattr(sc.time, "time", lambda: next(t))
    client = FakeSSHClient(shell=lambda: FakeShell(cisco_handler(**kw)))
    with pytest.raises(SSHError, match=msg):
        sc.run_cisco_config_commands(client, [ADD])
