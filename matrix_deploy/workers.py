"""Qt worker threads wrapping the Qt-free deployment and download logic."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from .artifactory import ArtifactoryClient, ArtifactoryCredentials, ArtifactoryError
from .config import AppConfig, Room
from .deployer import DeploymentCredentials, DeploymentRequest, Deployer


class SystemActionWorker(QThread):
    """Runs a single system action (restart service or reboot) on one or more rooms."""

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
    ):
        super().__init__()
        self.config = config
        self.creds = creds
        self.rooms = rooms
        self.action = action
        self.bandwidth = bandwidth
        self.link_bandwidth_kbps = link_bandwidth_kbps
        self.logs_dir = logs_dir
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        self.log.emit("Cancellation requested - finishing current step...", "warning")

    def run(self) -> None:
        deployer = Deployer(
            self.config,
            self.creds,
            log=lambda m, lvl: self.log.emit(m, lvl),
            progress=lambda s, t: None,
            is_cancelled=self._cancel.is_set,
            milestone=lambda done, total: None,
        )

        for room in self.rooms:
            if self._cancel.is_set():
                self.room_status.emit(room.number, "cancelled")
                continue

            self.room_status.emit(room.number, "running")

            ok = False
            try:
                if self.action == "restart_service":
                    ok = deployer.restart_service(room)
                elif self.action == "restart_nms_service":
                    ok = deployer.restart_nms_service(room)
                elif self.action == "reboot":
                    ok = deployer.reboot(room)
                elif self.action == "nms_bandwidth":
                    ok = deployer.deploy_golden_nms_config(room, self.bandwidth)
                elif self.action == "nms_link_bandwidth":
                    ok = deployer.deploy_nms_link_bandwidth(room, self.link_bandwidth_kbps)
                elif self.action == "get_logs":
                    ok = deployer.fetch_service_logs(room, self.logs_dir) is not None
                elif self.action == "view_config":
                    ok = deployer.view_matrix_config(room, self.logs_dir)
                elif self.action == "get_nms_password":
                    ok = deployer.get_nms_password(room)
                else:
                    self.log.emit(f"Unknown action: {self.action}", "error")
            except Exception as exc:  # noqa: BLE001 - never let a thread die silently
                self.log.emit(f"Unexpected error on OR {room.number}: {exc}", "error")

            self.room_status.emit(room.number, "success" if ok else "failed")
            self.room_done.emit(room.number, ok)

        self.all_done.emit()


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


class DeploymentWorker(QThread):
    """Runs deployment to one or more rooms, optionally sequentially.

    For a shared physical host, sequential mode is strongly recommended so the
    rooms do not contend over /tmp extraction space and reboots.
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
        self._cancel = threading.Event()
        self._current_room_number: Optional[int] = None

    def cancel(self) -> None:
        self._cancel.set()
        self.log.emit("Cancellation requested - finishing current step...", "warning")

    def _emit_progress(self, sent: int, total: int) -> None:
        # Byte-level upload progress drives the global bar only.
        self.progress.emit(sent, total)

    def _emit_milestone(self, done: int, total: int) -> None:
        # Milestone progress drives the per-room bar (one room runs at a time).
        if self._current_room_number is not None:
            self.room_progress.emit(self._current_room_number, done, total)

    def run(self) -> None:
        deployer = Deployer(
            self.config,
            self.creds,
            log=lambda m, lvl: self.log.emit(m, lvl),
            progress=self._emit_progress,
            is_cancelled=self._cancel.is_set,
            milestone=self._emit_milestone,
        )

        for room in self.rooms:
            if self._cancel.is_set():
                self.room_status.emit(room.number, "cancelled")
                continue

            self._current_room_number = room.number
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

        self._current_room_number = None
        self.all_done.emit()
