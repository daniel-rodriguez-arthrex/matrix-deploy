"""Qt worker threads wrapping the Qt-free deployment and download logic."""

from __future__ import annotations

import concurrent.futures
import shlex
import socket
import threading
from pathlib import Path
from typing import List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from .artifactory import ArtifactoryClient, ArtifactoryCredentials, ArtifactoryError
from .config import AppConfig, Room
from .deployer import DeploymentCredentials, DeploymentRequest, Deployer
from .jenkins import JENKINS_JOB, JenkinsClient, JenkinsCredentials, JenkinsError
from .ssh_client import SSHError, SSHTarget, connect

# Actions that mutate a single shared *local* resource (e.g. ~/.ssh/known_hosts)
# and therefore must never run concurrently across rooms.
_LOCAL_SERIAL_ACTIONS = {"remove_fingerprint"}


class SystemActionWorker(QThread):
    """Runs a single system action (restart service or reboot) on one or more rooms.

    Rooms are independent physical devices reachable through different router
    ports, so actions can safely run concurrently across rooms. If the config
    marks the rooms as sharing one physical host (``same_physical_host``),
    execution is forced sequential regardless of ``sequential`` to avoid
    stepping on shared resources (e.g. simultaneous reboots).
    """

    log = pyqtSignal(str, str)         # message, level
    room_status = pyqtSignal(int, str) # room_number, status
    room_done = pyqtSignal(int, bool)  # room_number, success
    all_done = pyqtSignal()

    def __init__(
        self,
        config: AppConfig,
        creds: DeploymentCredentials,
        rooms: List[Room],
        action: str,
        bandwidth: Optional[str] = None,
        link_bandwidth_kbps: Optional[int] = None,
        logs_dir: Optional[Path] = None,
        sequential: bool = True,
        max_concurrency: Optional[int] = None,
    ):
        super().__init__()
        self.config = config
        self.creds = creds
        self.rooms = rooms
        self.action = action
        self.bandwidth = bandwidth
        self.link_bandwidth_kbps = link_bandwidth_kbps
        self.logs_dir = logs_dir
        self.sequential = sequential
        # Cap on simultaneously in-flight rooms when not forced sequential.
        # None means "no cap" (all selected rooms at once).
        self.max_concurrency = max_concurrency
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        self.log.emit("Cancellation requested - finishing current step...", "warning")

    def _run_room(self, room: Room) -> None:
        if self._cancel.is_set():
            self.room_status.emit(room.number, "cancelled")
            return

        deployer = Deployer(
            self.config,
            self.creds,
            log=lambda m, lvl: self.log.emit(m, lvl),
            progress=lambda s, t: None,
            is_cancelled=self._cancel.is_set,
            milestone=lambda done, total: None,
        )

        self.room_status.emit(room.number, "running")

        ok = False
        try:
            if self.action == "restart_service":
                ok = deployer.restart_service(room)
            elif self.action == "restart_nms_service":
                ok = deployer.restart_nms_service(room)
            elif self.action == "stop_service":
                ok = deployer.stop_service(room)
            elif self.action == "stop_nms_service":
                ok = deployer.stop_nms_service(room)
            elif self.action == "reboot":
                ok = deployer.reboot(room)
            elif self.action == "shutdown":
                ok = deployer.shutdown(room)
            elif self.action == "nms_bandwidth":
                ok = deployer.deploy_golden_nms_config(room, self.bandwidth)
            elif self.action == "nms_link_bandwidth":
                ok = deployer.deploy_nms_link_bandwidth(room, self.link_bandwidth_kbps)
            elif self.action == "remove_overlay":
                ok = deployer.deploy_nms_remove_overlay(room)
            elif self.action == "get_logs":
                ok = deployer.fetch_service_logs(room, self.logs_dir) is not None
            elif self.action == "view_config":
                ok = deployer.view_matrix_config(room, self.logs_dir)
            elif self.action == "get_nms_password":
                ok = deployer.get_nms_password(room)
            elif self.action == "remove_fingerprint":
                ok = deployer.remove_known_hosts_entry(room)
            elif self.action == "matrix_api_certs":
                ok = deployer.deploy_matrix_api_certs(room)
            elif self.action == "fix_room_config_race":
                ok = deployer.fix_room_config_race(room)
            elif self.action == "set_log_debug":
                ok = deployer.set_log_level(room, "debug")
            else:
                self.log.emit(f"Unknown action: {self.action}", "error")
        except Exception as exc:  # noqa: BLE001 - never let a thread die silently
            self.log.emit(f"Unexpected error on OR {room.number}: {exc}", "error")

        self.room_status.emit(room.number, "success" if ok else "failed")
        self.room_done.emit(room.number, ok)

    def run(self) -> None:
        try:
            run_sequential = self.sequential
            if not run_sequential and self.config.connection.same_physical_host:
                self.log.emit(
                    "same_physical_host is set in config; forcing sequential "
                    "execution to avoid contention on the shared host.",
                    "warning",
                )
                run_sequential = True

            # remove_fingerprint mutates the single local ~/.ssh/known_hosts
            # file via ssh-keygen -R; running it concurrently across rooms would
            # race on that shared file, so it always runs sequentially.
            if self.action in _LOCAL_SERIAL_ACTIONS:
                run_sequential = True

            if run_sequential or len(self.rooms) <= 1:
                for room in self.rooms:
                    self._run_room(room)
            else:
                workers = len(self.rooms)
                if self.max_concurrency:
                    workers = max(1, min(self.max_concurrency, len(self.rooms)))
                self.log.emit(
                    f"Running '{self.action}' on {len(self.rooms)} rooms "
                    f"({workers} at a time)...",
                    "info",
                )
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers
                ) as executor:
                    list(executor.map(self._run_room, self.rooms))
        finally:
            self.all_done.emit()


class OpenRoomGuiWorker(QThread):
    """Fetches a single room's default NMS/admin password off the UI thread
    so the GUI can open the room's demonstrator GUI and show the password
    without blocking while SSH'ing in."""

    log = pyqtSignal(str, str)                 # message, level
    finished_ok = pyqtSignal(int, object)      # room_number, password (str or None)

    def __init__(self, config: AppConfig, creds: DeploymentCredentials, room: Room):
        super().__init__()
        self.config = config
        self.creds = creds
        self.room = room

    def run(self) -> None:
        deployer = Deployer(
            self.config,
            self.creds,
            log=lambda m, lvl: self.log.emit(m, lvl),
            progress=lambda s, t: None,
            is_cancelled=lambda: False,
        )
        try:
            password = deployer.fetch_nms_password(self.room)
        except Exception as exc:  # noqa: BLE001 - never let a thread die silently
            self.log.emit(f"Unexpected error on OR {self.room.number}: {exc}", "error")
            password = None
        self.finished_ok.emit(self.room.number, password)


class DownloadWorker(QThread):
    """Downloads the latest SWU from Artifactory off the UI thread."""

    log = pyqtSignal(str, str)          # message, level
    progress = pyqtSignal(int, int)      # sent, total
    finished_ok = pyqtSignal(bool, str)  # success, file_path

    def __init__(self, config: AppConfig, creds: ArtifactoryCredentials, cache_dir: Path):
        super().__init__()
        self.config = config
        self.creds = creds
        self.cache_dir = cache_dir
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        client = ArtifactoryClient(self.config.artifactory, self.creds)
        try:
            dest = client.download_latest(
                self.cache_dir,
                log=lambda m, lvl: self.log.emit(m, lvl),
                progress=lambda s, t: self.progress.emit(s, t),
                is_cancelled=self._cancel.is_set,
            )
            self.finished_ok.emit(True, str(dest))
        except ArtifactoryError as exc:
            self.log.emit(str(exc), "error")
            self.finished_ok.emit(False, "")
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors
            self.log.emit(f"Unexpected download error: {exc}", "error")
            self.finished_ok.emit(False, "")


class BuildTriggerWorker(QThread):
    """Triggers a new "Embedded Builder" Jenkins build off the UI thread."""

    log = pyqtSignal(str, str)                # message, level
    finished_ok = pyqtSignal(bool, str, str)   # success, message, build_url ("" if unknown)

    def __init__(self, creds: JenkinsCredentials, job_name: str = JENKINS_JOB):
        super().__init__()
        self.creds = creds
        self.job_name = job_name

    def cancel(self) -> None:
        # The crumb fetch + build POST are a single quick round-trip with no
        # cancellable midpoint; this exists only so _launch_job's generic
        # on_cancel wiring has something to call.
        pass

    def run(self) -> None:
        client = JenkinsClient(self.creds, job_name=self.job_name)
        try:
            result = client.trigger_build(log=lambda m, lvl: self.log.emit(m, lvl))
            if result.build_number is not None:
                message = f"'{self.job_name}' #{result.build_number} triggered."
            else:
                message = f"'{self.job_name}' triggered (build number not yet known)."
            self.finished_ok.emit(True, message, result.build_url or "")
        except JenkinsError as exc:
            self.log.emit(str(exc), "error")
            self.finished_ok.emit(False, str(exc), "")
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors
            self.log.emit(f"Unexpected error triggering build: {exc}", "error")
            self.finished_ok.emit(False, str(exc), "")


class LogTailWorker(QThread):
    """Streams a systemd service's journal live (``journalctl -f``) from a
    single room until stopped.

    Read-only and non-mutating, so it is safe to run alongside (and does not
    lock out) other jobs on the same room - e.g. watching logs during a
    deploy to that room.
    """

    log = pyqtSignal(str, str)  # message, level
    stopped = pyqtSignal()

    def __init__(
        self,
        config: AppConfig,
        creds: DeploymentCredentials,
        room: Room,
        service: str,
    ):
        super().__init__()
        self.config = config
        self.creds = creds
        self.room = room
        self.service = service
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        conn = self.config.connection
        target = SSHTarget(
            host=conn.router_ip,
            port=self.room.ssh_port(conn.ssh_port_base),
            username=conn.ssh_username,
            password=self.creds.ssh_password,
        )
        try:
            client = connect(target)
        except SSHError as exc:
            self.log.emit(str(exc), "error")
            self.stopped.emit()
            return

        channel = None
        try:
            # Mirrors Deployer._read_sudo_prefix: only elevate if a sudo
            # password was actually supplied, to avoid a hanging prompt.
            prefix = ""
            if self.creds.sudo_password:
                quoted = shlex.quote(self.creds.sudo_password)
                prefix = f"echo {quoted} | sudo -S -p '' "
            cmd = (
                f"{prefix}journalctl -fu {shlex.quote(self.service)} "
                f"-n 50 --no-pager"
            ).strip()

            channel = client.get_transport().open_session()
            channel.get_pty()
            channel.settimeout(1.0)
            channel.exec_command(cmd)
            self.log.emit(
                f"--- Watching {self.service} live (Stop to end) ---", "info"
            )

            buf = ""
            while not self._cancel.is_set():
                try:
                    chunk = channel.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk.decode(errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self.log.emit(line.rstrip("\r"), "detail")
            if buf.strip():
                self.log.emit(buf.rstrip("\r"), "detail")
            self.log.emit(f"--- Stopped watching {self.service} ---", "warning")
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors
            self.log.emit(f"Log tail error: {exc}", "error")
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
            self.stopped.emit()


class DeploymentWorker(QThread):
    """Runs deployment to one or more rooms, optionally sequentially.

    Rooms are independent physical devices reachable through different router
    ports, so deployments can safely run concurrently across rooms. If the
    config marks the rooms as sharing one physical host (``same_physical_host``),
    execution is forced sequential regardless of ``sequential`` so simultaneous
    SWU installs don't contend over /tmp extraction space or trigger
    overlapping reboots of the same device.
    """

    log = pyqtSignal(str, str)               # message, level
    progress = pyqtSignal(int, int)          # sent, total (current file)
    room_progress = pyqtSignal(int, int, int)  # room_number, sent, total
    room_status = pyqtSignal(int, str)       # room_number, status
    room_done = pyqtSignal(int, bool)        # room_number, success
    all_done = pyqtSignal()

    def __init__(
        self,
        config: AppConfig,
        creds: DeploymentCredentials,
        rooms: List[Room],
        do_swu: bool,
        do_config: bool,
        swu_file: Optional[Path],
        template_path: Optional[Path],
        output_dir: Optional[Path],
        sequential: bool = True,
        max_concurrency: Optional[int] = None,
    ):
        super().__init__()
        self.config = config
        self.creds = creds
        self.rooms = rooms
        self.do_swu = do_swu
        self.do_config = do_config
        self.swu_file = swu_file
        self.template_path = template_path
        self.output_dir = output_dir
        self.sequential = sequential
        # Cap on simultaneously in-flight rooms when not forced sequential.
        # None means "no cap" (all selected rooms at once).
        self.max_concurrency = max_concurrency
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        self.log.emit("Cancellation requested - finishing current step...", "warning")

    def _run_room(self, room: Room) -> None:
        if self._cancel.is_set():
            self.room_status.emit(room.number, "cancelled")
            return

        def milestone(done: int, total: int, room_number: int = room.number) -> None:
            self.room_progress.emit(room_number, done, total)

        deployer = Deployer(
            self.config,
            self.creds,
            log=lambda m, lvl: self.log.emit(m, lvl),
            progress=lambda sent, total: self.progress.emit(sent, total),
            is_cancelled=self._cancel.is_set,
            milestone=milestone,
        )

        self.room_status.emit(room.number, "running")

        request = DeploymentRequest(
            room=room,
            do_swu=self.do_swu,
            do_config=self.do_config,
            swu_file=self.swu_file,
            template_path=self.template_path,
            output_dir=self.output_dir,
        )

        try:
            ok = deployer.deploy(request)
        except Exception as exc:  # noqa: BLE001 - never let a thread die silently
            self.log.emit(f"Unexpected error on OR {room.number}: {exc}", "error")
            ok = False

        self.room_status.emit(room.number, "success" if ok else "failed")
        self.room_done.emit(room.number, ok)

    def run(self) -> None:
        try:
            run_sequential = self.sequential
            if not run_sequential and self.config.connection.same_physical_host:
                self.log.emit(
                    "same_physical_host is set in config; forcing sequential "
                    "execution to avoid /tmp and reboot contention on the shared host.",
                    "warning",
                )
                run_sequential = True

            if run_sequential or len(self.rooms) <= 1:
                for room in self.rooms:
                    self._run_room(room)
            else:
                workers = len(self.rooms)
                if self.max_concurrency:
                    workers = max(1, min(self.max_concurrency, len(self.rooms)))
                self.log.emit(
                    f"Deploying to {len(self.rooms)} rooms ({workers} at a time)...",
                    "info",
                )
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers
                ) as executor:
                    list(executor.map(self._run_room, self.rooms))
        finally:
            self.all_done.emit()
