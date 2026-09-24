#!/usr/bin/env python3
"""Entry point for the Matrix Deploy localhost web UI.

Starts a local-only (127.0.0.1) web server and opens the default browser to
it - no separate install step beyond ``pip install -r requirements.txt``.
Never bind this to 0.0.0.0: the server holds SSH/sudo/Artifactory secrets in
memory once entered in the browser, and is designed for a single local user.

This is also the entry point of the distributable ``MatrixDeploy.exe`` (see
``build_exe.ps1``); a Setup Check report is printed on every launch.

Usage:
    python run_server.py [--port 8420] [--no-browser] [--check]
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import urllib.request
import webbrowser

HOST = "127.0.0.1"
DEFAULT_PORT = 8420
FROZEN = getattr(sys, "frozen", False)


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


def _already_running(port: int) -> bool:
    """True if a Matrix Deploy server already answers on ``port``."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=1) as resp:
            return json.load(resp).get("app") == "matrix-deploy"
    except Exception:  # noqa: BLE001
        return False


def _exit(code: int) -> None:
    """Exit, keeping the console window open when launched by double-click so
    the user can actually read what went wrong."""
    if FROZEN and code:
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Matrix Deploy web UI.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Preferred port (default: 8420)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open a browser tab")
    parser.add_argument("--check", action="store_true", help="Run the Setup Check, print it and exit (1 on problems)")
    args = parser.parse_args()

    try:
        import uvicorn

        from matrix_deploy.config import AppConfig, config_dir
        from matrix_deploy.preflight import format_report, run_preflight, summarize
        from matrix_deploy.web.server import create_app
    except ImportError as exc:
        print(f"Missing dependency: {exc}. Run: pip install -r requirements.txt", file=sys.stderr)
        _exit(1)

    print("Matrix Deploy - Setup Check")
    print("=" * 60)
    config, config_error = None, None
    try:
        config = AppConfig.load()
    except FileNotFoundError:
        config_error = f"No site profile found in {config_dir()}."
    except (KeyError, TypeError, ValueError) as exc:
        config_error = f"Site profile is invalid ({exc.__class__.__name__}: {exc})."

    checks = run_preflight(config, config_error, network_timeout=2.0)
    print(format_report(checks))
    print("=" * 60)
    if args.check:
        _exit(0 if summarize(checks)["ok"] else 1)
    if config is None:
        _exit(1)

    if _already_running(args.port):
        url = f"http://{HOST}:{args.port}"
        print(f"Matrix Deploy is already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    port = _find_open_port(HOST, args.port)
    url = f"http://{HOST}:{port}"
    app = create_app(config)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"\nMatrix Deploy is running at {url}")
    print("Keep this window open while you use it. Close it (or press Ctrl+C) to stop.\n")
    uvicorn.run(app, host=HOST, port=port, log_level="warning" if FROZEN else "info")


if __name__ == "__main__":
    main()
