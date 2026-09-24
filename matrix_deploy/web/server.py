"""FastAPI app for the Matrix Deploy localhost web UI.

Deliberately Qt-free: reuses ``AppConfig``/``Deployer``/``ArtifactoryClient``/
``JenkinsClient`` exactly as ``matrix_deploy/workers.py`` does for the PyQt5
GUI, but replaces ``QThread``/``pyqtSignal`` with a plain ``threading.Thread``
per job that pushes events onto a ``queue.Queue``, drained by a WebSocket
handler. Intended to run on ``127.0.0.1`` only for a single local user - see
``run_server.py``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import shlex
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Union

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..artifactory import ArtifactoryClient, ArtifactoryCredentials, ArtifactoryError
from ..config import AppConfig, Room, list_profiles
from ..deployer import DeploymentCredentials, DeploymentRequest, Deployer
from ..env_settings import (
    active_env_paths,
    default_env_path,
    load_merged_env,
    profile_env_path,
    save_env_values,
)
from ..preflight import run_preflight, summarize
from ..jenkins import JENKINS_JOB, JenkinsClient, JenkinsCredentials, JenkinsError
from ..ssh_client import SSHError, SSHTarget, connect
from .. import webapp_builder
from .tunnel import (
    TunnelManager,
    build_inspector_url,
    capture_cdp_screenshot,
    make_devtools_proxy,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
ARTIFACTS_DIR = Path(tempfile.gettempdir()) / "matrix_deploy_web_artifacts"
# Default folder for downloaded SWU builds (mirrors the desktop app's CACHE_DIR).
DEFAULT_SWU_CACHE_DIR = Path.home() / "Desktop" / "latest-matrix-wrynose"

# Mirrors workers.MAX_CONCURRENT_SWU_UPLOADS: rooms share one physical
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
    "matrix_api_certs",
    "fix_room_config_race",
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
    "reset_web_app",
    "diag_web_app",
}

# "Fetch" actions save an artifact file AND stream content to the console.
FETCH_ACTIONS = {"get_logs", "get_full_journal", "export_bundle"}


class Job:
    """One running (or finished) job: its background thread and event queue."""

    def __init__(self, job_id: str, rooms: List[Room]):
        self.id = job_id
        self.rooms = rooms
        self.events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def emit(self, event: Dict[str, Any]) -> None:
        self.events.put(event)

    def finish(self) -> None:
        self.events.put({"type": "all_done"})
        self.events.put(None)  # sentinel for the websocket reader


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

        def run() -> None:
            conn = config.connection
            target = SSHTarget(
                host=conn.router_ip,
                port=room.ssh_port(conn.ssh_port_base),
                username=conn.ssh_username,
                password=creds.ssh_password,
            )
            try:
                client = connect(target)
            except SSHError as exc:
                job.emit({"type": "log", "message": str(exc), "level": "error"})
                job.finish()
                return
            channel = None
            try:
                prefix = ""
                if creds.sudo_password:
                    prefix = f"echo {shlex.quote(creds.sudo_password)} | sudo -S -p '' "
                cmd = f"{prefix}journalctl -fu {shlex.quote(service)} -n 50 --no-pager".strip()
                channel = client.get_transport().open_session()
                channel.get_pty()
                channel.settimeout(1.0)
                channel.exec_command(cmd)
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
                        job.emit({"type": "log", "message": line.rstrip("\r"), "level": "detail"})
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
    if action == "matrix_api_certs":
        return deployer.deploy_matrix_api_certs(room)
    if action == "fix_room_config_race":
        return deployer.fix_room_config_race(room)
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


class DownloadLatestRequest(BaseModel):
    artifactory_email: str
    artifactory_token: str
    cache_dir: Optional[str] = None
    branch: Optional[str] = None


class TriggerBuildRequest(BaseModel):
    jenkins_username: str
    jenkins_token: str


class SaveCredentialsRequest(BaseModel):
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None
    artifactory_email: Optional[str] = None
    artifactory_token: Optional[str] = None
    jenkins_username: Optional[str] = None
    jenkins_token: Optional[str] = None


class ProfileSelectRequest(BaseModel):
    path: str


class MatrixAppInspectRequest(BaseModel):
    room_number: int
    ssh_password: Optional[str] = None


class NmsPasswordRequest(BaseModel):
    room_number: int
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


class StreamLogsRequest(BaseModel):
    room_number: int
    service: str  # "matrix" | "nms" | an explicit systemd unit name
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


def create_app(config: Optional[AppConfig] = None) -> FastAPI:
    """Build the FastAPI app. ``config`` defaults to ``AppConfig.load()``
    (the same config file the PyQt5 GUI reads)."""
    app_config = config or AppConfig.load()
    jobs = JobManager()
    tunnels = TunnelManager()
    app = FastAPI(title="Matrix Deploy")

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
                "demonstrator_url": r.demonstrator_gui_url(router_ip),
                "web_app_url": r.web_app_url(router_ip),
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
        base = Path(path) if path else DEFAULT_SWU_CACHE_DIR
        try:
            base = base.resolve()
        except Exception:  # noqa: BLE001
            base = DEFAULT_SWU_CACHE_DIR
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
        fields the same way the desktop GUI does. Localhost-only by design."""
        # Merge the shared root .env (Artifactory/Jenkins etc.) with the
        # active profile's sibling .env (per-lab SSH/sudo creds), letting the
        # profile override shared values where they overlap.
        _, env_path = active_env_paths(app_config.path)
        env, secrets = load_merged_env(app_config.path)
        conn = app_config.connection
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
                "swu_file": env.get("swu_file", ""),
                "backend_repo": env.get("backend_repo", ""),
                "web_repo": env.get("web_repo", ""),
            },
            "secrets": {
                "ssh_password": secrets.get("ssh_password", ""),
                "sudo_password": secrets.get("sudo_password", ""),
                "artifactory_token": secrets.get("artifactory_token", ""),
                "jenkins_token": secrets.get("jenkins_token", ""),
            },
            "env_path": str(env_path),
            "lab_env_path": str(_lab_env_path()),
            "env_present": bool(env or secrets),
            "defaults": {
                "swu_download_dir": str(DEFAULT_SWU_CACHE_DIR),
            },
        }

    @app.post("/api/setup/save")
    def save_credentials(req: SaveCredentialsRequest) -> Dict[str, Any]:
        """First-run / edit credentials: lab SSH+sudo go to the active site's
        own env file (labs have different passwords); Artifactory/Jenkins go
        to the shared root .env. Blank fields leave saved values alone."""
        lab_path, shared_path = _lab_env_path(), default_env_path()
        try:
            save_env_values(lab_path, {"ssh_password": req.ssh_password, "sudo_password": req.sudo_password})
            save_env_values(shared_path, {
                "artifactory_email": req.artifactory_email,
                "artifactory_token": req.artifactory_token,
                "jenkins_username": req.jenkins_username,
                "jenkins_token": req.jenkins_token,
            })
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not save credentials: {exc}")
        return {"lab_env_path": str(lab_path), "shared_env_path": str(shared_path)}

    @app.get("/api/health")
    def health() -> Dict[str, str]:
        """Lets a second launch of run_server.py detect this instance."""
        return {"app": "matrix-deploy"}

    @app.get("/api/preflight")
    def preflight(network: bool = True) -> Dict[str, Any]:
        """Setup Check: is this machine ready to use the tool?"""
        return summarize(run_preflight(app_config, network=network))

    @app.post("/api/jobs/deploy")
    def start_deploy(req: DeployRequest) -> Dict[str, str]:
        rooms = _rooms_by_numbers(req.room_numbers)
        if not req.swu_file:
            raise HTTPException(status_code=400, detail="An SWU file path is required")
        creds = DeploymentCredentials(ssh_password=req.ssh_password, sudo_password=req.sudo_password)
        job = jobs.start_deploy(
            app_config, rooms, creds,
            swu_file=Path(req.swu_file),
            sequential=req.sequential,
            max_concurrency=req.max_concurrency,
        )
        return {"job_id": job.id}

    @app.post("/api/jobs/webapp-deploy")
    def start_webapp_deploy(req: WebAppDeployRequest) -> Dict[str, str]:
        rooms = [] if req.build_only else _rooms_by_numbers(req.room_numbers)
        creds = DeploymentCredentials(ssh_password=req.ssh_password, sudo_password=req.sudo_password)
        job = jobs.start_webapp_deploy(app_config, rooms, creds, req)
        return {"job_id": job.id}

    @app.post("/api/jobs/system-action")
    def start_system_action(req: SystemActionRequest) -> Dict[str, str]:
        rooms = _rooms_by_numbers(req.room_numbers)
        creds = DeploymentCredentials(ssh_password=req.ssh_password, sudo_password=req.sudo_password)
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
        the UI can copy it to the clipboard when opening the demonstrator,
        mirroring the desktop app's 'Open GUI' behavior."""
        room = _one_room(req.room_number)
        creds = DeploymentCredentials(ssh_password=req.ssh_password, sudo_password=req.sudo_password)
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

    @app.post("/api/jobs/stream-logs")
    def start_stream_logs(req: StreamLogsRequest) -> Dict[str, str]:
        room = _one_room(req.room_number)
        conn = app_config.connection
        service = {"matrix": conn.service_name, "nms": conn.nms_service_name}.get(req.service, req.service)
        creds = DeploymentCredentials(ssh_password=req.ssh_password, sudo_password=req.sudo_password)
        job = jobs.start_log_stream(app_config, creds, room, service)
        return {"job_id": job.id}

    @app.post("/api/config/load")
    def config_load(req: ConfigLoadRequest) -> Dict[str, Any]:
        """Blocking (threadpool) load of a room's live config for the editor."""
        room = _one_room(req.room_number)
        creds = DeploymentCredentials(ssh_password=req.ssh_password, sudo_password=req.sudo_password)
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
        room = _one_room(req.room_number)
        creds = DeploymentCredentials(ssh_password=req.ssh_password, sudo_password=req.sudo_password)
        job = jobs.start_config_deploy(app_config, room, creds, req.content)
        return {"job_id": job.id}

    @app.post("/api/config/apply-changes")
    def config_apply_changes(req: ConfigPatchRequest) -> Dict[str, str]:
        """Apply field-level edits to each selected room's own config (not a
        whole-file copy, so room-specific values survive)."""
        rooms = _rooms_by_numbers(req.room_numbers)
        creds = DeploymentCredentials(ssh_password=req.ssh_password, sudo_password=req.sudo_password)
        job = jobs.start_config_patch(
            app_config, rooms, creds, [c.model_dump() for c in req.changes],
            sequential=req.sequential, max_concurrency=req.max_concurrency,
        )
        return {"job_id": job.id}

    @app.get("/api/artifactory/branches")
    def artifactory_branches() -> List[Dict[str, str]]:
        """Selectable build sources (e.g. wrynose vs MatrixG2-2.0) for the
        download pop-up."""
        return [
            {"label": b.label, "build_path": b.build_path, "branch_filter": b.branch_filter}
            for b in app_config.artifactory.available_branches()
        ]

    @app.post("/api/jobs/download-latest")
    def start_download_latest(req: DownloadLatestRequest) -> Dict[str, str]:
        creds = ArtifactoryCredentials(username=req.artifactory_email, token=req.artifactory_token)
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
        if req.cache_dir:
            cache_dir = Path(req.cache_dir)
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
        creds = JenkinsCredentials(username=req.jenkins_username, token=req.jenkins_token)
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

    @app.post("/api/matrix-app/inspect")
    def matrix_app_inspect(req: MatrixAppInspectRequest) -> Dict[str, Any]:
        """Tunnel to the room's Matrix App Chrome DevTools port and return an
        inspector URL (served by the CDP endpoint itself), mirroring the
        matrix-lab extension's approach."""
        import requests

        room = _one_room(req.room_number)
        creds = DeploymentCredentials(ssh_password=req.ssh_password)
        port = app_config.connection.matrix_app_debug_port
        try:
            t = tunnels.open_port(app_config, creds, room, port, f"Matrix App OR {room.number}", scheme="http")
        except SSHError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to open tunnel: {exc}")

        try:
            resp = requests.get(f"http://127.0.0.1:{t.local_port}/json/list", timeout=6)
            resp.raise_for_status()
            targets = resp.json()
        except Exception as exc:  # noqa: BLE001
            tunnels.close(t.id)
            raise HTTPException(
                status_code=502,
                detail=f"Matrix App DevTools not reachable on port {port} - is the app running with "
                       f"remote debugging enabled? ({exc})",
            )

        title = ""
        for x in targets:
            if isinstance(x, dict) and x.get("type") == "page":
                title = x.get("title", "")
                break
        # Serve the inspector HTML/assets straight through the raw tunnel
        # (transparent TCP handles HTTP keep-alive fine), and route only the
        # WebSocket through a proxy that strips the Origin header - which is
        # what Chromium requires to accept a tunneled DevTools connection.
        proxy_server, proxy_port = make_devtools_proxy("127.0.0.1", t.local_port)
        inspector = build_inspector_url(t.local_port, proxy_port, targets)
        if not inspector:
            try:
                proxy_server.shutdown(); proxy_server.server_close()
            except Exception:  # noqa: BLE001
                pass
            tunnels.close(t.id)
            raise HTTPException(status_code=502, detail="No Matrix App page/target with a debugger URL was found.")
        tunnels.attach_proxy(t.id, proxy_server, inspector)
        return {
            "url": inspector, "tunnel_id": t.id, "local_port": proxy_port,
            "title": title, "room_number": room.number,
        }

    @app.post("/api/matrix-app/screenshot")
    def matrix_app_screenshot(req: MatrixAppInspectRequest) -> Dict[str, Any]:
        """Capture a PNG of the room's Matrix App via Chrome DevTools
        (``Page.captureScreenshot``) over a short-lived tunnel and save it as
        a downloadable artifact. Needs remote debugging enabled."""
        import requests

        room = _one_room(req.room_number)
        creds = DeploymentCredentials(ssh_password=req.ssh_password)
        port = app_config.connection.matrix_app_debug_port
        try:
            t = tunnels.open_port(app_config, creds, room, port, f"Matrix App screenshot OR {room.number}")
        except SSHError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to open tunnel: {exc}")
        try:
            try:
                resp = requests.get(f"http://127.0.0.1:{t.local_port}/json/list", timeout=6)
                resp.raise_for_status()
                targets = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=502,
                    detail=f"Matrix App DevTools not reachable on port {port} - click Enable Remote "
                           f"Debugging first. ({exc})",
                )
            try:
                png = capture_cdp_screenshot(t.local_port, targets)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"Screenshot failed: {exc}")
        finally:
            tunnels.close(t.id)

        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"or{room.number}-matrix-app-{time.strftime('%Y%m%d-%H%M%S')}.png"
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

    @app.middleware("http")
    async def revalidate_ui_assets(request, call_next):
        # Make the browser re-check the UI files on every load (cheap 304 via
        # ETag when unchanged) so an update never pairs new HTML with a stale
        # cached app.js.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
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
