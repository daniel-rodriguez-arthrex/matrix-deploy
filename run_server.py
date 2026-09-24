#!/usr/bin/env python3
"""Entry point for Matrix Deploy (also ``MatrixDeploy.exe``, see ``build_exe.ps1``).

Starts a local-only (127.0.0.1) web server and opens the UI in its own app
window (Chrome/Edge ``--app`` mode: no tabs or address bar, falling back to
the default browser). Never bind this to 0.0.0.0: the server holds SSH/sudo/
Artifactory secrets in memory and is designed for a single local user.

The packaged exe has no console window: it quits by itself once its last
window is closed and no job is running, logs to ``matrixdeploy.log`` next to
the exe, and reports startup problems in a message box.

Usage:
    python run_server.py [--port 8420] [--no-browser] [--check] [--keep-running]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

HOST = "127.0.0.1"
DEFAULT_PORT = 8420
FROZEN = getattr(sys, "frozen", False)
# Quit this long after the last window closed (covers a page reload).
IDLE_EXIT_AFTER = 10


def _setup_stdio() -> bool:
    """Make the packaged app a single window. The exe is a console-type build
    (SentinelOne quarantines PyInstaller's windowed builds on sight), so when
    it was double-clicked - i.e. it's the only process on its console - it
    detaches, which closes that console window, and logs to
    ``matrixdeploy.log`` instead. Started from a terminal, it keeps printing
    there (e.g. ``--check``). Returns True if output goes to a console."""
    if not (FROZEN and os.name == "nt"):
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    procs = (ctypes.c_uint * 4)()
    if kernel32.GetConsoleProcessList(procs, 4) > 1:
        return True
    kernel32.FreeConsole()
    try:
        from matrix_deploy.config import app_dir

        sys.stdout = sys.stderr = open(app_dir() / "matrixdeploy.log", "w", buffering=1, encoding="utf-8")
    except OSError:
        sys.stdout = sys.stderr = open(os.devnull, "w")
    return False


HAS_CONSOLE = _setup_stdio()


def _alert(text: str, error: bool = True) -> None:
    """Show ``text`` to the user: a message box for the windowed exe, else print."""
    print(text, file=sys.stderr if error else sys.stdout)
    if FROZEN and not HAS_CONSOLE and os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Matrix Deploy", 0x10 if error else 0x40)


def _find_open_port(host: str, preferred: int) -> int:
    """Return ``preferred`` if free, otherwise the next free port."""
    port = preferred
    while port < preferred + 50:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                port += 1
    raise RuntimeError(f"Could not find a free port near {preferred} on {host}")


def _is_matrix_deploy(port: int) -> bool:
    """True if a Matrix Deploy server answers on ``port``."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=0.5) as resp:
            return json.load(resp).get("app") == "matrix-deploy"
    except Exception:  # noqa: BLE001
        return False


def _running_instance(preferred: int) -> Optional[int]:
    """Port of an already-running Matrix Deploy near ``preferred`` (the same
    range ``_find_open_port`` uses), so a second double-click reuses it even
    if something else took the preferred port."""
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((HOST, port)) != 0:
                continue
        if _is_matrix_deploy(port):
            return port
    return None


def _app_browser() -> Optional[str]:
    """Chrome, else Edge (always present on Windows 10/11)."""
    bases = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    for rel in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
        for base in filter(None, bases):
            candidate = Path(base) / rel
            if candidate.is_file():
                return str(candidate)
    return shutil.which("chrome") or shutil.which("msedge")


def _open_window(url: str) -> None:
    """Open the UI as a standalone app window (normal browser profile, so
    links like Jenkins/NMS open with the user's own logins)."""
    browser = _app_browser()
    if browser:
        try:
            subprocess.Popen([browser, f"--app={url}", "--window-size=1440,920"], close_fds=True)
            return
        except OSError:
            pass
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Matrix Deploy.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Preferred port (default: 8420)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the app window")
    parser.add_argument("--check", action="store_true", help="Run the Setup Check, print it and exit (1 on problems)")
    parser.add_argument("--keep-running", action="store_true",
                        help="Don't quit when the last window closes (default when not packaged)")
    args = parser.parse_args()
    auto_exit = FROZEN and not args.keep_running

    try:
        import uvicorn

        from matrix_deploy.config import AppConfig, config_dir
        from matrix_deploy.preflight import format_report, run_preflight, summarize
        from matrix_deploy.web.server import create_app
    except ImportError as exc:
        _alert(f"Missing dependency: {exc}. Run: pip install -r requirements.txt")
        sys.exit(1)

    config, config_error = None, None
    try:
        config = AppConfig.load()
    except FileNotFoundError:
        config_error = f"No site profile found in {config_dir()}."
    except (KeyError, TypeError, ValueError) as exc:
        config_error = f"Site profile is invalid ({exc.__class__.__name__}: {exc})."

    checks = run_preflight(config, config_error, network_timeout=2.0)
    report = f"Matrix Deploy - Setup Check\n{'=' * 60}\n{format_report(checks)}\n{'=' * 60}"
    ok = summarize(checks)["ok"]
    if args.check:
        if FROZEN and not HAS_CONSOLE:
            _alert(report, error=not ok)
        else:
            print(report)
        sys.exit(0 if ok else 1)
    print(report)
    if config is None:
        _alert("Matrix Deploy can't start:\n\n" + format_report(checks))
        sys.exit(1)

    running = _running_instance(args.port)
    if running:
        url = f"http://{HOST}:{running}"
        print(f"Matrix Deploy is already running at {url}")
        if not args.no_browser:
            _open_window(url)
        return

    port = _find_open_port(HOST, args.port)
    url = f"http://{HOST}:{port}"
    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning" if FROZEN else "info"))

    if auto_exit:
        def watchdog() -> None:
            while not server.should_exit:
                time.sleep(2)
                idle = app.state.idle_seconds()
                if idle is not None and idle >= IDLE_EXIT_AFTER:
                    print("No open windows and no running jobs - quitting.")
                    server.should_exit = True

        threading.Thread(target=watchdog, daemon=True).start()

    if not args.no_browser:
        threading.Timer(1.0, lambda: _open_window(url)).start()

    print(f"\nMatrix Deploy is running at {url}")
    print("Close the app window to quit." if auto_exit else "Press Ctrl+C to stop.")
    server.run()


if __name__ == "__main__":
    main()
