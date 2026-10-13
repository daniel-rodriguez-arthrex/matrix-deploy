"""FastAPI app for the Matrix Deploy localhost web UI.

Wraps ``AppConfig``/``Deployer``/``ArtifactoryClient``/``JenkinsClient`` in a
plain ``threading.Thread`` per job that pushes events onto a ``queue.Queue``,
drained by a WebSocket handler. Intended to run on ``127.0.0.1`` only for a single local user - see
``run_server.py``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import ipaddress
import json as json_mod
import queue
import re
import shlex
import socket
import tempfile
import threading
import time
import uuid
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union

import paramiko
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..artifactory import ArtifactoryClient, ArtifactoryCredentials, ArtifactoryError
from ..config import AppConfig, Room, app_dir, config_dir, list_profiles
from .. import facility as facility_mod
from ..deployer import DeploymentCredentials, DeploymentRequest, Deployer
from ..env_settings import (
    PATH_FIELDS,
    active_env_paths,
    default_env_path,
    load_merged_env,
    parse_env_file,
    profile_env_path,
    protect_env_files,
    save_env_values,
    switch_passwords_saved,
)
from ..health_check import collect_device_health
from ..lab_status import collect_room_sfp, collect_room_status
from .. import qa_lock, router_changes, router_policy, switch_status
from ..preflight import run_preflight, summarize
from ..jenkins import JENKINS_JOB, JenkinsClient, JenkinsCredentials, JenkinsError
from ..ssh_client import KNOWN_HOSTS, SSHError, SSHTarget, connect, redact, run_cisco_config_commands, run_cisco_show, sudo_prefix
from ..ssh_client import exec_command as ssh_exec_command
from .. import webapp_builder
from .tunnel import (
    TunnelManager,
    build_inspector_url,
    capture_cdp_screenshot,
    cdp_call,
    first_page,
    largest_page_first,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
ARTIFACTS_DIR = Path(tempfile.gettempdir()) / "matrix_deploy_web_artifacts"
# Logs/journals/support bundles can hold secrets; they're only a download cache.
ARTIFACT_MAX_AGE = 7 * 24 * 3600
# Room configs older builds staged in %TEMP% (API keys, passwords in plaintext).
_LEGACY_TEMP_CONFIG_RE = re.compile(r"or\d+-(edited|loglevel|patched|trustedendpoints|webappconfig)\.json")


def cleanup_temp_files() -> None:
    """Delete expired artifacts and the plaintext room configs that earlier
    versions left in %TEMP%."""
    now = time.time()
    for p in ARTIFACTS_DIR.glob("*") if ARTIFACTS_DIR.is_dir() else []:
        try:
            if p.is_file() and now - p.stat().st_mtime > ARTIFACT_MAX_AGE:
                p.unlink()
        except OSError:
            pass
    for p in Path(tempfile.gettempdir()).glob("or*-*.json"):
        if _LEGACY_TEMP_CONFIG_RE.fullmatch(p.name):
            try:
                p.unlink()
            except OSError:
                pass
# Default folder for downloaded SWU builds.
DEFAULT_SWU_CACHE_DIR = Path.home() / "Desktop" / "latest-matrix-wrynose"

# Rooms share one physical
# uplink through the router, so cap simultaneous SWU uploads regardless of
# overall room concurrency.
MAX_CONCURRENT_SWU_UPLOADS = 1

# Actions that take no extra parameters and simply return bool.
SIMPLE_ACTIONS = {
    "restart_service",
    "restart_nms_service",
    "stop_service",
    "stop_nms_service",
    "matrix_api_status",
    "nms_status",
    "reboot",
    "shutdown",
    "remove_overlay",
    "get_nms_password",
    "set_log_debug",
    "configure_web_app",
    "check_disk_space",
    "check_uptime",
    "check_specs",
    "check_system_errors",
    "remove_fingerprint",
    "run_command",
    "nms_bandwidth",
    "nms_link_bandwidth",
    "add_trusted_endpoint",
    "enable_matrix_app_debug",
    "disable_matrix_app_debug",
    "enable_admin_app_debug",
    "disable_admin_app_debug",
    "wake_display",
    "reset_web_app",
    "diag_web_app",
}

# Room actions behind the QA lock (Tunnels and Web App tabs, and Actions >
# NMS Configuration); see qa_lock.py.
QA_ACTIONS = {
    "enable_matrix_app_debug", "disable_matrix_app_debug", "enable_admin_app_debug", "disable_admin_app_debug",
    "wake_display", "configure_web_app", "reset_web_app", "diag_web_app",
    "nms_bandwidth", "nms_link_bandwidth", "remove_overlay",
    # Arbitrary shell commands (optionally as root) on the rooms.
    "run_command",
    # Not get_nms_password: every team gets room NMS logins (sidebar NMS
    # button, /api/room/nms-password, Facility check).
}

# "Fetch" actions save an artifact file AND stream content to the console.
FETCH_ACTIONS = {"get_logs", "get_full_journal", "export_bundle"}

_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; font-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


class LocalOnlyGuard:
    """Only serve the app's own pages. Binding to 127.0.0.1 isn't enough: any
    website the user visits can send requests to localhost (CSRF) or point
    its own hostname at 127.0.0.1 (DNS rebinding) and then read ``/api/setup``
    or start jobs. So: the Host must be 127.0.0.1/localhost, and anything that
    changes state (non-GET, WebSocket) must come from this origin. Browsers
    always send Origin on those; non-browser local tools send none.

    With an ``api_token`` (run_server.py makes a new one per launch), the API
    also needs the session cookie that ``/?t=<token>`` sets, so other programs
    or Windows users on this computer can't use it either."""

    def __init__(self, app: Any, api_token: Optional[str] = None) -> None:
        self.app = app
        self.api_token = api_token

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] in ("http", "websocket"):
            problem = self._problem(scope)
            if problem:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008, "reason": problem})
                else:
                    from starlette.responses import PlainTextResponse
                    await PlainTextResponse(problem, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _problem(self, scope: Dict[str, Any]) -> Optional[str]:
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        if host.rsplit(":", 1)[0].lower() not in _LOCAL_HOSTNAMES:
            return "Refused: unexpected Host header."
        origin = headers.get("origin")
        unsafe = scope["type"] == "websocket" or scope.get("method") not in _SAFE_METHODS
        if unsafe and origin is not None and origin.lower() != f"http://{host}".lower():
            return "Refused: cross-origin request."
        # The file:// splash page polls /api/health; nothing else is used cross-site.
        path = scope.get("path", "")
        if headers.get("sec-fetch-site") == "cross-site" and path.startswith("/api/") and path != "/api/health":
            return "Refused: cross-site request."
        if self.api_token and path.startswith(("/api/", "/ws/")) and path != "/api/health":
            cookies = SimpleCookie()
            try:
                cookies.load(headers.get("cookie", ""))
            except CookieError:
                pass
            got = cookies.get(session_cookie_name(host))
            if not got or not hmac.compare_digest(got.value, self.api_token):
                return "Refused: not signed in. Open Matrix Facility Manager from MatrixDeploy.exe (or run_server.py)."
        return None


_SIGN_IN_PAGE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Matrix Facility Manager</title></head>"
    "<body style='font-family:sans-serif;max-width:36em;margin:4em auto'><h2>Matrix Facility Manager</h2>"
    "<p>This window isn't signed in to the running app. Open it again from <b>MatrixDeploy.exe</b> "
    "(or <code>python run_server.py</code>); a second launch reuses the running app.</p></body></html>"
)


def _signed_in_page() -> str:
    """The loading screen (splash.html) without its script, moving on to "/"
    by meta refresh, so signing in looks like the app is still starting."""
    html = (STATIC_DIR / "splash.html").read_text(encoding="utf-8")
    html = re.sub(r"(?s)<script>.*?</script>", "", html)
    # Served at "/", so the splash's file-relative icon links need /static/.
    html = html.replace('href="app-icon', 'href="/static/app-icon')
    return html.replace("<head>", "<head>\n<meta http-equiv=\"refresh\" content=\"0;url=/\" />", 1)


_SIGNED_IN_PAGE = _signed_in_page()


def session_cookie_name(host: str) -> str:
    """Per port: cookies are shared by every port on 127.0.0.1."""
    port = host.rsplit(":", 1)[1] if ":" in host else "80"
    return f"md_session_{port}"


class Job:
    """One running (or finished) job: its background thread and event queue."""

    def __init__(self, job_id: str, rooms: List[Room]):
        self.id = job_id
        self.rooms = rooms
        self.events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        # Real work (deploys, actions, downloads) keeps the app running after
        # its window closes until it finishes; endless live log tails don't.
        self.keeps_alive = True

    def emit(self, event: Dict[str, Any]) -> None:
        self.events.put(event)

    def finish(self) -> None:
        self.events.put({"type": "all_done"})
        self.events.put(None)  # sentinel for the websocket reader


class Presence:
    """Which app windows/tabs are open, via heartbeats. Lets the packaged app
    quit when its window closes (see ``run_server.py``).

    A tab says ``bye`` on close (fast path). Heartbeats from minimized or
    background windows can be throttled to ~1/minute, and a crashed tab never
    says bye, so a silent client only expires after ``CLIENT_TIMEOUT``.
    """

    CLIENT_TIMEOUT = 15 * 60
    STARTUP_GRACE = 120  # how long to wait for the first window to connect

    def __init__(self) -> None:
        self._clients: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._seen_client = False
        self._idle_since: Optional[float] = None

    def beat(self, client_id: str) -> None:
        with self._lock:
            self._clients[client_id] = time.monotonic()
            self._seen_client = True

    def bye(self, client_id: str) -> None:
        with self._lock:
            self._clients.pop(client_id, None)

    def idle_seconds(self, active_jobs: int) -> Optional[float]:
        """Seconds since the app last had an open window and no running
        jobs, or ``None`` while it's in use."""
        now = time.monotonic()
        with self._lock:
            self._clients = {c: t for c, t in self._clients.items() if now - t < self.CLIENT_TIMEOUT}
            waiting_for_first = not self._seen_client and now - self._started < self.STARTUP_GRACE
            in_use = bool(self._clients) or active_jobs > 0 or waiting_for_first
        if in_use:
            self._idle_since = None
            return None
        if self._idle_since is None:
            self._idle_since = now
        return now - self._idle_since


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def _register(self, rooms: List[Room]) -> Job:
        job = Job(str(uuid.uuid4()), rooms)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job id")
        return job

    def cancel(self, job_id: str) -> None:
        self.get(job_id).cancel_event.set()

    def active_count(self) -> int:
        """Jobs still doing real work (see ``Job.keeps_alive``)."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.keeps_alive and j.thread and j.thread.is_alive())

    def cancel_all(self) -> None:
        with self._lock:
            for j in self._jobs.values():
                j.cancel_event.set()

    def _make_deployer(self, job: Job, config: AppConfig, creds: DeploymentCredentials, room: Room) -> Deployer:
        return Deployer(
            config,
            creds,
            log=lambda m, lvl: job.emit({"type": "log", "message": m, "level": lvl}),
            progress=lambda sent, total: job.emit(
                {"type": "progress", "room": room.number, "sent": sent, "total": total}
            ),
            is_cancelled=job.cancel_event.is_set,
            milestone=lambda done, total: job.emit(
                {"type": "milestone", "room": room.number, "done": done, "total": total}
            ),
        )

    # -- deploy (SWU only) --------------------------------------------

    def start_deploy(
        self,
        config: AppConfig,
        rooms: List[Room],
        creds: DeploymentCredentials,
        swu_file: Optional[Path],
        sequential: bool,
        max_concurrency: Optional[int],
    ) -> Job:
        job = self._register(rooms)
        swu_semaphore = threading.Semaphore(MAX_CONCURRENT_SWU_UPLOADS)

        def run_room(room: Room) -> None:
            if job.cancel_event.is_set():
                job.emit({"type": "room_status", "room": room.number, "status": "cancelled"})
                return
            deployer = self._make_deployer(job, config, creds, room)
            deployer.swu_upload_semaphore = swu_semaphore
            job.emit({"type": "room_status", "room": room.number, "status": "running"})
            request = DeploymentRequest(room=room, do_swu=True, swu_file=swu_file)
            try:
                ok = deployer.deploy(request)
            except Exception as exc:  # noqa: BLE001
                job.emit({"type": "log", "message": f"Unexpected error on OR {room.number}: {exc}", "level": "error"})
                ok = False
            job.emit({"type": "room_status", "room": room.number, "status": "success" if ok else "failed"})
            job.emit({"type": "room_done", "room": room.number, "ok": ok})

        self._spawn_fanout(job, rooms, run_room, sequential or config.connection.same_physical_host, max_concurrency)
        return job

    # -- system actions -----------------------------------------------

    def start_system_action(
        self,
        config: AppConfig,
        rooms: List[Room],
        creds: DeploymentCredentials,
        action: str,
        params: Dict[str, Any],
        sequential: bool,
        max_concurrency: Optional[int],
    ) -> Job:
        if action not in SIMPLE_ACTIONS and action not in FETCH_ACTIONS:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
        job = self._register(rooms)
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

        def run_room(room: Room) -> None:
            if job.cancel_event.is_set():
                job.emit({"type": "room_status", "room": room.number, "status": "cancelled"})
                return
            deployer = self._make_deployer(job, config, creds, room)
            job.emit({"type": "room_status", "room": room.number, "status": "running"})
            try:
                ok = _dispatch_action(deployer, room, action, params, job)
            except Exception as exc:  # noqa: BLE001
                job.emit({"type": "log", "message": f"Unexpected error on OR {room.number}: {exc}", "level": "error"})
                ok = False
            job.emit({"type": "room_status", "room": room.number, "status": "success" if ok else "failed"})
            job.emit({"type": "room_done", "room": room.number, "ok": ok})

        # remove_fingerprint mutates a single local file; force sequential.
        force_seq = sequential or config.connection.same_physical_host or action == "remove_fingerprint"
        self._spawn_fanout(job, rooms, run_room, force_seq, max_concurrency)
        return job

    # -- config editor deploy -----------------------------------------

    def start_config_deploy(
        self, config: AppConfig, room: Room, creds: DeploymentCredentials, content: str
    ) -> Job:
        job = self._register([room])

        def run() -> None:
            deployer = self._make_deployer(job, config, creds, room)
            job.emit({"type": "room_status", "room": room.number, "status": "running"})
            try:
                ok = deployer.deploy_matrix_config_text(room, content)
            except Exception as exc:  # noqa: BLE001
                job.emit({"type": "log", "message": f"Unexpected error on OR {room.number}: {exc}", "level": "error"})
                ok = False
            job.emit({"type": "room_status", "room": room.number, "status": "success" if ok else "failed"})
            job.emit({"type": "room_done", "room": room.number, "ok": ok})
            job.finish()

        job.thread = threading.Thread(target=run, daemon=True)
        job.thread.start()
        return job

    # -- Router NAT (Cisco config, not an OR room) ---------------------

    def start_router_apply(
        self,
        router_ip: str,
        admin_username: str,
        admin_password: str,
        enable_password: Optional[str],
        commands: List[str],
        changes_key: Optional[str] = None,
    ) -> Job:
        job = self._register([])
        show_nat = ["show running-config | include ip nat inside source static"]

        def session(fn):
            """One SSH connection per CLI session: Cisco IOS ends the whole
            connection when a shell closes, so a second shell on the same
            client fails (paramiko EOFError)."""
            # Routers in other labs may run older Cisco IOS (SHA-1 only).
            client = connect(SSHTarget(host=router_ip, port=22, username=admin_username, password=admin_password), try_keys=False, sha1="fallback")
            try:
                return fn(client)
            except (EOFError, OSError, paramiko.SSHException) as exc:
                raise SSHError(f"the router closed the SSH session ({type(exc).__name__}{f': {exc}' if str(exc) else ''})") from exc
            finally:
                client.close()

        def read_nat() -> str:
            return session(lambda c: run_cisco_show(c, show_nat, enable_password=enable_password))

        def run() -> None:
            log = lambda m, lvl: job.emit({"type": "log", "message": m, "level": lvl})  # noqa: E731
            before = None
            try:
                log(f"Connecting to router {router_ip}:22 as {admin_username}...", "info")
                # The rules before/after are compared so the app knows which
                # ones it added or removed (Fix router config > Undo).
                if changes_key is not None:
                    try:
                        before = read_nat()
                    except SSHError as exc:
                        if "auth failed" in str(exc) or "No answer" in str(exc) or "Refused" in str(exc):
                            raise
                        log(f"Couldn't read the NAT rules first ({exc}); this change won't be undoable from the app.", "warning")
                log(f"Applying {len(commands)} line(s) in config mode...", "info")
                session(lambda c: run_cisco_config_commands(c, commands, enable_password=enable_password, log=log))
                job.emit({"type": "log", "message": "Router NAT config applied and saved (write memory).", "level": "success"})
            except SSHError as exc:
                log(f"Failed to apply router config: {exc}", "error")
            except Exception as exc:  # noqa: BLE001
                log(f"Unexpected error applying router config: {type(exc).__name__}: {exc}", "error")
            finally:
                # Also after a failure part-way: record whatever did change.
                try:
                    if before is not None:
                        entry = router_changes.record(changes_key, before, read_nat(), commands)
                        job.emit({"type": "router_changes", "added": len(entry["added"]), "removed": len(entry["removed"])})
                        if entry["added"] or entry["removed"]:
                            log(f"This app has now added {len(entry['added'])} and removed {len(entry['removed'])} NAT rule(s) on this router. "
                                "Network > Fix router config > 'Undo this app's changes' puts them back.", "info")
                except Exception as exc:  # noqa: BLE001
                    log(f"Couldn't record the change for undo: {exc}", "warning")
                finally:
                    job.finish()

        job.thread = threading.Thread(target=run, daemon=True)
        job.thread.start()
        return job

    def start_config_patch(
        self,
        config: AppConfig,
        rooms: List[Room],
        creds: DeploymentCredentials,
        changes: List[Dict[str, Any]],
        sequential: bool,
        max_concurrency: Optional[int],
    ) -> Job:
        job = self._register(rooms)

        def run_room(room: Room) -> None:
            if job.cancel_event.is_set():
                job.emit({"type": "room_status", "room": room.number, "status": "cancelled"})
                return
            deployer = self._make_deployer(job, config, creds, room)
            job.emit({"type": "room_status", "room": room.number, "status": "running"})
            try:
                ok = deployer.patch_matrix_config(room, changes)
            except Exception as exc:  # noqa: BLE001
                job.emit({"type": "log", "message": f"Unexpected error on OR {room.number}: {exc}", "level": "error"})
                ok = False
            job.emit({"type": "room_status", "room": room.number, "status": "success" if ok else "failed"})
            job.emit({"type": "room_done", "room": room.number, "ok": ok})

        self._spawn_fanout(job, rooms, run_room, sequential or config.connection.same_physical_host, max_concurrency)
        return job

    # -- Web app (Matrix Electron web app + matrix.api backend) -------

    def start_webapp_deploy(
        self,
        config: AppConfig,
        rooms: List[Room],
        creds: DeploymentCredentials,
        req: "WebAppDeployRequest",
    ) -> Job:
        job = self._register(rooms if not req.build_only else [])

        def run_room(room: Room, local_dist: Path, local_web: Path) -> None:
            if job.cancel_event.is_set():
                job.emit({"type": "room_status", "room": room.number, "status": "cancelled"})
                return
            deployer = self._make_deployer(job, config, creds, room)
            job.emit({"type": "room_status", "room": room.number, "status": "running"})
            try:
                ok = deployer.deploy_web_app(room, local_dist, local_web)
            except Exception as exc:  # noqa: BLE001
                job.emit({"type": "log", "message": f"Unexpected error on OR {room.number}: {exc}", "level": "error"})
                ok = False
            job.emit({"type": "room_status", "room": room.number, "status": "success" if ok else "failed"})
            job.emit({"type": "room_done", "room": room.number, "ok": ok})

        def run() -> None:
            _log = lambda m, lvl: job.emit({"type": "log", "message": m, "level": lvl})  # noqa: E731
            try:
                local_dist = Path(req.local_dist) if req.local_dist else None
                local_web = Path(req.local_web) if req.local_web else None

                if req.do_build:
                    if not req.backend_repo or not req.web_repo:
                        _log("Backend repo and web repo paths are required to build from source.", "error")
                        return
                    backend_repo, web_repo = Path(req.backend_repo), Path(req.web_repo)
                    _log("=== Building backend (matrix-api) from source ===", "info")
                    if not webapp_builder.build_repo(backend_repo, _log, job.cancel_event.is_set):
                        return
                    _log("=== Building web app from source ===", "info")
                    if not webapp_builder.build_repo(web_repo, _log, job.cancel_event.is_set):
                        return
                    local_dist = local_dist or backend_repo / webapp_builder.DEFAULT_BACKEND_DIST_SUBPATH
                    local_web = local_web or web_repo / webapp_builder.DEFAULT_WEB_DIST_SUBPATH

                if not local_dist or not local_web:
                    _log("Backend dist and web assets paths are required (build from source or set them directly).", "error")
                    return

                resolved_web = webapp_builder.resolve_web_assets(local_web)
                if resolved_web != local_web:
                    _log(f"Web assets folder has no index.html; using {resolved_web} instead.", "warning")
                    local_web = resolved_web
                problems = [p for p in (webapp_builder.folder_problem("webapp_dist", local_dist),
                                        webapp_builder.folder_problem("webapp_web", local_web)) if p]
                if problems:
                    for problem in problems:
                        _log(problem, "error")
                    return

                webapp_builder.check_version_compatibility(local_dist, local_web, _log)

                if req.build_only:
                    _log("Build only - skipping deploy.", "success")
                    return

                force_seq = req.sequential or config.connection.same_physical_host
                if force_seq or len(rooms) <= 1:
                    for room in rooms:
                        run_room(room, local_dist, local_web)
                else:
                    workers = len(rooms)
                    if req.max_concurrency:
                        workers = max(1, min(req.max_concurrency, len(rooms)))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                        list(executor.map(lambda r: run_room(r, local_dist, local_web), rooms))
            finally:
                job.finish()

        job.thread = threading.Thread(target=run, daemon=True)
        job.thread.start()
        return job

    # -- Artifactory / Jenkins ----------------------------------------

    def start_download_latest(
        self,
        config: AppConfig,
        creds: ArtifactoryCredentials,
        cache_dir: Path,
        build_path: Optional[str] = None,
        branch_filter: Optional[str] = None,
    ) -> Job:
        job = self._register([])

        def run() -> None:
            client = ArtifactoryClient(config.artifactory, creds)
            try:
                dest = client.download_latest(
                    cache_dir,
                    log=lambda m, lvl: job.emit({"type": "log", "message": m, "level": lvl}),
                    progress=lambda sent, total: job.emit({"type": "progress", "sent": sent, "total": total}),
                    is_cancelled=job.cancel_event.is_set,
                    build_path=build_path,
                    branch_filter=branch_filter,
                )
                job.emit({"type": "log", "message": f"Saved to {dest}", "level": "success"})
                # Let the UI auto-fill the SWU file path with what we just got.
                job.emit({"type": "swu_downloaded", "path": str(dest)})
            except ArtifactoryError as exc:
                job.emit({"type": "log", "message": str(exc), "level": "error"})
            except Exception as exc:  # noqa: BLE001
                job.emit({"type": "log", "message": f"Unexpected download error: {exc}", "level": "error"})
            job.finish()

        job.thread = threading.Thread(target=run, daemon=True)
        job.thread.start()
        return job

    def start_trigger_build(self, creds: JenkinsCredentials, job_name: str = JENKINS_JOB) -> Job:
        job = self._register([])

        def run() -> None:
            client = JenkinsClient(creds, job_name=job_name)
            try:
                result = client.trigger_build(log=lambda m, lvl: job.emit({"type": "log", "message": m, "level": lvl}))
                if result.build_number is not None:
                    job.emit({"type": "log", "message": f"Build #{result.build_number} started: {result.build_url}", "level": "success"})
                else:
                    job.emit({"type": "log", "message": "Build triggered (number not yet known).", "level": "success"})
            except JenkinsError as exc:
                job.emit({"type": "log", "message": str(exc), "level": "error"})
            except Exception as exc:  # noqa: BLE001
                job.emit({"type": "log", "message": f"Unexpected error triggering build: {exc}", "level": "error"})
            job.finish()

        job.thread = threading.Thread(target=run, daemon=True)
        job.thread.start()
        return job

    def start_log_stream(
        self, config: AppConfig, creds: DeploymentCredentials, room: Room, service: str
    ) -> Job:
        """Follow a systemd service's journal live (``journalctl -f``) and push
        each line as a log event until the job is cancelled."""
        job = self._register([room])
        job.keeps_alive = False  # endless; don't hold the app open for it

        def run() -> None:
            conn = config.connection
            target = SSHTarget(
                host=conn.router_ip,
                port=room.ssh_port(conn.ssh_port_base),
                username=conn.ssh_username,
                password=creds.ssh_password,
                sudo_password=creds.sudo_password,
            )
            try:
                client = connect(target)
            except SSHError as exc:
                job.emit({"type": "log", "message": str(exc), "level": "error"})
                job.finish()
                return
            channel = None
            try:
                prefix = f"{sudo_prefix(creds.sudo_password)} " if creds.sudo_password else ""
                cmd = f"{prefix}journalctl -fu {shlex.quote(service)} -n 50 --no-pager".strip()
                channel = ssh_exec_command(client, cmd, get_pty=True)[1].channel
                channel.settimeout(1.0)
                job.emit({"type": "log", "message": f"--- Watching {service} live (Cancel to stop) ---", "level": "info"})
                buf = ""
                while not job.cancel_event.is_set():
                    try:
                        chunk = channel.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk.decode(errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = redact(client, line.rstrip("\r"))
                        if line.strip() != "<hidden>":  # the PTY's echo of the password line
                            job.emit({"type": "log", "message": line, "level": "detail"})
                job.emit({"type": "log", "message": f"--- Stopped watching {service} ---", "level": "warning"})
            except Exception as exc:  # noqa: BLE001
                job.emit({"type": "log", "message": f"Log stream error: {exc}", "level": "error"})
            finally:
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
                job.finish()

        job.thread = threading.Thread(target=run, daemon=True)
        job.thread.start()
        return job

    # -- shared fanout ------------------------------------------------

    def _spawn_fanout(self, job, rooms, run_room, force_sequential, max_concurrency):
        job.thread = threading.Thread(
            target=self._run_rooms,
            args=(job, rooms, run_room, force_sequential, max_concurrency),
            daemon=True,
        )
        job.thread.start()

    @staticmethod
    def _run_rooms(job, rooms, run_room, force_sequential, max_concurrency) -> None:
        try:
            if force_sequential or len(rooms) <= 1:
                for room in rooms:
                    run_room(room)
            else:
                workers = len(rooms)
                if max_concurrency:
                    workers = max(1, min(max_concurrency, len(rooms)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    list(executor.map(run_room, rooms))
        finally:
            job.finish()


def _dispatch_action(deployer: Deployer, room: Room, action: str, params: Dict[str, Any], job: Job) -> bool:
    """Run a single system/fetch action against ``room``. Returns success."""
    if action == "restart_service":
        return deployer.restart_service(room)
    if action == "restart_nms_service":
        return deployer.restart_nms_service(room)
    if action == "stop_service":
        return deployer.stop_service(room)
    if action == "stop_nms_service":
        return deployer.stop_nms_service(room)
    if action == "matrix_api_status":
        return deployer.matrix_api_status(room)
    if action == "nms_status":
        return deployer.nms_service_status(room)
    if action == "reboot":
        return deployer.reboot(room)
    if action == "shutdown":
        return deployer.shutdown(room)
    if action == "nms_bandwidth":
        return deployer.deploy_golden_nms_config(room, params.get("bandwidth", "MAX"))
    if action == "nms_link_bandwidth":
        return deployer.deploy_nms_link_bandwidth(room, int(params.get("link_bandwidth_kbps", 500000)))
    if action == "remove_overlay":
        return deployer.deploy_nms_remove_overlay(room)
    if action == "get_nms_password":
        return deployer.get_nms_password(room)
    if action == "set_log_debug":
        return deployer.set_log_level(room, "debug")
    if action == "add_trusted_endpoint":
        endpoint = params.get("trusted_endpoint", "").strip()
        if not endpoint:
            deployer.log("No trusted endpoint provided.", "error")
            return False
        return deployer.add_trusted_endpoint(room, endpoint)
    if action == "configure_web_app":
        return deployer.configure_web_app(room)
    if action == "check_disk_space":
        return deployer.check_disk_space(room)
    if action == "check_uptime":
        return deployer.check_uptime(room)
    if action == "check_specs":
        return deployer.check_specs(room)
    if action == "check_system_errors":
        return deployer.check_system_errors(room, int(params.get("since_hours") or 3))
    if action == "remove_fingerprint":
        return deployer.remove_known_hosts_entry(room)
    if action == "enable_matrix_app_debug":
        return deployer.enable_matrix_app_debugging(room)
    if action == "disable_matrix_app_debug":
        return deployer.disable_matrix_app_debugging(room)
    if action in ("enable_admin_app_debug", "disable_admin_app_debug"):
        return deployer.set_admin_app_debugging(room, action == "enable_admin_app_debug")
    if action == "wake_display":
        return deployer.wake_display(room)
    if action == "reset_web_app":
        return deployer.reset_web_app(room)
    if action == "diag_web_app":
        return deployer.diagnose_web_app(room)
    if action == "run_command":
        return deployer.run_custom_command(room, params.get("custom_command", ""), bool(params.get("use_sudo")))
    # Fetch actions produce a downloadable artifact.
    if action in FETCH_ACTIONS:
        since_hours = params.get("since_hours")
        if action == "get_logs":
            path = deployer.fetch_service_logs(room, ARTIFACTS_DIR, since_hours=since_hours)
        elif action == "get_full_journal":
            path = deployer.fetch_full_journal(room, ARTIFACTS_DIR, since_hours=since_hours)
        else:  # export_bundle
            path = deployer.export_support_bundle(room, ARTIFACTS_DIR)
        if path is not None:
            job.emit({"type": "artifact", "room": room.number, "name": path.name, "url": f"/api/artifacts/{path.name}"})
            return True
    return False


# --------------------------------------------------------------------------
# Request/response models
# --------------------------------------------------------------------------


class DeployRequest(BaseModel):
    room_numbers: List[int]
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None
    swu_file: Optional[str] = None
    sequential: bool = True
    max_concurrency: Optional[int] = None


class WebAppDeployRequest(BaseModel):
    room_numbers: List[int]
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None
    do_build: bool = False
    backend_repo: Optional[str] = None
    web_repo: Optional[str] = None
    local_dist: Optional[str] = None
    local_web: Optional[str] = None
    build_only: bool = False
    sequential: bool = True
    max_concurrency: Optional[int] = None


class SystemActionRequest(BaseModel):
    room_numbers: List[int]
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None
    action: str
    custom_command: str = ""
    use_sudo: bool = False
    bandwidth: Optional[str] = None
    link_bandwidth_kbps: Optional[int] = None
    trusted_endpoint: Optional[str] = None
    since_hours: Optional[int] = Field(default=None, gt=0, le=24 * 30)  # log export time window
    sequential: bool = True
    max_concurrency: Optional[int] = None


class ConfigLoadRequest(BaseModel):
    room_number: int
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


class ConfigDeployRequest(BaseModel):
    room_number: int
    content: str
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


class ConfigChange(BaseModel):
    op: Literal["set", "delete", "add_item", "remove_item"]
    path: List[Union[int, str]] = Field(min_length=1)
    value: Any = None


class ConfigPatchRequest(BaseModel):
    room_numbers: List[int]
    changes: List[ConfigChange] = Field(min_length=1)
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None
    sequential: bool = True
    max_concurrency: Optional[int] = None


# Credential fields left blank use the value saved on this computer. Saved
# passwords/tokens are never sent to the browser (see /api/setup "saved").
class DownloadLatestRequest(BaseModel):
    artifactory_email: Optional[str] = None
    artifactory_token: Optional[str] = None
    cache_dir: Optional[str] = None
    branch: Optional[str] = None


class TriggerBuildRequest(BaseModel):
    jenkins_username: Optional[str] = None
    jenkins_token: Optional[str] = None


class SaveCredentialsRequest(BaseModel):
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None
    artifactory_email: Optional[str] = None
    artifactory_token: Optional[str] = None
    jenkins_username: Optional[str] = None
    jenkins_token: Optional[str] = None
    router_admin_username: Optional[str] = None
    router_admin_password: Optional[str] = None
    router_enable_password: Optional[str] = None
    windows_password: Optional[str] = None
    switch_password: Optional[str] = None


class RouterApplyNatRequest(BaseModel):
    router_admin_username: Optional[str] = None
    router_admin_password: Optional[str] = None
    router_enable_password: Optional[str] = None
    # The (possibly hand-edited) command text from the UI. None = the full plan.
    commands: Optional[List[str]] = None


# Apply to Router only sends static NAT rules (or their "no" form to remove
# one); anything else is refused so an edit can't run arbitrary router config.
_NAT_RULE_RE = re.compile(
    r"^(no )?ip nat inside source static (tcp|udp) \d+\.\d+\.\d+\.\d+ \d+ (interface \S+|\d+\.\d+\.\d+\.\d+) \d+( extendable)?$"
)
# Wrapper lines from the copy-paste block; the apply job adds these itself.
_NAT_WRAPPERS = {"configure terminal", "conf t", "end", "write memory", "wr", "exit"}


def validate_nat_commands(lines: List[str]) -> Tuple[List[str], List[str]]:
    """``(commands, rejected)`` from the editor text: blank lines, ``!``
    comments and the configure/end/write wrappers are dropped."""
    commands, rejected = [], []
    for raw in lines:
        line = " ".join(raw.split())
        if not line or line.startswith("!") or line.lower() in _NAT_WRAPPERS:
            continue
        (commands if _NAT_RULE_RE.match(line) else rejected).append(line)
    return commands, rejected


class RouterLiveRequest(BaseModel):
    """Blank fields fall back to the router login saved for this lab."""
    router_admin_username: Optional[str] = None
    router_admin_password: Optional[str] = None
    router_enable_password: Optional[str] = None


class SwitchesLiveRequest(BaseModel):
    # Fiber tab: also read SFP light levels and the port error counters.
    fiber: bool = False


class FacilityImportRequest(BaseModel):
    upload_id: str
    # "new" = add as its own lab, else the path of an existing profile to update.
    target: str = "new"
    qa_port_forwards: bool = True


class SavePathsRequest(BaseModel):
    """Settings > Local folders. ``""`` clears a saved folder, ``None`` leaves it."""
    swu_download_dir: Optional[str] = None
    swu_file: Optional[str] = None
    backend_repo: Optional[str] = None
    web_repo: Optional[str] = None
    webapp_dist: Optional[str] = None
    webapp_web: Optional[str] = None


class ProfileSelectRequest(BaseModel):
    path: str


class TeamRequest(BaseModel):
    team: Literal["qa", "integration"]


class QaUnlockRequest(BaseModel):
    password: str
    remember: bool = False


class QaPasswordRequest(BaseModel):
    password: str


class MatrixAppInspectRequest(BaseModel):
    room_number: int
    ssh_password: Optional[str] = None
    # Needed for the admin app (bringing its login screen up uses sudo).
    sudo_password: Optional[str] = None
    # "matrix" = kiosk Matrix App, "admin" = Matrix Admin App.
    app: Literal["matrix", "admin"] = "matrix"


class NmsPasswordRequest(BaseModel):
    room_number: int
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


class LabStatusRequest(BaseModel):
    room_number: int
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


class HealthCheckRequest(BaseModel):
    """Either a facility room (``room_number``; blank passwords use the
    sidebar/saved lab ones) or any device by address (``host``; its own
    login, never saved). Blank ``sudo_password`` on a device = its SSH one."""
    room_number: Optional[int] = None
    host: Optional[str] = Field(default=None, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=64)
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


# Hostname or IPv4/IPv6 literal; no spaces, options or shell characters.
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$|^[0-9A-Fa-f:]+$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,63}$")


class StreamLogsRequest(BaseModel):
    room_number: int
    service: str  # "matrix" | "nms" | an explicit systemd unit name
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


def create_app(config: Optional[AppConfig] = None, api_token: Optional[str] = None) -> FastAPI:
    """Build the FastAPI app. ``config`` defaults to ``AppConfig.load()``.
    ``api_token``: see ``LocalOnlyGuard`` (``None`` = no sign-in, for tests)."""
    app_config = config or AppConfig.load()
    # Encrypt plaintext saved secrets (hand-edited, or lab passwords shipped
    # in the build) for this Windows user before anything else reads them.
    try:
        protect_env_files([default_env_path(), *config_dir().glob("*.env")])
    except OSError as exc:
        print(f"Warning: couldn't encrypt saved passwords: {exc}")
    cleanup_temp_files()
    jobs = JobManager()
    tunnels = TunnelManager()
    presence = Presence()
    # QA lock (qa_lock.py): unlocked for this app session after the QA password.
    qa_session = {"unlocked": False}

    def qa_status() -> Dict[str, bool]:
        lock = qa_lock.is_configured()
        return {"lock": lock, "unlocked": not lock or qa_session["unlocked"] or qa_lock.remembered()}

    def require_qa() -> None:
        if not qa_status()["unlocked"]:
            raise HTTPException(status_code=403, detail=(
                "This is a QA-only feature. Unlock it with the QA password under Settings > Your team."))

    app = FastAPI(title="Matrix Facility Manager")
    # For run_server.py's auto-quit watchdog.
    app.state.idle_seconds = lambda: presence.idle_seconds(jobs.active_count())

    def _swu_download_dir() -> Path:
        """The user's saved SWU download folder, else the default."""
        saved = load_merged_env(app_config.path)[0].get("swu_download_dir")
        return Path(saved) if saved else DEFAULT_SWU_CACHE_DIR

    def _saved(key: str) -> Optional[str]:
        """A password/token saved on this computer for the active site (root
        .env + the lab's .env), decrypted only here on the server."""
        return load_merged_env(app_config.path)[1].get(key) or None

    def _creds(req: Any) -> DeploymentCredentials:
        """Room SSH/sudo passwords for a request: what was typed in the sidebar,
        else the ones saved for this lab. The page never receives the saved ones."""
        return DeploymentCredentials(ssh_password=req.ssh_password or _saved("ssh_password"),
                                     sudo_password=req.sudo_password or _saved("sudo_password"))

    def _lab_env_path() -> Path:
        """Where the active site's SSH/sudo passwords are saved: its sibling
        ``<stem>.env`` (created on first save), else the root .env."""
        return profile_env_path(app_config.path) if app_config.path else default_env_path()

    def _rooms_by_numbers(numbers: List[int]) -> List[Room]:
        rooms = []
        for n in numbers:
            room = app_config.room(n)
            if room is None:
                raise HTTPException(status_code=400, detail=f"Unknown room number: {n}")
            rooms.append(room)
        if not rooms:
            raise HTTPException(status_code=400, detail="No rooms selected")
        return rooms

    def _one_room(number: int) -> Room:
        room = app_config.room(number)
        if room is None:
            raise HTTPException(status_code=400, detail=f"Unknown room number: {number}")
        return room

    @app.get("/api/rooms")
    def list_rooms() -> List[Dict[str, Any]]:
        router_ip = app_config.connection.router_ip
        return [
            {
                "number": r.number,
                "room_id": r.room_id,
                "name": r.name,
                "secondary": r.secondary,
                "demonstrator_url": r.demonstrator_gui_url(router_ip),
                # Reached through the API forward, which only QA labs have.
                "web_app_url": r.web_app_url(router_ip) if app_config.connection.qa_port_forwards else None,
            }
            for r in app_config.rooms
        ]

    @app.get("/api/connection")
    def connection_info() -> Dict[str, Any]:
        conn = app_config.connection
        return {
            "router_ip": conn.router_ip,
            "ssh_username": conn.ssh_username,
            "ssh_port_base": conn.ssh_port_base,
            "service_name": conn.service_name,
            "nms_service_name": conn.nms_service_name,
            "same_physical_host": conn.same_physical_host,
        }

    def _qa_view() -> bool:
        """QA features show on this computer: team isn't Integration and the
        QA lock (if any) is open. Same rule as ``applyTeam`` in app.js."""
        return load_merged_env(app_config.path)[0].get("team") != "integration" and qa_status()["unlocked"]

    def _router_nat_plan(config: AppConfig, qa: Optional[bool] = None):
        """Return ``(rooms, commands)``: the per-room forwards and the flat
        list of ``ip nat inside source static`` lines for the active router.
        SSH (200+n) and NMS (11000+n) are what this app needs in every lab
        (room SSH, Facility check, Fiber Test); SWU (300+n) and API (10000+n)
        are QA-only, planned only when ``qa_port_forwards`` is on and (unless
        ``qa`` says otherwise) the QA features show on this computer."""
        conn = config.connection
        iface = conn.router_wan_interface
        if qa is None:
            qa = conn.qa_port_forwards and _qa_view()
        rooms = []
        commands = []
        for r in config.rooms:
            kinds = [
                ("ssh", 22, r.ssh_port(conn.ssh_port_base), "facility"),
                *([("swu", conn.swu_service_port, r.swu_external_port(conn.swu_port_base), "qa"),
                   ("api", 443, r.api_external_port(), "qa")] if qa else []),
                ("nms", 8443, r.nms_external_port(conn.nms_port_base), "facility"),
            ]
            forwards = {k: {"inside_port": ip, "outside_port": op, "source": src} for k, ip, op, src in kinds}
            rooms.append({"number": r.number, "name": r.name, "room_id": r.room_id, "forwards": forwards})
            commands.extend(
                f"ip nat inside source static tcp {r.room_id} {f['inside_port']} interface {iface} {f['outside_port']}"
                for f in forwards.values()
            )
        return rooms, commands

    @app.get("/api/router-ports")
    def router_ports() -> Dict[str, Any]:
        """Per-room NAT port-forwarding plan for the active site's router,
        ready-to-paste Cisco config (see ROUTER_PORT_FORWARDING_PATTERN.md),
        and the switch/router layout from an imported facility config."""
        conn = app_config.connection
        rooms, commands = _router_nat_plan(app_config)
        nat_script = "configure terminal\n" + "\n".join(commands) + "\nend\nwrite memory\n"
        fac = app_config.facility or {}
        return {
            "site_name": app_config.site_name,
            "router_ip": conn.router_ip,
            "wan_interface": conn.router_wan_interface,
            # Groups / access rules / NAT policy / routes (not checked yet).
            "policy": _policy_compare(None),
            "qa_port_forwards": conn.qa_port_forwards,
            # False for the Integration team (or QA locked): no SWU/API forwards.
            "qa_forwards_shown": conn.qa_port_forwards and _qa_view(),
            "rooms": rooms,
            "nat_script": nat_script,
            "facility": {
                "site_code": fac.get("site_code"),
                "version": fac.get("version"),
                "hostname": (fac.get("router") or {}).get("hostname"),
                "switches": fac.get("switches") or [],
                # Planned devices per room (Network tab "expected vs seen").
                "rooms": {n: {"extron": meta.get("extron") or []} for n, meta in (fac.get("rooms") or {}).items()},
            } if fac else None,
        }

    def _nat_target_label(ip: str, port: int) -> str:
        """What a NAT rule points at, from the rooms and facility layout."""
        service = {22: "SSH", 443: "HTTPS", 8443: "NMS", 9222: "DevTools (Matrix App)",
                   9223: "DevTools (Admin App)"}.get(port, f"port {port}")
        fac = app_config.facility or {}
        for room in app_config.rooms:
            if room.room_id == ip:
                return f"{room.name} {service}"
        for n, meta in (fac.get("rooms") or {}).items():
            if ip in (meta.get("extron") or []):
                return f"OR {n} Extron {service}"
        for sw in fac.get("switches") or []:
            if any(v.get("ip") == ip for v in sw.get("vlans", [])):
                return f"{sw.get('name') or 'Switch'} {service}"
        for sw in fac.get("switches") or []:
            for v in sw.get("vlans", []):
                if v.get("kind") == "room" and v.get("ip") and v.get("prefix") and \
                        ipaddress.ip_address(ip) in ipaddress.ip_network(f"{v['ip']}/{v['prefix']}", strict=False):
                    return f"OR {v['room']} network device {service}"
        return service

    def _policy_compare(live: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The router's groups / access rules / NAT policy / routes / hardening:
        the planner's router config when the lab has one stored, else the
        standard Matrix router template; compared with ``live`` when given."""
        fac = app_config.facility or {}
        stored = (fac.get("router") or {}).get("policy")
        plan = stored or router_policy.standard_plan(app_config.connection.router_wan_interface)
        uplinks = {v["ip"]: f"{sw.get('name') or 'switch'} uplink" for sw in fac.get("switches") or []
                   for v in sw.get("vlans", []) if v.get("kind") == "uplink" and v.get("ip")}
        rooms = [{"name": r.name, "room_id": r.room_id} for r in app_config.rooms]
        result = router_policy.compare(plan, live, rooms=rooms, uplinks=uplinks)
        result["source"] = ({"kind": "plan", "version": fac.get("version"), "file": fac.get("source_file")} if stored
                            else {"kind": "standard", "facility": bool(fac)})
        return result

    # Live router NAT rules per lab (profile path), from the last router read.
    live_nat_cache: Dict[str, List[Dict[str, Any]]] = {}
    # Parsed groups / rules / routes from the same read (no secrets).
    live_policy_cache: Dict[str, Dict[str, Any]] = {}

    def _read_router_nat(req: Optional[RouterLiveRequest] = None) -> List[Dict[str, Any]]:
        """Read-only: the router's static NAT rules (never enters config mode).
        Reads the whole running-config (same ~1 s as the filtered one) so the
        groups / access rules / routes come from the same read; only the
        parsed, secret-free parts are kept."""
        req = req or RouterLiveRequest()
        env, secrets = load_merged_env(app_config.path)
        # Planner sites use the router's "admin" account.
        user = req.router_admin_username or env.get("router_admin_username") or "admin"
        password = req.router_admin_password or secrets.get("router_admin_password")
        enable = req.router_enable_password or secrets.get("router_enable_password")
        if not password:
            raise HTTPException(status_code=400, detail="No router password saved for this lab. Click 'Enter router & switch passwords' (Settings > Edit saved credentials > Router & switches).")
        try:
            # Routers in other labs may run older Cisco IOS (SHA-1 only).
            client = connect(SSHTarget(host=app_config.connection.router_ip, port=22, username=user, password=password), try_keys=False, sha1="fallback")
        except SSHError as exc:
            raise HTTPException(status_code=502, detail=(
                f"Couldn't log in to the router at {app_config.connection.router_ip}: {exc}. If it isn't answering at all, "
                "the router isn't on the network - see Settings > Setup Check > Lab router."))
        try:
            out = run_cisco_show(client, ["show running-config"], enable_password=enable)
        except SSHError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except (EOFError, OSError, paramiko.SSHException) as exc:
            raise HTTPException(status_code=502, detail=f"The router closed the SSH session while reading its config ({type(exc).__name__}). Try again.")
        finally:
            client.close()
        if "Current configuration" not in out and "hostname" not in out:
            raise HTTPException(status_code=502, detail=(
                "The router didn't show its running-config (the account may need privilege 15 or an enable password: "
                "Settings > Edit saved credentials > Router & switches)."))
        live = [r for line in out.splitlines() if (r := facility_mod.parse_nat_line(line.strip()))]
        live_nat_cache[app_config.path or ""] = live
        live_policy_cache[app_config.path or ""] = router_policy.parse(out)
        return live

    @app.post("/api/router/live-nat")
    def router_live_nat(req: RouterLiveRequest) -> Dict[str, Any]:
        """Read-only: compare the router's real NAT rules with the plan."""
        live = _read_router_nat(req)
        by_port = {r["outside_port"]: r for r in live}
        rooms, _ = _router_nat_plan(app_config)
        status: Dict[str, str] = {}
        conflicts: Dict[str, Dict[str, Any]] = {}
        planned = set()
        for room in rooms:
            for f in room["forwards"].values():
                planned.add(f["outside_port"])
                got = by_port.get(f["outside_port"])
                ok = got and got["inside_ip"] == room["room_id"] and got["inside_port"] == f["inside_port"]
                status[str(f["outside_port"])] = "ok" if ok else ("conflict" if got else "missing")
                if got and not ok:
                    # What the router actually forwards this port to instead.
                    conflicts[str(f["outside_port"])] = {"inside_ip": got["inside_ip"], "inside_port": got["inside_port"],
                                                         "label": _nat_target_label(got["inside_ip"], got["inside_port"])}
        # QA forwards hidden on this computer aren't checked or listed either.
        hidden = {f["outside_port"] for room in _router_nat_plan(app_config, qa=app_config.connection.qa_port_forwards)[0]
                  for f in room["forwards"].values()} - planned
        other = sorted((r for r in live if r["outside_port"] not in planned | hidden), key=lambda r: r["outside_port"])
        for r in other:
            r["label"] = _nat_target_label(r["inside_ip"], r["inside_port"])
        return {"checked_at": time.time(), "rule_count": len(live) - sum(r["outside_port"] in hidden for r in live),
                "status": status, "conflicts": conflicts, "other": other,
                "policy": _policy_compare(live_policy_cache.get(app_config.path or ""))}

    def _router_changes_key() -> str:
        return f"{app_config.path or ''}|{app_config.connection.router_ip}"

    @app.get("/api/router/changes")
    def router_changes_get() -> Dict[str, Any]:
        """NAT rules this app added/removed on the active router (recorded by
        Apply to Router) and the commands that put the router back."""
        entry = router_changes.load(_router_changes_key())
        return {**entry, "commands": router_changes.undo_commands(entry)}

    @app.post("/api/router/changes/forget")
    def router_changes_forget() -> Dict[str, Any]:
        """Drop the record (e.g. the rules were put back by hand)."""
        try:
            router_changes.forget(_router_changes_key())
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Couldn't update the record: {exc}")
        return {"added": [], "removed": [], "commands": []}

    def _device_label(ip: Optional[str]) -> str:
        """Name a device seen on a switch port from what the lab profile knows."""
        if not ip:
            return ""
        fac = app_config.facility or {}
        for room in app_config.rooms:
            if room.room_id == ip:
                return f"{room.name} PC"
            if not room.secondary and room.trusted_endpoint() == ip:
                return f"{room.name} Secondary PC"
        for n, meta in (fac.get("rooms") or {}).items():
            if ip in (meta.get("extron") or []):
                return f"OR {n} Extron"
        if any(i.get("ip") == ip for i in (fac.get("router") or {}).get("inside_interfaces", [])):
            return "Router"
        return ""

    @app.post("/api/switches/live")
    def switches_live(req: Optional[SwitchesLiveRequest] = None) -> Dict[str, Any]:
        """Read-only: link state and the devices (IP / MAC / vendor) on every
        port of every switch in the facility config, plus (``fiber``) SFP
        light levels and error counters. Switches are reached through the
        router's SSH forwards to their VLAN gateway IPs."""
        fiber = bool(req and req.fiber)
        fac = app_config.facility or {}
        switches = fac.get("switches") or []
        if not switches:
            raise HTTPException(status_code=400, detail="Import this lab's facility config first (Settings > Facility config).")
        live = live_nat_cache.get(app_config.path or "") or _read_router_nat()
        env, secrets = load_merged_env(app_config.path)
        raw = parse_env_file(_lab_env_path())
        user = env.get("switch_username") or "admin"

        def one(i: int, sw: Dict[str, Any]) -> Dict[str, Any]:
            ips = {v.get("ip") for v in sw.get("vlans", []) if v.get("ip")}
            fwd = next((r for r in live if r["inside_port"] == 22 and r["inside_ip"] in ips), None)
            base = {"name": sw.get("name"), "number": sw.get("number")}
            if not fwd:
                return {**base, "error": "No SSH forward to this switch on the router."}
            password = raw.get(f"MATRIX_SWITCH_{i}_PASSWORD") or secrets.get("switch_password")
            if not password:
                return {**base, "error": "No switch password saved for this lab. Click 'Enter router & switch passwords' (Settings > Edit saved credentials > Router & switches)."}
            try:
                data = switch_status.read_switch(app_config.connection.router_ip, fwd["outside_port"], user, password, fiber=fiber)
            except SSHError as exc:
                msg = str(exc)
                if "host key changed" in msg:
                    # Remove Fingerprint only covers rooms, so say how to clear a switch's key.
                    msg = (f"Refused: {sw.get('name') or 'this switch'}'s SSH host key changed since this computer last "
                           f"connected (router port {fwd['outside_port']}). If the switch was replaced or reset, delete the "
                           f"[{app_config.connection.router_ip}]:{fwd['outside_port']} line in {KNOWN_HOSTS} and check again. "
                           "Otherwise something may be impersonating it - don't connect, tell the team.")
                return {**base, "error": msg, "ssh_port": fwd["outside_port"]}
            for p in data["ports"].values():
                for d in p["devices"]:
                    d["label"] = _device_label(d["ip"])
            return {**base, "ssh_port": fwd["outside_port"], **data}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(switches)) as pool:
            results = list(pool.map(lambda a: one(*a), enumerate(switches)))
        return {"checked_at": time.time(), "fiber": fiber, "switches": results}

    # Uploaded facility zips waiting for the user to confirm the import. Only
    # the parsed result is kept (in memory), never the zip itself.
    pending_imports: Dict[str, Dict[str, Any]] = {}

    def _import_candidates(parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Existing labs this export could update: the one previously
        imported from the same site code, and any using the same router."""
        fac = parsed["facility"]
        same_site, same_router = None, []
        for p in list_profiles():
            try:
                data = json_mod.loads(Path(p.path).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if (data.get("facility") or {}).get("site_code") == fac["site_code"]:
                same_site = {"path": p.path, "name": p.name, "data": data}
            elif data.get("connection", {}).get("router_ip") == fac["router"]["wan_ip"]:
                same_router.append({"path": p.path, "name": p.name, "data": data})
        return {"same_site": same_site, "same_router": same_router}

    @app.post("/api/facility/preview")
    async def facility_preview(request: Request) -> Dict[str, Any]:
        """Parse an uploaded facility planner zip (raw request body) and
        describe what importing it would do. Nothing is written yet."""
        data = await request.body()
        try:
            parsed = facility_mod.parse_facility_zip(data, request.headers.get("x-filename", ""))
        except facility_mod.FacilityError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        fac = parsed["facility"]
        cands = _import_candidates(parsed)
        upload_id = uuid.uuid4().hex
        pending_imports.clear()
        pending_imports[upload_id] = parsed
        same = cands["same_site"]
        new_path = facility_mod.profile_path_for(fac["site_code"], config_dir())
        return {
            "upload_id": upload_id,
            "site_code": fac["site_code"],
            "facility_name": fac["facility_name"],
            "version": fac["version"],
            "router": {k: fac["router"][k] for k in ("hostname", "wan_ip", "wan_interface")},
            "rooms": [{"number": r["number"], "room_id": r["room_id"], "name": r["name"]} for r in parsed["rooms"]],
            "switches": [{"name": s["name"], "model": s["model"],
                          "rooms": [v["room"] for v in s["vlans"] if v["kind"] == "room"]} for s in fac["switches"]],
            "credentials": sorted(parsed["secrets"]),
            "warnings": parsed["warnings"],
            "new_lab_file": new_path.name,
            "update": {"path": same["path"], "name": same["name"],
                       "version": same["data"]["facility"].get("version"),
                       "changes": facility_mod.diff_summary(same["data"].get("facility"), same["data"].get("rooms", []), parsed),
                       "qa_port_forwards": same["data"].get("connection", {}).get("qa_port_forwards", True)} if same else None,
            "same_router": [{"path": c["path"], "name": c["name"]} for c in cands["same_router"]],
        }

    @app.post("/api/facility/import")
    def facility_import(req: FacilityImportRequest) -> Dict[str, Any]:
        """Write the previewed export as a lab: ``config/<site>.json`` (no
        secrets) + its ``.env`` (passwords), then switch to it."""
        nonlocal app_config
        parsed = pending_imports.get(req.upload_id)
        if not parsed:
            raise HTTPException(status_code=400, detail="Upload expired - choose the zip again.")
        cands = _import_candidates(parsed)
        allowed = {c["path"]: c for c in ([cands["same_site"]] if cands["same_site"] else []) + cands["same_router"]}
        if req.target == "new":
            path, base = facility_mod.profile_path_for(parsed["facility"]["site_code"], config_dir()), None
            if path.exists() and str(path) not in allowed:
                raise HTTPException(status_code=409, detail=f"{path.name} already exists and belongs to another lab.")
            if str(path) in allowed:
                base = allowed[str(path)]["data"]
        elif req.target in allowed:
            path, base = Path(req.target), allowed[req.target]["data"]
        else:
            raise HTTPException(status_code=400, detail="That lab can't be updated from this export.")
        artifactory = json_mod.loads(Path(app_config.path).read_text(encoding="utf-8-sig"))["artifactory"] if app_config.path else {}
        profile = facility_mod.build_profile(parsed, base, artifactory, req.qa_port_forwards)
        sec = parsed["secrets"]
        env_path = profile_env_path(path)
        _, saved = load_merged_env(str(path)) if path.exists() else ({}, {})
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json_mod.dumps(profile, indent=2) + "\n", encoding="utf-8")
            save_env_values(env_path, {
                **{k: sec.get(k) for k in ("windows_password", "nms_password", "extron_password", "site_api_key", "ic_key")},
                # The planner's Windows password is the rooms' SSH/sudo login;
                # don't overwrite one the lab already has saved.
                "ssh_password": None if saved.get("ssh_password") else sec.get("windows_password"),
                "sudo_password": None if saved.get("sudo_password") else sec.get("windows_password"),
            })
            app_config = AppConfig.load(path)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=500, detail=f"Import failed: {exc}")
        pending_imports.pop(req.upload_id, None)
        return {"active": app_config.path, "active_name": app_config.site_name, "env_path": str(env_path)}

    @app.post("/api/router/apply-nat")
    def apply_router_nat(req: RouterApplyNatRequest) -> Dict[str, Any]:
        """Actually SSH into the active site's router and apply the NAT plan:
        enable -> configure terminal -> the ip nat lines -> end -> write
        memory. Runs as a background job so the console streams the live
        transcript (see JobManager.start_router_apply). Applies the UI's
        command text when given (only static NAT lines are accepted)."""
        if req.commands is None:
            _, commands = _router_nat_plan(app_config)
        else:
            commands, rejected = validate_nat_commands(req.commands)
            if rejected:
                raise HTTPException(status_code=400, detail=(
                    "Only 'ip nat inside source static ...' lines (or 'no ip nat ...' to remove one) can be applied. "
                    "Not allowed: " + "; ".join(rejected[:5]) + (" ..." if len(rejected) > 5 else "")))
            if not commands:
                raise HTTPException(status_code=400, detail="No NAT rules to apply - pick at least one rule.")
        # Same login as Check live router (planner sites use "admin").
        user = req.router_admin_username or load_merged_env(app_config.path)[0].get("router_admin_username") or "admin"
        password = req.router_admin_password or _saved("router_admin_password")
        if not password:
            raise HTTPException(status_code=400, detail="Enter the router SSH username and password (or save them under Settings > Edit saved credentials).")
        job = jobs.start_router_apply(
            app_config.connection.router_ip,
            user,
            password,
            req.router_enable_password or _saved("router_enable_password"),
            commands,
            changes_key=_router_changes_key(),
        )
        return {"job_id": job.id}

    @app.get("/api/profiles")
    def list_site_profiles() -> Dict[str, Any]:
        profiles = list_profiles()
        return {
            "active": app_config.path,
            "active_name": app_config.site_name,
            "profiles": [{"path": p.path, "name": p.name} for p in profiles],
        }

    @app.post("/api/profiles/select")
    def select_profile(req: "ProfileSelectRequest") -> Dict[str, Any]:
        nonlocal app_config
        valid = {p.path for p in list_profiles()}
        if req.path not in valid:
            raise HTTPException(status_code=400, detail="Unknown profile")
        try:
            app_config = AppConfig.load(Path(req.path))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to load profile: {exc}")
        return {"active": app_config.path, "active_name": app_config.site_name}

    @app.get("/api/fs/list")
    def fs_list(path: Optional[str] = None) -> Dict[str, Any]:
        """List sub-folders and ``.swu`` files under ``path`` (defaults to the
        SWU download folder) so the web UI can offer a native-feeling file
        browser. Localhost/single-user, so browsing the machine is fine."""
        base = Path(path) if path else _swu_download_dir()
        try:
            base = base.resolve()
        except Exception:  # noqa: BLE001
            base = _swu_download_dir()
        if base.is_file():
            base = base.parent
        if not base.exists():
            base = Path.home()
        entries: List[Dict[str, Any]] = []
        try:
            for e in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                try:
                    if e.is_dir():
                        entries.append({"name": e.name, "path": str(e), "is_dir": True})
                    elif e.suffix.lower() == ".swu":
                        entries.append({"name": e.name, "path": str(e), "is_dir": False, "size": e.stat().st_size})
                except OSError:
                    continue
        except (PermissionError, OSError) as exc:
            raise HTTPException(status_code=400, detail=f"Cannot read {base}: {exc}")
        parent = str(base.parent) if base.parent != base else None
        return {"dir": str(base), "parent": parent, "entries": entries}

    @app.get("/api/setup")
    def setup_info() -> Dict[str, Any]:
        """Connection details plus ``.env`` prefill values (non-secret and,
        for a single local user, secret) so the web UI can prefill setup
        fields. Localhost-only by design."""
        # Merge the shared root .env (Artifactory/Jenkins etc.) with the
        # active profile's sibling .env (per-lab SSH/sudo creds), letting the
        # profile override shared values where they overlap.
        _, env_path = active_env_paths(app_config.path)
        env, secrets = load_merged_env(app_config.path)
        conn = app_config.connection
        switch_count = len((app_config.facility or {}).get("switches") or [])
        return {
            "connection": {
                "router_ip": conn.router_ip,
                "ssh_username": conn.ssh_username,
                "ssh_port_base": conn.ssh_port_base,
                "service_name": conn.service_name,
                "nms_service_name": conn.nms_service_name,
                "swu_service_port": conn.swu_service_port,
                "same_physical_host": conn.same_physical_host,
                "room_count": len(app_config.rooms),
                "site_name": app_config.site_name,
            },
            "prefill": {
                "router_ip": env.get("router_ip", ""),
                "username": env.get("username", ""),
                "artifactory_email": env.get("artifactory_email", ""),
                "jenkins_username": env.get("jenkins_username", ""),
                "router_admin_username": env.get("router_admin_username", ""),
                "team": env.get("team", ""),
                **{f: env.get(f, "") for f in PATH_FIELDS},
            },
            # Only whether each one is saved: the values stay on the server,
            # which fills them in when a request leaves the field blank.
            "saved": {**{k: bool(secrets.get(k)) for k in (
                "ssh_password", "sudo_password", "artifactory_token", "jenkins_token", "router_admin_password",
                "router_enable_password", "windows_password")},
                "switch_password": switch_passwords_saved(app_config.path, switch_count)},
            "facility": {**{k: (app_config.facility or {}).get(k) for k in ("site_code", "facility_name", "version", "source_file")},
                         "switch_count": switch_count}
            if app_config.facility else None,
            "qa": qa_status(),
            "env_path": str(env_path),
            "lab_env_path": str(_lab_env_path()),
            "env_present": bool(env or secrets),
            "defaults": {
                "swu_download_dir": str(_swu_download_dir()),
                "builtin_swu_download_dir": str(DEFAULT_SWU_CACHE_DIR),
            },
        }

    @app.post("/api/setup/save")
    def save_credentials(req: SaveCredentialsRequest) -> Dict[str, Any]:
        """First-run / edit credentials: lab SSH+sudo go to the active site's
        own env file (labs have different passwords); Artifactory/Jenkins go
        to the shared root .env. Blank fields leave saved values alone."""
        lab_path, shared_path = _lab_env_path(), default_env_path()
        try:
            save_env_values(lab_path, {
                "ssh_password": req.ssh_password,
                "sudo_password": req.sudo_password,
                "router_admin_username": req.router_admin_username,
                "router_admin_password": req.router_admin_password,
                "router_enable_password": req.router_enable_password,
                "windows_password": req.windows_password,
                "switch_password": req.switch_password,
            })
            save_env_values(shared_path, {
                "artifactory_email": req.artifactory_email,
                "artifactory_token": req.artifactory_token,
                "jenkins_username": req.jenkins_username,
                "jenkins_token": req.jenkins_token,
            })
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not save credentials: {exc}")
        return {"lab_env_path": str(lab_path), "shared_env_path": str(shared_path)}

    @app.post("/api/setup/team")
    def save_team(req: TeamRequest) -> Dict[str, Any]:
        """Which team this computer's user is on, saved to the root .env (per
        machine, never shipped, so every coworker is asked once). Choosing QA
        needs the QA password when a QA lock is set; choosing Integration
        locks the QA features again on this computer."""
        if req.team == "qa":
            require_qa()
        try:
            save_env_values(default_env_path(), {"team": req.team})
            if req.team == "integration" and qa_lock.is_configured():
                qa_session["unlocked"] = False
                qa_lock.save_token("")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not save team: {exc}")
        return {"team": req.team, "qa": qa_status()}

    @app.post("/api/qa/unlock")
    def qa_unlock(req: QaUnlockRequest) -> Dict[str, Any]:
        """Unlock the QA-only features with the shared QA password (for this
        app session, or remembered on this computer)."""
        if qa_lock.is_configured():
            if not qa_lock.check_password(req.password):  # slows down guessing itself
                raise HTTPException(status_code=403, detail="Wrong QA password.")
            qa_session["unlocked"] = True
            try:
                qa_lock.save_token(qa_lock.remember_token(req.password) if req.remember else "")
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Unlocked, but couldn't remember this computer: {exc}")
        return {"qa": qa_status()}

    @app.post("/api/qa/password")
    def qa_set_password(req: QaPasswordRequest) -> Dict[str, Any]:
        """Set or change the shared QA password (needs QA access already).
        Writes config/qa_lock.cfg (hash only), which ships with the build."""
        require_qa()
        if len(req.password) < qa_lock.MIN_LENGTH:
            raise HTTPException(status_code=400, detail=f"Use at least {qa_lock.MIN_LENGTH} characters.")
        try:
            token = qa_lock.set_password(req.password)
            qa_lock.save_token(token)  # the person who set it stays unlocked here
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not save the QA password: {exc}")
        qa_session["unlocked"] = True
        return {"qa": qa_status()}

    @app.post("/api/qa/remove")
    def qa_remove_password() -> Dict[str, Any]:
        """Remove the QA lock (needs QA access): every team member sees QA tabs again."""
        require_qa()
        try:
            qa_lock.remove_lock()
            qa_lock.save_token("")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not remove the QA password: {exc}")
        return {"qa": qa_status()}

    @app.post("/api/qa/lock")
    def qa_lock_now() -> Dict[str, Any]:
        """Lock the QA features again on this computer (forget "remember")."""
        qa_session["unlocked"] = False
        try:
            qa_lock.save_token("")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not forget this computer: {exc}")
        return {"qa": qa_status()}

    @app.post("/api/setup/save-paths")
    def save_paths(req: SavePathsRequest) -> Dict[str, Any]:
        """Settings > Local folders: save this machine's folders to the root
        .env. Saves even if a folder doesn't exist yet (e.g. repo not cloned
        yet) but reports it so the UI can warn."""
        values = {f: (v.strip().strip('"') if v is not None else None) for f, v in req.model_dump().items()}
        warnings = []
        for field, value in values.items():
            if not value:
                continue
            p = Path(value).expanduser()
            if field == "swu_file" and not p.is_file():
                warnings.append(f"SWU file not found: {value}")
            elif field == "swu_download_dir" and p.exists() and not p.is_dir():
                warnings.append(f"Download folder is a file, not a folder: {value}")
            elif field not in ("swu_file", "swu_download_dir") and not p.is_dir():
                warnings.append(f"Folder not found: {value}")
            elif problem := webapp_builder.folder_problem(field, p):
                warnings.append(problem)
        env_path = default_env_path()
        try:
            save_env_values(env_path, values, allow_clear=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not save folders: {exc}")
        return {"saved_to": str(env_path), "warnings": warnings}

    @app.get("/api/health")
    def health() -> Dict[str, str]:
        """Lets a second launch of run_server.py detect this instance."""
        return {"app": "matrix-deploy"}

    @app.post("/api/presence")
    def presence_beat(id: str) -> Dict[str, int]:
        presence.beat(id)
        return {"active_jobs": jobs.active_count()}

    @app.post("/api/presence/bye")
    def presence_bye(id: str) -> Dict[str, bool]:
        presence.bye(id)
        return {"ok": True}

    @app.get("/api/preflight")
    def preflight(network: bool = True) -> Dict[str, Any]:
        """Setup Check: is this machine ready to use the tool?"""
        return summarize(run_preflight(app_config, network=network))

    @app.post("/api/jobs/deploy")
    def start_deploy(req: DeployRequest) -> Dict[str, str]:
        require_qa()
        rooms = _rooms_by_numbers(req.room_numbers)
        if not req.swu_file:
            raise HTTPException(status_code=400, detail="An SWU file path is required")
        creds = _creds(req)
        job = jobs.start_deploy(
            app_config, rooms, creds,
            swu_file=Path(req.swu_file),
            sequential=req.sequential,
            max_concurrency=req.max_concurrency,
        )
        return {"job_id": job.id}

    @app.post("/api/jobs/webapp-deploy")
    def start_webapp_deploy(req: WebAppDeployRequest) -> Dict[str, str]:
        require_qa()
        rooms = [] if req.build_only else _rooms_by_numbers(req.room_numbers)
        creds = _creds(req)
        job = jobs.start_webapp_deploy(app_config, rooms, creds, req)
        return {"job_id": job.id}

    @app.post("/api/jobs/system-action")
    def start_system_action(req: SystemActionRequest) -> Dict[str, str]:
        if req.action in QA_ACTIONS:
            require_qa()
        rooms = _rooms_by_numbers(req.room_numbers)
        creds = _creds(req)
        params = {
            "custom_command": req.custom_command,
            "use_sudo": req.use_sudo,
            "bandwidth": req.bandwidth,
            "link_bandwidth_kbps": req.link_bandwidth_kbps,
            "trusted_endpoint": req.trusted_endpoint,
            "since_hours": req.since_hours,
        }
        job = jobs.start_system_action(
            app_config, rooms, creds, req.action, params,
            sequential=req.sequential, max_concurrency=req.max_concurrency,
        )
        return {"job_id": job.id}

    @app.post("/api/room/nms-password")
    def nms_password(req: NmsPasswordRequest) -> Dict[str, Any]:
        """Fetch a room's default NMS/admin password (from act-mfg-eeprom) so
        the UI can copy it to the clipboard when opening the demonstrator."""
        room = _one_room(req.room_number)
        creds = _creds(req)
        deployer = Deployer(
            app_config, creds,
            log=lambda m, lvl: None,
            progress=lambda s, t: None,
            is_cancelled=lambda: False,
        )
        pw = deployer.fetch_nms_password(room)
        if not pw:
            raise HTTPException(
                status_code=502,
                detail=f"Could not read NMS password for OR {room.number} "
                       f"(a sudo password is required for act-mfg-eeprom).",
            )
        return {"room_number": room.number, "password": pw}

    @app.post("/api/lab-status/room")
    def lab_status_room(req: LabStatusRequest) -> Dict[str, Any]:
        """Read-only health snapshot of one room (services, NMS devices,
        license). The UI calls this per room in parallel so cards fill in as
        each room answers."""
        room = _one_room(req.room_number)
        creds = _creds(req)
        return collect_room_status(app_config, creds, room)

    @app.post("/api/fiber/room")
    def fiber_room(req: LabStatusRequest) -> Dict[str, Any]:
        """Read-only: the device end of one room's fiber links (each NMS
        device's SFP light levels and receive errors). The Fiber Test calls
        this per room alongside the switch read."""
        room = _one_room(req.room_number)
        creds = _creds(req)
        return collect_room_sfp(app_config, creds, room)

    @app.post("/api/health-check")
    def health_check(req: HealthCheckRequest) -> Dict[str, Any]:
        """Read-only hardware health report (storage/SMART, memory, CPU and
        thermals, network links, update readiness) for one facility room or
        any device by address. A device's typed login is used for this
        request only and never saved."""
        conn = app_config.connection
        services = (conn.service_name, conn.nms_service_name)
        if req.room_number is not None:
            room = _one_room(req.room_number)
            creds = _creds(req)
            if not creds.ssh_password:
                raise HTTPException(status_code=400, detail="Enter the SSH password in the sidebar (or save it for this lab).")
            target = Deployer(app_config, creds, log=lambda m, lvl: None, progress=lambda s, t: None,
                              is_cancelled=lambda: False)._target(room)
            label = f"OR {room.number}" + (f" - {room.name}" if room.name and room.name != f"OR {room.number}" else "")
            return collect_device_health(target, label, services, conn.swu_service_port)
        host = (req.host or "").strip()
        user = (req.username or "").strip()
        if not host or not _HOST_RE.match(host):
            raise HTTPException(status_code=400, detail="Enter the device's IP address or hostname.")
        if not user or not _USER_RE.match(user):
            raise HTTPException(status_code=400, detail="Enter a valid SSH username.")
        if not req.ssh_password:
            raise HTTPException(status_code=400, detail="Enter the device's SSH password.")
        target = SSHTarget(host=host, port=req.port, username=user, password=req.ssh_password,
                           sudo_password=req.sudo_password or req.ssh_password)
        return collect_device_health(target, f"{user}@{host}" + (f":{req.port}" if req.port != 22 else ""),
                                     services, conn.swu_service_port)

    @app.get("/api/tools/health-check.ps1")
    def health_check_script() -> FileResponse:
        """The Windows health check script, to copy onto a device (the
        packaged app ships it in tools\\, a source checkout in tools/health-check/)."""
        for path in (app_dir() / "tools" / "MatrixHealthCheck.ps1", app_dir() / "tools" / "health-check" / "MatrixHealthCheck.ps1"):
            if path.is_file():
                return FileResponse(path, filename="MatrixHealthCheck.ps1", media_type="application/octet-stream")
        raise HTTPException(status_code=404, detail="MatrixHealthCheck.ps1 isn't in this app's tools folder.")

    @app.post("/api/jobs/stream-logs")
    def start_stream_logs(req: StreamLogsRequest) -> Dict[str, str]:
        room = _one_room(req.room_number)
        conn = app_config.connection
        service = {"matrix": conn.service_name, "nms": conn.nms_service_name}.get(req.service, req.service)
        creds = _creds(req)
        job = jobs.start_log_stream(app_config, creds, room, service)
        return {"job_id": job.id}

    @app.post("/api/config/load")
    def config_load(req: ConfigLoadRequest) -> Dict[str, Any]:
        """Blocking (threadpool) load of a room's live config for the editor."""
        require_qa()
        room = _one_room(req.room_number)
        creds = _creds(req)
        deployer = Deployer(
            app_config, creds,
            log=lambda m, lvl: None,
            progress=lambda s, t: None,
            is_cancelled=lambda: False,
        )
        content = deployer.read_matrix_config_text(room)
        if content is None:
            raise HTTPException(status_code=502, detail=f"Could not load config for OR {room.number}")
        return {"room_number": room.number, "content": content}

    @app.post("/api/config/deploy")
    def config_deploy(req: ConfigDeployRequest) -> Dict[str, str]:
        require_qa()
        room = _one_room(req.room_number)
        creds = _creds(req)
        job = jobs.start_config_deploy(app_config, room, creds, req.content)
        return {"job_id": job.id}

    @app.post("/api/config/apply-changes")
    def config_apply_changes(req: ConfigPatchRequest) -> Dict[str, str]:
        """Apply field-level edits to each selected room's own config (not a
        whole-file copy, so room-specific values survive)."""
        require_qa()
        rooms = _rooms_by_numbers(req.room_numbers)
        creds = _creds(req)
        job = jobs.start_config_patch(
            app_config, rooms, creds, [c.model_dump() for c in req.changes],
            sequential=req.sequential, max_concurrency=req.max_concurrency,
        )
        return {"job_id": job.id}

    @app.get("/api/artifactory/branches")
    def artifactory_branches() -> List[Dict[str, str]]:
        """Selectable build sources (e.g. wrynose vs MatrixG2-2.0) for the
        download pop-up."""
        require_qa()
        return [
            {"label": b.label, "build_path": b.build_path, "branch_filter": b.branch_filter}
            for b in app_config.artifactory.available_branches()
        ]

    @app.post("/api/jobs/download-latest")
    def start_download_latest(req: DownloadLatestRequest) -> Dict[str, str]:
        require_qa()
        email = req.artifactory_email or load_merged_env(app_config.path)[0].get("artifactory_email")
        token = req.artifactory_token or _saved("artifactory_token")
        if not email or not token:
            raise HTTPException(status_code=400, detail="Enter your Artifactory email and token (or save them under Settings > Edit saved credentials).")
        creds = ArtifactoryCredentials(username=email, token=token)
        build_path: Optional[str] = None
        branch_filter: Optional[str] = None
        label: Optional[str] = None
        if req.branch:
            match = next(
                (b for b in app_config.artifactory.available_branches() if b.label == req.branch),
                None,
            )
            if match is None:
                raise HTTPException(status_code=400, detail=f"Unknown build branch: {req.branch}")
            build_path, branch_filter, label = match.build_path, match.branch_filter, match.label
        saved_dir = load_merged_env(app_config.path)[0].get("swu_download_dir")
        if req.cache_dir:
            cache_dir = Path(req.cache_dir)
        elif saved_dir:
            cache_dir = Path(saved_dir)
        elif label:
            # Keep each branch's SWUs in their own folder so they don't collide.
            cache_dir = DEFAULT_SWU_CACHE_DIR.with_name(f"latest-matrix-{label}")
        else:
            cache_dir = DEFAULT_SWU_CACHE_DIR
        job = jobs.start_download_latest(
            app_config, creds, cache_dir, build_path=build_path, branch_filter=branch_filter
        )
        return {"job_id": job.id}

    @app.post("/api/jobs/trigger-build")
    def start_trigger_build(req: TriggerBuildRequest) -> Dict[str, str]:
        require_qa()
        user = req.jenkins_username or load_merged_env(app_config.path)[0].get("jenkins_username")
        token = req.jenkins_token or _saved("jenkins_token")
        if not user or not token:
            raise HTTPException(status_code=400, detail="Enter your Jenkins username and token (or save them under Settings > Edit saved credentials).")
        creds = JenkinsCredentials(username=user, token=token)
        job = jobs.start_trigger_build(creds)
        return {"job_id": job.id}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> Dict[str, bool]:
        jobs.cancel(job_id)
        return {"cancelled": True}

    @app.get("/api/artifacts/{name}")
    def download_artifact(name: str) -> FileResponse:
        # Guard against path traversal: only serve plain filenames from ARTIFACTS_DIR.
        safe = Path(name).name
        path = ARTIFACTS_DIR / safe
        if not path.exists() or path.parent != ARTIFACTS_DIR:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(path, filename=safe, media_type="application/octet-stream")

    def _debug_target(which: str):
        conn = app_config.connection
        if which == "admin":
            return conn.matrix_admin_app_debug_port, "Matrix Admin App"
        return conn.matrix_app_debug_port, "Matrix App"

    def _prepared_room_client(room: Room, req: "MatrixAppInspectRequest"):
        """One SSH connection to the room for both preparing the screen and
        the DevTools tunnel (saves a second login per click). Wakes the
        room's display (a sleeping screen renders nothing), and for the admin
        app also puts its login screen on the display (it must be on screen
        to accept DevTools input and render screenshots)."""
        errors: List[str] = []
        creds = _creds(req)
        deployer = Deployer(
            app_config,
            creds,
            log=lambda m, lvl: errors.append(m) if lvl == "error" else None,
            progress=lambda s, t: None,
            is_cancelled=lambda: False,
        )
        try:
            client = connect(deployer._target(room))
        except SSHError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        try:
            if req.app == "admin":
                if not deployer.show_admin_app(room, client):
                    raise HTTPException(status_code=502, detail=" ".join(errors) or "Could not bring up the admin login screen.")
            elif creds.sudo_password:
                deployer.wake_display(room, client)  # best effort; DevTools works without it
        except BaseException:
            client.close()
            raise
        return client

    def _repair_admin_page(base: str, targets: list) -> Optional[list]:
        """If the admin app is stuck on anything but its own file:// page
        (e.g. a mistyped address in the DevTools address bar), reload its
        login page. Returns the fresh target list, or None if nothing was done."""
        page = first_page(targets)
        if not page or str(page.get("url", "")).startswith("file://"):
            return None
        cdp_call(targets, "Page.navigate", {"url": app_config.connection.matrix_admin_app_start_url})
        time.sleep(2)
        return _devtools_targets(base, 5)

    def _devtools_targets(base: str, retries: int) -> list:
        """Fetch the CDP target list through the tunnel proxy at ``base``,
        retrying while a just-started app comes up."""
        import requests

        for attempt in range(retries):
            try:
                resp = requests.get(f"{base}/json/list", timeout=6)
                resp.raise_for_status()
                return resp.json()
            except Exception:  # noqa: BLE001
                if attempt == retries - 1:
                    raise
                time.sleep(1)
        return []

    def _open_devtools_tunnel(room: Room, req: "MatrixAppInspectRequest", label: str, settle: float = 0):
        """Prepare the room's screen (then wait ``settle`` s) and open a
        token-protected DevTools tunnel. Returns ``(tunnel, targets,
        repaired)``; closes the tunnel and raises HTTPException on failure."""
        port, app_name = _debug_target(req.app)
        client = _prepared_room_client(room, req)
        time.sleep(settle)
        try:
            t = tunnels.open_port(room.number, port, label, client)
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise HTTPException(status_code=502, detail=f"Failed to open tunnel: {exc}")
        try:
            targets = _devtools_targets(t.base, 10 if req.app == "admin" else 1)
        except Exception as exc:  # noqa: BLE001
            tunnels.close(t.id)
            # The proxy answers 502 when the room refuses the DevTools port.
            refused = getattr(getattr(exc, "response", None), "status_code", None) == 502
            raise HTTPException(
                status_code=502,
                detail=f"DevTools is off for the {app_name} on OR {room.number} - click Enable DevTools first." if refused
                else f"DevTools for the {app_name} on OR {room.number} didn't answer through the tunnel ({type(exc).__name__}). Try again.",
            )
        targets = largest_page_first(targets)
        repaired = False
        if req.app == "admin":
            try:
                fresh = _repair_admin_page(t.base, targets)
            except Exception:  # noqa: BLE001 - keep going with what we have
                fresh = None
            if fresh:
                targets, repaired = largest_page_first(fresh), True
        return t, targets, repaired

    @app.post("/api/matrix-app/inspect")
    def matrix_app_inspect(req: MatrixAppInspectRequest) -> Dict[str, Any]:
        """Tunnel to the room's Matrix App Chrome DevTools port and return an
        inspector URL (served by the CDP endpoint itself, through the tunnel's
        token-protected proxy), mirroring the matrix-lab extension's approach.
        The room's screen is woken first, and for the admin app its login
        screen is brought up (and reloaded if it was left on a broken page)."""
        require_qa()
        room = _one_room(req.room_number)
        _, app_name = _debug_target(req.app)
        t, targets, repaired = _open_devtools_tunnel(room, req, f"{app_name} OR {room.number}")
        title = ""
        for x in targets:
            if isinstance(x, dict) and x.get("type") == "page":
                title = x.get("title", "")
                break
        inspector = build_inspector_url(t.base, targets)
        if not inspector:
            tunnels.close(t.id)
            raise HTTPException(status_code=502, detail=f"No {app_name} page/target with a debugger URL was found.")
        tunnels.set_url(t.id, inspector)
        return {
            "url": inspector, "tunnel_id": t.id, "local_port": t.local_port,
            "title": title, "room_number": room.number, "repaired": repaired,
        }

    @app.post("/api/matrix-app/screenshot")
    def matrix_app_screenshot(req: MatrixAppInspectRequest) -> Dict[str, Any]:
        """Capture a PNG of the room's Matrix App via Chrome DevTools
        (``Page.captureScreenshot``) over a short-lived tunnel and save it as
        a downloadable artifact. Needs DevTools enabled. The screen is woken
        (and the admin app brought on screen) first, since a sleeping or
        hidden window doesn't render."""
        require_qa()
        room = _one_room(req.room_number)
        _, app_name = _debug_target(req.app)
        t, targets, _ = _open_devtools_tunnel(room, req, f"{app_name} screenshot OR {room.number}", settle=1)
        try:
            png = capture_cdp_screenshot(targets)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Screenshot failed: {exc}")
        finally:
            tunnels.close(t.id)

        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        slug = "matrix-admin-app" if req.app == "admin" else "matrix-app"
        name = f"or{room.number}-{slug}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        (ARTIFACTS_DIR / name).write_bytes(png)
        return {"room_number": room.number, "name": name, "url": f"/api/artifacts/{name}"}

    @app.get("/api/tunnel/list")
    def tunnel_list() -> List[Dict[str, Any]]:
        return [
            {"id": t.id, "room_number": t.room_number, "target_key": t.target_key,
             "label": t.label, "local_port": t.local_port, "url": t.url}
            for t in tunnels.list()
        ]

    @app.post("/api/tunnel/{tunnel_id}/close")
    def tunnel_close(tunnel_id: str) -> Dict[str, bool]:
        return {"closed": tunnels.close(tunnel_id)}

    @app.websocket("/ws/jobs/{job_id}")
    async def job_events(websocket: WebSocket, job_id: str) -> None:
        job = jobs.get(job_id)
        await websocket.accept()
        try:
            while True:
                try:
                    event = await _get_with_timeout(job.events)
                except _QueueEmpty:
                    continue
                if event is None:
                    break
                await websocket.send_json(event)
                if event.get("type") == "all_done":
                    break
        except WebSocketDisconnect:
            pass

    @app.on_event("shutdown")
    def _close_tunnels_on_shutdown() -> None:
        # Gracefully close all SSH tunnels/proxies when the server stops
        # (Ctrl+C). On a hard kill the OS closes the sockets anyway, so
        # nothing lingers locally or on the room either way.
        tunnels.close_all()
        # Stops any live log tails (the only jobs that can still be running
        # when the app auto-quits).
        jobs.cancel_all()

    @app.middleware("http")
    async def revalidate_ui_assets(request, call_next):
        # Make the browser re-check the UI files on every load (cheap 304 via
        # ETag when unchanged) so an update never pairs new HTML with a stale
        # cached app.js.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        else:
            # API replies can carry saved passwords; keep them out of any cache.
            response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index(request: Request, t: Optional[str] = None):
        cookie = session_cookie_name(request.headers.get("host", ""))
        if api_token and t is not None:
            # Sign-in link from run_server.py: swap it for a session cookie
            # and drop the token from the address.
            if not hmac.compare_digest(t.encode(), api_token.encode()):
                return HTMLResponse(_SIGN_IN_PAGE, status_code=401)
            # Not a 303: the sign-in link is opened from the file:// splash, and
            # Chrome doesn't send a SameSite=Strict cookie on a redirect that is
            # part of a cross-site navigation, so "/" would come back signed out.
            # This same-origin page moves on by itself (meta refresh: the CSP
            # blocks inline scripts), and that navigation carries the cookie.
            response = HTMLResponse(_SIGNED_IN_PAGE)
            response.set_cookie(cookie, api_token, httponly=True, samesite="strict", path="/")
            return response
        if api_token and not hmac.compare_digest(request.cookies.get(cookie, ""), api_token):
            return HTMLResponse(_SIGN_IN_PAGE, status_code=401)
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC_DIR / "app-icon.ico", media_type="image/x-icon")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # Outermost, so it also covers WebSockets and error responses.
    app.add_middleware(LocalOnlyGuard, api_token=api_token)
    return app


class _QueueEmpty(Exception):
    pass


async def _get_with_timeout(q: "queue.Queue", timeout: float = 0.25):
    """Await-friendly wrapper around a blocking ``queue.Queue.get`` so the
    websocket handler doesn't tie up the event loop indefinitely."""
    loop = asyncio.get_event_loop()

    def _get():
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise _QueueEmpty()

    return await loop.run_in_executor(None, _get)
