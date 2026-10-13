import io
import json
import logging
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

import run_server as rs
from matrix_deploy.web import server

from conftest import write_profile


def test_hide_sign_in_token():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("127.0.0.1", "GET", "/?t=SECRET&x=1"), None)
    assert rs._HideSignInToken().filter(rec)
    assert rec.args == ("127.0.0.1", "GET", "/?t=<hidden>&x=1")
    rec = logging.LogRecord("x", logging.INFO, "", 0, "plain", None, None)
    assert rs._HideSignInToken().filter(rec)


def test_fail(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        rs._fail("broken")
    assert exc.value.code == 1 and "broken" in capsys.readouterr().err
    monkeypatch.setattr(rs, "FROZEN", True)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError()))
    with pytest.raises(SystemExit):
        rs._fail("broken")


def test_ports(monkeypatch):
    busy = socket.socket()
    busy.bind((rs.HOST, 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    try:
        assert rs._find_open_port(rs.HOST, port) != port
        monkeypatch.setattr(rs, "_is_matrix_deploy", lambda p: p == port)
        assert rs._running_instance(port) == port
        monkeypatch.setattr(rs, "_is_matrix_deploy", lambda p: False)
        assert rs._running_instance(port) is None
    finally:
        busy.close()

    class Busy:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            raise OSError("in use")

    monkeypatch.setattr(rs.socket, "socket", Busy)
    with pytest.raises(RuntimeError, match="free port"):
        rs._find_open_port(rs.HOST, 9000)


def test_is_matrix_deploy(monkeypatch):
    monkeypatch.setattr(rs.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b'{"app": "matrix-deploy"}'))
    assert rs._is_matrix_deploy(1)
    monkeypatch.setattr(rs.urllib.request, "urlopen", lambda url, timeout: (_ for _ in ()).throw(OSError()))
    assert not rs._is_matrix_deploy(1)


def test_session_token(isolated):
    assert rs._session_file(8420) == isolated.appdata / "MatrixFacilityManager" / "session-8420"
    assert rs._read_session_token(8420) is None
    rs._session_file(8420).parent.mkdir(parents=True)
    rs._session_file(8420).write_text("tok\n")
    assert rs._read_session_token(8420) == "tok"


def test_app_browser_and_open_window(monkeypatch, tmp_path):
    chrome = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")
    for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(rs.shutil, "which", lambda n: None)
    assert rs._app_browser() is None
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert rs._app_browser() == str(chrome)
    launched, opened = [], []
    monkeypatch.setattr(rs.subprocess, "Popen", lambda args, close_fds: launched.append(args))
    monkeypatch.setattr(rs.webbrowser, "open", opened.append)
    rs._open_window("http://x")
    assert launched == [[str(chrome), "--app=http://x", "--window-size=1440,920"]] and not opened
    monkeypatch.setattr(rs.subprocess, "Popen", lambda args, close_fds: (_ for _ in ()).throw(OSError()))
    rs._open_window("http://y")
    assert opened == ["http://y"]


def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["run_server.py", *args])
    rs.main()


def test_check_mode(monkeypatch, isolated, capsys):
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, "--check")
    out = capsys.readouterr().out
    # No lab yet is only a warning (Health Check > Device still works).
    assert exc.value.code == 0 and "Setup Check" in out and "device health checks only" in out
    write_profile(isolated.config)  # a lab without any saved passwords
    monkeypatch.setattr("matrix_deploy.preflight._tcp_reachable", lambda h, p, t: None)
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, "--check")
    assert exc.value.code == 1 and "[FAIL] Lab SSH password" in capsys.readouterr().out


def test_invalid_profile(monkeypatch, isolated, capsys):
    (isolated.config / "bad.json").write_text(json.dumps({"connection": {"nope": 1}, "rooms": []}))
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "--no-browser")
    assert "can't start" in capsys.readouterr().err


def test_reuses_running_instance(monkeypatch, isolated, capsys):
    write_profile(isolated.config)
    monkeypatch.setattr(rs, "_running_instance", lambda p: 8421)
    windows = []
    monkeypatch.setattr(rs, "_open_window", windows.append)
    run_main(monkeypatch)
    assert windows == ["http://127.0.0.1:8421"] and "already running" in capsys.readouterr().out
    rs._session_file(8421).parent.mkdir(parents=True, exist_ok=True)
    rs._session_file(8421).write_text("tok")
    run_main(monkeypatch)
    assert windows[-1] == "http://127.0.0.1:8421/?t=tok"
    run_main(monkeypatch, "--no-browser")
    assert len(windows) == 2


def test_starts_server_and_quits_when_idle(monkeypatch, isolated, capsys):
    write_profile(isolated.config)
    monkeypatch.setattr(rs, "_running_instance", lambda p: None)
    monkeypatch.setattr(rs, "_find_open_port", lambda h, p: 8455)
    monkeypatch.setattr(rs, "FROZEN", True)
    monkeypatch.setattr(server, "cleanup_temp_files", lambda: None)
    windows = []
    monkeypatch.setattr(rs, "_open_window", windows.append)
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    seen = {}
    real_create = server.create_app

    def create_app(cfg, api_token):
        app = real_create(cfg, api_token=api_token)
        app.state.idle_seconds = lambda: 99
        seen["token"] = api_token
        return app

    monkeypatch.setattr(server, "create_app", create_app)

    class Server:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            seen["session"] = rs._session_file(8455).read_text()
            for _ in range(500):
                if self.should_exit:
                    return
                threading.Event().wait(0.01)

    import uvicorn
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(uvicorn, "Config", lambda app, host, port, log_level: SimpleNamespace())
    run_main(monkeypatch)
    out = capsys.readouterr().out
    assert "No open windows and no running jobs - quitting." in out and "closes by itself" in out
    assert seen["session"] == seen["token"] and not rs._session_file(8455).exists()
    assert windows[0].startswith("file:") and "splash.html?port=8455&t=" in windows[0]


def test_keyboard_interrupt_and_no_browser(monkeypatch, isolated, capsys):
    write_profile(isolated.config)
    monkeypatch.setattr(rs, "_running_instance", lambda p: None)
    monkeypatch.setattr(rs, "_find_open_port", lambda h, p: 8456)
    monkeypatch.setattr(server, "cleanup_temp_files", lambda: None)

    class Server:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            raise KeyboardInterrupt

    import uvicorn
    monkeypatch.setattr(uvicorn, "Server", Server)
    run_main(monkeypatch, "--no-browser", "--keep-running")
    out = capsys.readouterr().out
    assert "Open this link to sign in" in out and "Press Ctrl+C" in out and "stopped." in out
