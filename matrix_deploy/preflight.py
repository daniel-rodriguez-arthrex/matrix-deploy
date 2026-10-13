"""Startup readiness checks ("Setup Check") for Matrix Deploy (Qt-free).

Answers "is this machine set up to use the tool?" with a list of checks.
Each has a status (``ok``/``warn``/``error``), a plain-English detail and a
concrete fix. Used by ``run_server.py --check``, by the console summary
printed at startup, and by the web UI's Setup Check card (``/api/preflight``).
``error`` means core features won't work; ``warn`` means an optional feature
(Download Latest, Jenkins, Build from source) won't work.
"""

from __future__ import annotations

import shutil
import socket
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import GOLDEN_NMS_CONFIGS, NMS_USER_CONFIG_TEMPLATE, AppConfig, app_dir, config_dir, list_profiles
from .env_settings import load_merged_env, switch_passwords_saved
from .webapp_builder import folder_problem

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

# Values copied verbatim from the committed examples - a profile/.env still
# holding these was never filled in.
PLACEHOLDERS = {"10.0.0.1", "your-ssh-user", "your.email@example.com", "Your Full Name"}


@dataclass
class Check:
    id: str
    label: str
    status: str  # "ok" | "warn" | "error"
    detail: str
    fix: str = ""
    # UI hint for a one-click fix button: "creds" (lab passwords),
    # "creds-shared" (Artifactory/Jenkins section of the same dialog),
    # "creds-router" (router & switch section), "folders" (Settings > Local
    # folders) or "network" (run the check again).
    action: str = ""


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-test-{uuid.uuid4().hex}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _running_from_zip(folder: Path) -> bool:
    """Windows Explorer runs an .exe opened straight from a .zip out of a temp
    extraction folder that is wiped later - saved credentials would vanish."""
    s = str(folder).lower()
    temp = str(Path(tempfile.gettempdir())).lower()
    return s.startswith(temp) and (".zip" in s or "temp1_" in s or "rar$" in s)


def _tcp_reachable(host: str, port: int, timeout: float) -> Optional[str]:
    """Return ``None`` if a TCP connection succeeds, else the error text."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return str(exc) or exc.__class__.__name__


def run_preflight(
    config: Optional[AppConfig],
    config_error: Optional[str] = None,
    network: bool = True,
    network_timeout: float = 3.0,
) -> List[Check]:
    checks: List[Check] = []
    add = lambda *a, **k: checks.append(Check(*a, **k))  # noqa: E731
    folder = app_dir()
    cfg_dir = config_dir()

    # --- Install location -------------------------------------------------
    if getattr(sys, "frozen", False) and _running_from_zip(folder):
        add("location", "App folder", "error",
            f"Running from inside a .zip ({folder}). Anything you save will be lost.",
            "Close this, right-click the .zip > Extract All, then run MatrixDeploy.exe from the extracted folder.")
    elif not _writable(cfg_dir) or not _writable(folder):
        add("location", "App folder", "error",
            f"Can't write to {cfg_dir}, so saved passwords can't be stored.",
            "Move the MatrixDeploy folder somewhere you own, e.g. Documents or Desktop (not Program Files).")
    else:
        add("location", "App folder", "ok", str(folder))

    # --- Bundled app files ------------------------------------------------
    missing = [p for p in [STATIC_DIR / "index.html", NMS_USER_CONFIG_TEMPLATE, *GOLDEN_NMS_CONFIGS.values()]
               if not p.exists()]
    if missing:
        add("bundle", "App files", "error",
            "Missing: " + ", ".join(p.name for p in missing),
            "The folder is incomplete. Re-extract the full MatrixDeploy .zip.")
    else:
        add("bundle", "App files", "ok", "All UI and golden files present.")

    # --- Site profiles ----------------------------------------------------
    profiles = list_profiles()
    if not profiles:
        add("profiles", "Site profiles", "warn" if config is not None and config.path is None else "error",
            f"No site profile (.json with 'connection' and 'rooms') found in {cfg_dir}.",
            "Import your site's facility planner zip under Settings > Facility config.")
    else:
        add("profiles", "Site profiles", "ok", ", ".join(p.name for p in profiles))

    if config is None:
        add("profile", "Active site", "error",
            config_error or "No site profile could be loaded.",
            "Fix or replace the profile .json in the config folder, then restart.")
        return checks
    if config.path is None:
        # AppConfig.standalone(): no lab, only Health Check > Device works.
        add("profile", "Active site", "warn",
            "No facility configured: only Health Check (Device) works.",
            "Import a facility planner zip under Settings > Facility config to manage a lab.")
        return checks

    site = config.site_name or Path(config.path or "").stem
    conn = config.connection
    problems = []
    if not config.rooms:
        problems.append("no rooms defined")
    if not conn.router_ip or conn.router_ip in PLACEHOLDERS:
        problems.append(f"router_ip is '{conn.router_ip}'")
    if not conn.ssh_username or conn.ssh_username in PLACEHOLDERS:
        problems.append(f"ssh_username is '{conn.ssh_username}'")
    if problems:
        add("profile", f"Site '{site}'", "error",
            "Profile looks unfinished: " + "; ".join(problems) + ".",
            f"Edit {config.path} with the real lab values, then restart.")
    else:
        add("profile", f"Site '{site}'", "ok",
            f"{len(config.rooms)} rooms via {conn.router_ip}, SSH user '{conn.ssh_username}'.")

    # --- Credentials ------------------------------------------------------
    env, secrets = load_merged_env(config.path)
    if not secrets.get("ssh_password"):
        add("ssh_password", "Lab SSH password", "error",
            f"Not set for '{site}'. Nothing that talks to a room will work.",
            "Click 'Enter passwords' (saved on this computer only).", "creds")
    else:
        add("ssh_password", "Lab SSH password", "ok", "Saved.")
    if not secrets.get("sudo_password"):
        add("sudo_password", "Lab sudo password", "warn",
            "Not set - the SSH password will be tried for sudo.",
            "Click 'Enter passwords' if this lab uses a different sudo password.", "creds")
    else:
        add("sudo_password", "Lab sudo password", "ok", "Saved.")
    # Router + switches: the Network tab, Fiber Test and Fix router config log
    # in to them, and the switches are only reachable through the router.
    # Neither is in the facility planner zip, so a new lab never has them.
    if not secrets.get("router_admin_password"):
        add("router_password", "Router password", "error",
            f"Not set for '{site}'. Network, Fiber Test and Fix router config log in to the router "
            "(and reach the switches through it), so they won't work.",
            "Click 'Enter router & switch passwords'. They aren't in the facility planner zip - "
            "ask whoever set up the lab's router.", "creds-router")
    else:
        add("router_password", "Router password", "ok", "Saved.")
    switch_count = len((config.facility or {}).get("switches") or [])
    if switch_count:
        if not switch_passwords_saved(config.path, switch_count):
            add("switch_password", "Switch password", "error",
                f"Not set for '{site}' ({switch_count} switch{'es' if switch_count != 1 else ''}). "
                "Network (Check switches) and Fiber Test can't read the switches without it.",
                "Click 'Enter router & switch passwords'. Not in the facility planner zip either.", "creds-router")
        else:
            add("switch_password", "Switch password", "ok", "Saved.")

    def _pair(check_id, label, user_key, secret_key, needed_for):
        user, token = env.get(user_key, ""), secrets.get(secret_key, "")
        if user in PLACEHOLDERS:
            user = ""
        if user and token:
            add(check_id, label, "ok", f"Set for {user}.")
        else:
            gaps = [n for n, v in (("username/email", user), ("token", token)) if not v]
            add(check_id, label, "warn",
                f"Missing {' and '.join(gaps)} - needed only for {needed_for}.",
                "Click 'Enter my Artifactory/Jenkins details' and use YOUR OWN account.", "creds-shared")

    _pair("artifactory", "Artifactory", "artifactory_email", "artifactory_token", "Download Latest")
    _pair("jenkins", "Jenkins", "jenkins_username", "jenkins_token", "Trigger Jenkins Build")

    # --- Optional local tools ---------------------------------------------
    if env.get("backend_repo") or env.get("web_repo"):
        tools = [t for t in ("git", "npm") if not (shutil.which(t) or shutil.which(f"{t}.cmd"))]
        if tools:
            add("build_tools", "Build from source", "warn",
                f"{' and '.join(tools)} not found on PATH.",
                "Install Git / Node.js, or deploy pre-built dist folders instead.")
        else:
            add("build_tools", "Build from source", "ok", "git and npm found.")

    # --- Saved local folders ----------------------------------------------
    # Only folders the user saved; unset ones just use defaults / get typed in.
    labels = {"swu_file": "Default SWU file", "backend_repo": "Backend repo", "web_repo": "Web app repo",
              "webapp_dist": "Backend dist folder", "webapp_web": "Web assets folder"}
    missing = [f"{label} ({env[f]})" for f, label in labels.items()
               if env.get(f) and not Path(env[f]).expanduser().exists()]
    dl = env.get("swu_download_dir")
    if dl and Path(dl).expanduser().exists() and not Path(dl).expanduser().is_dir():
        missing.append(f"SWU download folder ({dl}) is a file")
    saved = [f for f in (*labels, "swu_download_dir") if env.get(f)]
    missing += [p for f in ("backend_repo", "web_repo", "webapp_dist", "webapp_web")
                if env.get(f) and (p := folder_problem(f, Path(env[f])))]
    if missing:
        add("folders", "Local folders", "warn",
            "Saved folder problems: " + "; ".join(missing),
            "Click 'Set my folders' and point them at folders on this computer (or clear them).", "folders")
    elif saved:
        add("folders", "Local folders", "ok", f"{len(saved)} saved folder(s) found.")

    # --- Network ----------------------------------------------------------
    # Everything (rooms, NMS, switches) is reached through the lab router's
    # WAN address, so a router that isn't on the network blocks the whole tool.
    if network and conn.router_ip and conn.router_ip not in PLACEHOLDERS:
        room = config.rooms[0] if config.rooms else None
        room_port = room.ssh_port(conn.ssh_port_base) if room else None
        with ThreadPoolExecutor(max_workers=2) as pool:
            router_job = pool.submit(_tcp_reachable, conn.router_ip, 22, network_timeout)
            room_job = pool.submit(_tcp_reachable, conn.router_ip, room_port, network_timeout) if room else None
            router_err, room_err = router_job.result(), room_job.result() if room_job else "no rooms"
        where = f"Settings > Connection shows {conn.router_ip}; it comes from {Path(config.path or '').name or 'the site profile'}"
        if router_err and room_err:
            add("network", "Lab router", "error",
                f"The lab router at {conn.router_ip} isn't answering ({router_err}). Every room, NMS and switch "
                "is reached through it, so nothing in this tool will work until it's on the network.",
                "1) Make sure this computer is on the Arthrex network or VPN. 2) Check the router is powered on and "
                f"its WAN cable is plugged in. 3) Check the router IP is right ({where}). Then click 'Check again'.",
                "network")
        elif router_err:
            add("network", "Lab router", "warn",
                f"{conn.router_ip} forwards to the rooms, but the router's own SSH (port 22) isn't answering "
                f"({router_err}). Network, Fiber Test and Fix router config won't work.",
                "Check SSH is enabled on the router's WAN interface.", "network")
        elif room_err:
            add("network", "Lab router", "warn",
                f"Router {conn.router_ip} is on the network, but {room.name} (port {room_port}) isn't answering: {room_err}",
                "Check that room is powered on and its port forward is on the router (Network tab).", "network")
        else:
            add("network", "Lab router", "ok",
                f"Router {conn.router_ip} is on the network ({room.name} answered on port {room_port}).")

    return checks


def summarize(checks: List[Check]) -> Dict[str, Any]:
    errors = sum(c.status == "error" for c in checks)
    warnings = sum(c.status == "warn" for c in checks)
    return {
        "ok": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "checks": [asdict(c) for c in checks],
    }


def format_report(checks: List[Check]) -> str:
    icon = {"ok": "[ OK ]", "warn": "[WARN]", "error": "[FAIL]"}
    lines = []
    for c in checks:
        lines.append(f"  {icon[c.status]} {c.label}: {c.detail}")
        if c.status != "ok" and c.fix:
            lines.append(f"         -> {c.fix}")
    s = summarize(checks)
    lines.append("")
    if s["errors"]:
        lines.append(f"  {s['errors']} problem(s) must be fixed before the tool will work.")
    elif s["warnings"]:
        lines.append(f"  Ready. {s['warnings']} optional item(s) not set up.")
    else:
        lines.append("  All checks passed.")
    return "\n".join(lines)
