"""Core deployment logic - Qt-free so it is testable and CLI-reusable.

A ``Deployer`` performs SWU updates and/or config deployment to a single room.
It reports progress through plain callbacks (``log`` / ``progress``) and supports
cooperative cancellation via an ``is_cancelled`` callable.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import paramiko

from .config import AppConfig, Room, golden_nms_config_path, render_nms_user_config
from .ssh_client import (
    SSHError,
    SSHTarget,
    connect,
    download_file,
    get_disk_free_kb,
    run_command,
    upload_dir,
    upload_file,
    wait_for_reboot,
)

Logger = Callable[[str, str], None]
_MISSING = object()
Progress = Callable[[int, int], None]
Milestone = Callable[[int, int], None]  # steps_done, steps_total


@dataclass
class DeploymentCredentials:
    ssh_password: Optional[str] = None
    sudo_password: Optional[str] = None


@dataclass
class DeploymentRequest:
    room: Room
    do_swu: bool
    swu_file: Optional[Path] = None


class Deployer:
    def __init__(
        self,
        config: AppConfig,
        creds: DeploymentCredentials,
        log: Logger,
        progress: Progress,
        is_cancelled: Callable[[], bool],
        milestone: Optional[Milestone] = None,
        swu_upload_semaphore: Optional[threading.Semaphore] = None,
    ):
        self.config = config
        self.creds = creds
        self.log = log
        self.progress = progress
        self.is_cancelled = is_cancelled
        self.milestone = milestone or (lambda done, total: None)
        # Rooms are reached through forwarded ports on the *same* router, so
        # their SWU uploads share one physical uplink. Running several
        # multi-GB uploads at once can starve/reset the slower connections
        # (empty-message socket/EOF errors) even though the rooms themselves
        # are independent devices. This semaphore, when provided, caps how
        # many SWU uploads are in flight at once regardless of overall room
        # concurrency, while still letting connect/install/reboot phases run
        # concurrently.
        self.swu_upload_semaphore = swu_upload_semaphore
        self._steps_done = 0
        self._steps_total = 1

    # -- milestone progress ----------------------------------------------

    def _plan_steps(self, request: "DeploymentRequest") -> int:
        """Number of milestones for this request.

        Always 1 for the connect step; SWU adds upload/install/online (3).
        """
        total = 1
        if request.do_swu:
            total += 3
        return total

    def _begin(self, total: int) -> None:
        self._steps_total = max(total, 1)
        self._steps_done = 0
        self.milestone(0, self._steps_total)

    def _advance(self) -> None:
        self._steps_done = min(self._steps_done + 1, self._steps_total)
        self.milestone(self._steps_done, self._steps_total)

    # -- helpers ----------------------------------------------------------

    def _target(self, room: Room) -> SSHTarget:
        conn = self.config.connection
        return SSHTarget(
            host=conn.router_ip,
            port=room.ssh_port(conn.ssh_port_base),
            username=conn.ssh_username,
            password=self.creds.ssh_password,
        )

    def _remote_swu_name(self, room: Room, swu_file: Path) -> str:
        """Per-room unique remote filename.

        Critical: when every "room" is a port on the same physical host,
        a shared filename causes parallel deploys to clobber each other.
        """
        return f"update-or{room.number}-{swu_file.name}"

    # -- public API -------------------------------------------------------

    def deploy(self, request: DeploymentRequest) -> bool:
        room = request.room
        self._begin(self._plan_steps(request))
        self.log(f"=== OR {room.number} ({room.name}) ===", "info")
        self.log(f"Connecting to {room.name}...", "info")

        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        self._advance()  # connected

        try:
            if request.do_swu:
                if not self._deploy_swu(client, room, request.swu_file):
                    return False

            self.log(f"OR {room.number}: deployment complete", "success")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    # -- SWU --------------------------------------------------------------

    def _fetch_room_password(
        self, client: paramiko.SSHClient, room: Room
    ) -> Optional[str]:
        """Run sudo act-mfg-eeprom display on the room and parse the NMS password.

        The password is stored under the key ``barco_nms_password`` in the
        command output.  We supply the user-provided sudo password via stdin
        so the sensitive string does not appear in the process list.
        """
        if not self.creds.sudo_password:
            self.log(
                "No sudo password provided; cannot run act-mfg-eeprom display.",
                "warning",
            )
            return None

        self.log("Fetching NMS password from act-mfg-eeprom display...", "detail")
        stdin, stdout, stderr = client.exec_command(
            "sudo -S -p '' act-mfg-eeprom display"
        )
        stdin.write(self.creds.sudo_password + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()

        output = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        if err:
            self.log(f"act-mfg-eeprom display stderr: {err}", "warning")

        if output:
            # Log raw output for debugging; may be multi-line.
            self.log(f"act-mfg-eeprom display output:\n{output}", "detail")

        password = self._parse_eeprom_password(output)
        if password:
            self.log("Parsed NMS password from act-mfg-eeprom display.", "detail")
        else:
            self.log(
                "Could not parse NMS password from act-mfg-eeprom display output.",
                "warning",
            )
        return password

    def _parse_eeprom_password(self, output: str) -> Optional[str]:
        """Parse barco_nms_password from act-mfg-eeprom display output."""
        # Primary: the field the product code uses for NMS authentication.
        match = re.search(r"^barco_nms_password\s*=\s*(\S+)", output, re.MULTILINE)
        if match:
            return match.group(1)

        # Legacy fallback patterns in case the field name changes.
        patterns = [
            re.compile(r"[Pp]assword\s*[:=]\s*(\S+)"),
            re.compile(r"[Ss]udo\s*[:=]\s*(\S+)"),
            re.compile(r"[Aa]dmin\s*[:=]\s*(\S+)"),
            re.compile(r"[Dd]efault\s*[:=]\s*(\S+)"),
        ]
        for line in output.splitlines():
            for pattern in patterns:
                m = pattern.search(line)
                if m:
                    return m.group(1)
        return None

    def _check_swu_space(self, client: paramiko.SSHClient, swu_file: Path) -> bool:
        """Abort early if /tmp doesn't have room for swupdate to extract the
        SWU's artifacts, instead of uploading and failing mid-install.

        swupdate's ``check_free_space`` fails when the extracted artifact
        (dominated by the rootfs image) doesn't fit in /tmp; that required
        size tracks the SWU file's own size, so we compare against it with a
        safety margin rather than trying to inspect the SWU contents.
        """
        swu_size_kb = swu_file.stat().st_size / 1024
        required_kb = swu_size_kb * 1.15  # 15% margin for non-artifact overhead

        free_kb = get_disk_free_kb(client, "/tmp")
        if free_kb is None:
            self.log("Could not determine free space on /tmp; proceeding anyway.", "warning")
            return True

        if free_kb < required_kb:
            self.log(
                f"Not enough free space on /tmp to install {swu_file.name}: "
                f"need ~{required_kb / 1024:.0f} MB, have {free_kb / 1024:.0f} MB free. "
                "Free up space on the device (e.g. journalctl --vacuum-size, "
                "old logs/tmp files) and try again.",
                "error",
            )
            return False

        self.log(
            f"/tmp free space check OK: {free_kb / 1024:.0f} MB available "
            f"(need ~{required_kb / 1024:.0f} MB).",
            "detail",
        )
        return True

    def check_disk_space(self, room: Room, path: str = "/tmp") -> bool:
        """Connect to a room and report free space on the filesystem containing
        ``path`` (defaults to /tmp, where swupdate extracts SWU artifacts)."""
        self.log(f"=== OR {room.number}: Disk space ({path}) ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            collected: List[str] = []
            run_command(
                client, f"df -h {shlex.quote(path)}", on_line=collected.append
            )
            self._advance()
            if not collected:
                self.log("Could not retrieve disk space.", "error")
                return False
            # Prefix each line with the room number: with several rooms
            # queried concurrently, lines otherwise interleave in the shared
            # log with no way to tell which room a given line belongs to.
            for line in collected:
                self.log(f"OR {room.number}: {line}", "detail")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def check_uptime(self, room: Room) -> bool:
        """Connect to a room and report system uptime (time since last boot).

        The box can now soft-reboot itself on a lockup/watchdog event
        without ever dropping the SSH connection, so "can I still SSH in"
        is no longer a reliable signal that the system is healthy -
        ``uptime`` since the last boot is.
        """
        self.log(f"=== OR {room.number}: Uptime ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            collected: List[str] = []
            run_command(client, "uptime", on_line=collected.append)
            self._advance()
            if not collected:
                self.log("Could not retrieve uptime.", "error")
                return False
            for line in collected:
                self.log(f"OR {room.number}: {line}", "detail")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def check_specs(self, room: Room) -> bool:
        """Connect to a room and report basic hardware/OS specs: kernel/OS
        version, CPU model/core count, memory, and root filesystem usage.

        All sections are collected first and emitted as a single log call
        so concurrent per-room output doesn't interleave line-by-line with
        other rooms in the shared log/output panel.
        """
        self.log(f"=== OR {room.number}: Specs ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            sections = [
                ("Kernel/OS", "uname -a"),
                (
                    "OS Release",
                    "cat /etc/os-release 2>/dev/null || cat /etc/*release 2>/dev/null",
                ),
                (
                    "CPU",
                    "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^[[:space:]]*//' "
                    "|| lscpu 2>/dev/null | grep 'Model name' | cut -d: -f2 | sed 's/^[[:space:]]*//'",
                ),
                (
                    "CPU cores",
                    "nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo",
                ),
                (
                    "Memory",
                    "free -m 2>/dev/null || head -3 /proc/meminfo",
                ),
                ("Root filesystem", "df -h /"),
            ]
            out_lines: List[str] = []
            for label, cmd in sections:
                lines: List[str] = []
                run_command(client, cmd, on_line=lines.append)
                if lines:
                    out_lines.append(f"OR {room.number}: --- {label} ---")
                    out_lines.extend(f"OR {room.number}: {line}" for line in lines)
            self._advance()
            if not out_lines:
                self.log("Could not retrieve specs.", "error")
                return False
            self.log("\n".join(out_lines), "detail")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _deploy_swu(
        self, client: paramiko.SSHClient, room: Room, swu_file: Optional[Path]
    ) -> bool:
        if not swu_file or not Path(swu_file).exists():
            self.log(f"SWU file not found: {swu_file}", "error")
            return False
        swu_file = Path(swu_file)

        self.log("--- SWU Update ---", "info")
        remote_name = self._remote_swu_name(room, swu_file)
        remote_path = f"/home/{self.config.connection.ssh_username}/{remote_name}"

        # Clean only THIS room's prior staged file (safe on shared host).
        run_command(client, f"rm -f {shlex.quote(remote_path)}")

        if not self._check_swu_space(client, swu_file):
            return False

        self.log(f"Uploading {swu_file.name}...", "detail")
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            if self.is_cancelled():
                self.log("Cancelled before upload completed.", "warning")
                return False

            if self.swu_upload_semaphore is not None:
                self.swu_upload_semaphore.acquire()
            try:
                # If a previous attempt's connection died mid-transfer, the
                # control connection is dead too - reconnect before retrying.
                transport = client.get_transport()
                if transport is None or not transport.is_active():
                    self.log("Reconnecting before upload retry...", "detail")
                    client = connect(self._target(room))
                upload_file(client, str(swu_file), remote_path, self.progress, self.log)
                break
            except Exception as exc:  # noqa: BLE001
                # Exceptions raised when a connection is reset/dropped mid
                # transfer (e.g. socket.timeout, EOFError) often have no
                # message, so fall back to the exception type name.
                detail = str(exc) or type(exc).__name__
                if attempt >= max_attempts:
                    self.log(
                        f"Upload failed after {attempt} attempt(s): {detail}",
                        "error",
                    )
                    return False
                wait_s = 5 * attempt
                self.log(
                    f"Upload attempt {attempt} failed ({detail}); "
                    f"retrying in {wait_s}s...",
                    "warning",
                )
                try:
                    run_command(client, f"rm -f {shlex.quote(remote_path)}")
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass
                time.sleep(wait_s)
            finally:
                if self.swu_upload_semaphore is not None:
                    self.swu_upload_semaphore.release()
        self.log("Upload complete.", "success")
        self._advance()  # SWU uploaded

        if self.is_cancelled():
            self.log("Cancelled before install.", "warning")
            return False

        self.log("Installing via swupdate-client...", "detail")
        success = {"ok": False}

        def on_line(line: str) -> None:
            if not line:
                return
            if "SWUPDATE successful" in line:
                success["ok"] = True
                self.log(line, "success")
            elif "ERROR" in line or "FAILURE" in line:
                self.log(line, "error")
            # Suppress the verbose "Keeping file" overlay-cleanup spam.
            elif "Keeping file" in line or "Keeping directory" in line:
                return
            else:
                self.log(line, "detail")

        install_start = time.monotonic()
        run_command(
            client,
            f"swupdate-client -v {shlex.quote(remote_path)}",
            get_pty=True,
            on_line=on_line,
        )
        install_elapsed = time.monotonic() - install_start

        # Clean up uploaded file (best effort; ignore errors).
        run_command(client, f"rm -f {shlex.quote(remote_path)}")

        if not success["ok"]:
            self.log("SWU update failed - no success message received.", "error")
            return False

        self.log(f"Install (swupdate-client) took {install_elapsed:.1f}s.", "detail")
        self.log("SWU update successful - system will reboot.", "success")
        self._advance()  # SWU installed
        reboot_start = time.monotonic()
        if not wait_for_reboot(self._target(room), self.log, self.is_cancelled):
            self.log("System did not come back online within timeout.", "error")
            return False
        self.log(
            f"Reboot/online wait took {time.monotonic() - reboot_start:.1f}s.",
            "detail",
        )
        self.log("System is back online.", "success")
        self._advance()  # system back online
        return True

    # -- Config -----------------------------------------------------------

    def _apply_config_and_restart_service(
        self, client: paramiko.SSHClient, room: Room
    ) -> bool:
        """Copy the staged config to the remote path and restart the service."""
        conn = self.config.connection
        sudo = self._sudo_prefix()
        remote_staging = f"/home/{conn.ssh_username}/or{room.number}.json"
        cmd = (
            f"{sudo} cp {shlex.quote(remote_staging)} {shlex.quote(conn.remote_config_path)} "
            f"&& {sudo} systemctl restart {shlex.quote(conn.service_name)} "
            f"&& {sudo} systemctl --no-pager --full status {shlex.quote(conn.service_name)} -n 10"
        )
        exit_status = run_command(
            client, cmd, get_pty=True, on_line=lambda l: self.log(l, "detail")
        )
        return exit_status == 0

    def _restart_service(
        self, client: paramiko.SSHClient, room: Room, service_name: str
    ) -> bool:
        """Restart the given service without touching config files."""
        sudo = self._sudo_prefix()
        cmd = (
            f"{sudo} systemctl restart {shlex.quote(service_name)} "
            f"&& {sudo} systemctl --no-pager --full status {shlex.quote(service_name)} -n 10"
        )
        exit_status = run_command(
            client, cmd, get_pty=True, on_line=lambda l: self.log(l, "detail")
        )
        return exit_status == 0

    def _restart_service_on_room(self, room: Room, service_name: str) -> bool:
        """Connect to a room and restart the given service."""
        self.log(f"=== OR {room.number}: Restarting {service_name} ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            ok = self._restart_service(client, room, service_name)
            if ok:
                self.log("Service restarted.", "success")
            return ok
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def restart_service(self, room: Room) -> bool:
        """Connect to a room and restart the matrix-api service."""
        return self._restart_service_on_room(room, self.config.connection.service_name)

    def restart_nms_service(self, room: Room) -> bool:
        """Connect to a room and restart the barco-nms service."""
        return self._restart_service_on_room(room, self.config.connection.nms_service_name)

    def _stop_service(
        self, client: paramiko.SSHClient, room: Room, service_name: str
    ) -> bool:
        """Stop the given service."""
        sudo = self._sudo_prefix()
        cmd = (
            f"{sudo} systemctl stop {shlex.quote(service_name)} "
            f"&& {sudo} systemctl --no-pager --full status {shlex.quote(service_name)} -n 10"
        )
        exit_status = run_command(
            client, cmd, get_pty=True, on_line=lambda l: self.log(l, "detail")
        )
        # `systemctl status` exits 3 for an inactive (stopped) unit, which is
        # the expected outcome here, so accept it alongside a clean 0.
        return exit_status in (0, 3)

    def _stop_service_on_room(self, room: Room, service_name: str) -> bool:
        """Connect to a room and stop the given service."""
        self.log(f"=== OR {room.number}: Stopping {service_name} ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            ok = self._stop_service(client, room, service_name)
            if ok:
                self.log("Service stopped.", "success")
            return ok
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def stop_service(self, room: Room) -> bool:
        """Connect to a room and stop the matrix-api service."""
        return self._stop_service_on_room(room, self.config.connection.service_name)

    def stop_nms_service(self, room: Room) -> bool:
        """Connect to a room and stop the barco-nms service."""
        return self._stop_service_on_room(room, self.config.connection.nms_service_name)

    def _service_status(
        self, client: paramiko.SSHClient, room: Room, service_name: str
    ) -> bool:
        """Read-only ``systemctl status`` for the given service, streamed to the log."""
        prefix = self._read_sudo_prefix()
        cmd = (
            f"{prefix} systemctl --no-pager --full status {shlex.quote(service_name)} -n 20"
        ).strip()
        # `systemctl status` exit codes reflect unit state (0=active, 3=inactive/
        # failed, 4=unit not found), not command failure - log whatever comes
        # back and only treat this as a hard failure if we got no output at all.
        collected: List[str] = []

        def _on_line(line: str) -> None:
            collected.append(line)
            self.log(line, "detail")

        run_command(client, cmd, get_pty=True, on_line=_on_line)
        return bool(collected)

    def _service_status_on_room(self, room: Room, service_name: str) -> bool:
        """Connect to a room and report the given service's status."""
        self.log(f"=== OR {room.number}: {service_name} status ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            ok = self._service_status(client, room, service_name)
            self._advance()
            if not ok:
                self.log("Could not retrieve service status.", "error")
            return ok
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def matrix_api_status(self, room: Room) -> bool:
        """Connect to a room and report the matrix-api service status."""
        return self._service_status_on_room(room, self.config.connection.service_name)

    def nms_service_status(self, room: Room) -> bool:
        """Connect to a room and report the barco-nms service status."""
        return self._service_status_on_room(room, self.config.connection.nms_service_name)

    def _read_sudo_prefix(self) -> str:
        """Sudo prefix for read-only commands: only elevate if a password is
        available, otherwise run unprivileged (avoids a hanging password prompt)."""
        return self._sudo_prefix() if self.creds.sudo_password else ""

    @staticmethod
    def _since_label(since_hours: int) -> str:
        return f"{since_hours // 24}d" if since_hours % 24 == 0 else f"{since_hours}h"

    def fetch_service_logs(
        self, room: Room, dest_dir: Path, lines: int = 2000, since_hours: Optional[int] = None
    ) -> Optional[Path]:
        """Collect journald logs for matrix-api and barco-nms and save them to a
        timestamped text file in ``dest_dir``. Returns the file path on success.
        With ``since_hours`` the export covers that time window instead of the
        last ``lines`` lines."""
        conn = self.config.connection
        services = [conn.service_name, conn.nms_service_name]
        if since_hours:
            window = f"last {self._since_label(since_hours)}"
            limit = f"--since {shlex.quote(f'-{int(since_hours)}h')}"
        else:
            window = f"last {lines} lines"
            limit = f"-n {int(lines)}"
        self.log(f"=== OR {room.number}: Collecting logs ({', '.join(services)}) - {window} ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return None

        try:
            self._begin(len(services))
            prefix = self._read_sudo_prefix()
            sections: List[str] = []
            for service in services:
                self.log(f"Reading {window} of logs for {service}...", "detail")
                collected: List[str] = []
                cmd = (
                    f"{prefix} journalctl -u {shlex.quote(service)} --no-pager {limit}"
                ).strip()
                run_command(
                    client, cmd, get_pty=True, on_line=lambda l: collected.append(l)
                )
                header = f"{'=' * 70}\n{service} ({window})\n{'=' * 70}"
                sections.append(header + "\n" + "\n".join(collected))
                self._advance()

            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            dest = dest_dir / f"or{room.number}-logs-{timestamp}.txt"
            banner = (
                f"Matrix Deploy log export\n"
                f"Room: OR {room.number} ({room.name})\n"
                f"Host: {conn.router_ip}:{room.ssh_port(conn.ssh_port_base)}\n"
                f"Generated: {timestamp}\n"
                f"Window: {window}\n"
            )
            dest.write_text(banner + "\n" + "\n\n".join(sections) + "\n", encoding="utf-8")
            self.log(f"Saved logs to {dest}", "success")
            return dest
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def fetch_full_journal(
        self, room: Room, dest_dir: Path, since_hours: Optional[int] = None
    ) -> Optional[Path]:
        """Download the complete systemd journal (unfiltered - every unit,
        every priority; limited to the last ``since_hours`` hours if given,
        otherwise no time window) to a timestamped text file in
        ``dest_dir``. Returns the file path on success; nothing is printed
        to the log terminal besides progress/status.

        The journal is dumped to a temp file on the room and pulled down
        via SCP (with a progress callback) instead of being streamed
        line-by-line through the SSH channel - for a large journal that is
        both much faster and gives real transfer progress instead of an
        indefinite-looking wait.
        """
        conn = self.config.connection
        window = f"last {self._since_label(since_hours)}" if since_hours else "all available"
        since_arg = f" --since {shlex.quote(f'-{int(since_hours)}h')}" if since_hours else ""
        self.log(f"=== OR {room.number}: Downloading full journal ({window}) ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return None

        remote_tmp = f"/tmp/or{room.number}-full-journal.log"
        try:
            self._begin(2)
            prefix = self._read_sudo_prefix()
            self.log("Dumping journal to a temp file on the room...", "detail")
            dump_cmd = (
                f"{prefix} journalctl --no-pager{since_arg} > {shlex.quote(remote_tmp)} 2>&1"
            ).strip()
            run_command(client, dump_cmd)
            self._advance()  # dumped

            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            dest = dest_dir / f"or{room.number}-journal-{timestamp}.txt"
            local_tmp = dest_dir / f".or{room.number}-journal-{timestamp}.tmp"

            self.log("Downloading journal file...", "detail")
            try:
                download_file(client, remote_tmp, str(local_tmp), self.progress, self.log)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Download failed: {exc}", "error")
                return None
            self._advance()  # downloaded

            banner = (
                f"Matrix Deploy full journal export\n"
                f"Room: OR {room.number} ({room.name})\n"
                f"Host: {conn.router_ip}:{room.ssh_port(conn.ssh_port_base)}\n"
                f"Generated: {timestamp}\n"
                f"Window: {window}\n\n"
            )
            with open(dest, "w", encoding="utf-8", errors="replace") as out_f:
                out_f.write(banner)
                with open(local_tmp, "r", encoding="utf-8", errors="replace") as in_f:
                    shutil.copyfileobj(in_f, out_f)
            local_tmp.unlink(missing_ok=True)

            self.log(f"OR {room.number}: saved full journal to {dest}", "success")
            return dest
        finally:
            run_command(client, f"rm -f {shlex.quote(remote_tmp)}")
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _matrix_app_session_script(launcher: str) -> List[str]:
        """Shell lines that validate the kiosk launcher and discover the
        active Wayland/sway session env needed to relaunch the Matrix App."""
        return [
            f"launcher={shlex.quote(launcher)}",
            'if ! test -f "$launcher"; then echo "Launcher not found: $launcher" >&2; exit 2; fi',
            "if ! grep -Fq -- '--ozone-platform=wayland' \"$launcher\"; then echo \"Launcher has no supported Matrix App invocation.\" >&2; exit 3; fi",
            'gui_session=$(loginctl list-sessions --no-legend 2>/dev/null | awk "{print \\$1}" | while read -r session; do',
            '  [ "$(loginctl show-session "$session" -p Active --value 2>/dev/null)" = yes ] || continue',
            '  [ "$(loginctl show-session "$session" -p Type --value 2>/dev/null)" = wayland ] || continue',
            '  [ "$(loginctl show-session "$session" -p Class --value 2>/dev/null)" = user ] || continue',
            '  printf "%s" "$session"; break',
            "done)",
            'if [ -z "$gui_session" ]; then echo "No active local Wayland session for Matrix App." >&2; exit 4; fi',
            'app_user=$(loginctl show-session "$gui_session" -p Name --value)',
            'app_uid=$(loginctl show-session "$gui_session" -p User --value)',
            'sway_pid=$(pgrep -u "$app_uid" -x sway 2>/dev/null | head -n 1)',
            'if [ -z "$sway_pid" ]; then echo "No Sway compositor running for $app_user." >&2; exit 5; fi',
            'sway_env=$(tr "\\0" "\\n" < "/proc/$sway_pid/environ" 2>/dev/null || true)',
            'runtime_dir=$(printf "%s\\n" "$sway_env" | sed -n "s/^XDG_RUNTIME_DIR=//p" | head -n 1)',
            'wayland_display=$(printf "%s\\n" "$sway_env" | sed -n "s/^WAYLAND_DISPLAY=//p" | head -n 1)',
            'sway_socket=$(printf "%s\\n" "$sway_env" | sed -n "s/^SWAYSOCK=//p" | head -n 1)',
            'dbus_address=$(printf "%s\\n" "$sway_env" | sed -n "s/^DBUS_SESSION_BUS_ADDRESS=//p" | head -n 1)',
            'runtime_dir=${runtime_dir:-/run/user/$app_uid}',
            'if [ -z "$wayland_display" ]; then wayland_display=$(find "$runtime_dir" -maxdepth 1 -type s -name "wayland-*" -printf "%f\\n" 2>/dev/null | head -n 1); fi',
            'if [ -z "$sway_socket" ]; then sway_socket=$(find "$runtime_dir" -maxdepth 1 -type s -name "sway-ipc.*.sock" -print -quit 2>/dev/null); fi',
            'dbus_address=${dbus_address:-unix:path=$runtime_dir/bus}',
        ]

    @staticmethod
    def _matrix_app_relaunch_script(port: int) -> List[str]:
        """Shell lines that stop the running Matrix App and relaunch it via
        the launcher inside the discovered sway session."""
        return [
            "app_pids=$(ps -eo pid=,args= | awk '/[e]lectron.*matrix-app\\.asar/ && $0 !~ /--type=/ {print $1}')",
            '[ -n "$app_pids" ] && kill $app_pids 2>/dev/null || true',
            "sleep 1",
            # A lingering child (e.g. gdbus) can keep the debug port bound,
            # making the relaunched app fail with "Address already in use".
            # Free the port before relaunching.
            f"port_holder=$(ss -ltnpH 'sport = :{port}' 2>/dev/null | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2)",
            '[ -n "$port_holder" ] && kill "$port_holder" 2>/dev/null || true',
            "sleep 1",
            'runuser -u "$app_user" -- env "XDG_RUNTIME_DIR=$runtime_dir" "WAYLAND_DISPLAY=$wayland_display" "SWAYSOCK=$sway_socket" "DBUS_SESSION_BUS_ADDRESS=$dbus_address" swaymsg -s "$sway_socket" exec "$launcher" 2>&1',
            'echo "Relaunch requested."',
        ]

    def _run_sudo_script(self, client: paramiko.SSHClient, script: str) -> bool:
        """Run ``script`` as root via ``sudo -S bash -s`` (password on the
        first stdin line, script after it), logging its output. Returns True
        on exit status 0."""
        stdin, stdout, stderr = client.exec_command("sudo -S -p '' bash -s")
        stdin.write(self.creds.sudo_password + "\n")
        stdin.write(script + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        for line in (out.splitlines() + err.splitlines()):
            if line.strip():
                self.log(line, "detail")
        return stdout.channel.recv_exit_status() == 0

    @staticmethod
    def _devtools_answers(client: paramiko.SSHClient, port: int) -> bool:
        probe: List[str] = []
        run_command(
            client,
            f"curl -sS --connect-timeout 2 --max-time 3 http://127.0.0.1:{port}/json/version 2>/dev/null || true",
            on_line=probe.append,
        )
        return any("webSocketDebuggerUrl" in l for l in probe)

    def enable_matrix_app_debugging(self, room: Room) -> bool:
        """Enable Chrome DevTools on the Matrix App (Electron kiosk) over SSH:
        inject ``--remote-debugging-*`` flags into the launcher (backing it up
        first) and relaunch the app through the room's active sway session,
        then poll until the CDP endpoint answers. Ported from the matrix-lab
        extension's ``enableMatrixAppDebugger``. Requires the sudo password."""
        conn = self.config.connection
        port = conn.matrix_app_debug_port
        launcher = conn.matrix_app_launcher_path
        self.log(f"=== OR {room.number}: Enabling Matrix App remote debugging ===", "info")
        if not self.creds.sudo_password:
            self.log("A sudo password is required to relaunch the Matrix App.", "error")
            return False

        flags = f"--remote-debugging-address=127.0.0.1 --remote-debugging-port={port}"
        marker = f"--remote-debugging-port={port}"
        script = "\n".join(self._matrix_app_session_script(launcher) + [
            f"if grep -Fq -- {shlex.quote(marker)} \"$launcher\"; then echo 'DevTools flags already present.'; else",
            '  backup="${launcher}.matrix-deploy.bak.$(date +%Y%m%d%H%M%S)"',
            '  cp -- "$launcher" "$backup"',
            f"  sed -i 's|--ozone-platform=wayland|--ozone-platform=wayland {flags}|' \"$launcher\"",
            f"  if ! grep -Fq -- {shlex.quote(marker)} \"$launcher\"; then mv -- \"$backup\" \"$launcher\"; echo 'Could not add flags; launcher restored.' >&2; exit 7; fi",
            '  echo "DevTools flags added. Backup: $backup"',
            "fi",
        ] + self._matrix_app_relaunch_script(port))

        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False

        def _devtools_ready() -> bool:
            return self._devtools_answers(client, port)

        try:
            self._begin(2)

            # Don't kill/relaunch a working kiosk: if DevTools already answers,
            # we're done (re-running Enable otherwise restarts the app and can
            # hang while it comes back).
            if _devtools_ready():
                self.log(f"OR {room.number}: Matrix App DevTools already enabled on port {port}.", "success")
                self._advance()
                self._advance()
                return True

            if not self._run_sudo_script(client, script):
                self.log("Failed to enable Matrix App debugging.", "error")
                return False
            self._advance()

            self.log("Waiting for Chrome DevTools to come up (up to ~40s)...", "detail")
            for attempt in range(20):
                if self.is_cancelled():
                    return False
                if _devtools_ready():
                    self.log(f"OR {room.number}: Matrix App DevTools ready on port {port}.", "success")
                    self._advance()
                    return True
                if attempt and attempt % 3 == 0:
                    self.log(f"  still waiting for DevTools... ({attempt * 2}s)", "detail")
                time.sleep(1)
            self.log(
                f"Relaunched, but DevTools did not become ready on port {port}. "
                "The kiosk may still be starting - wait a moment and try View Matrix App.",
                "error",
            )
            return False
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def disable_matrix_app_debugging(self, room: Room) -> bool:
        """Undo ``enable_matrix_app_debugging``: strip the
        ``--remote-debugging-*`` flags from the launcher (backing it up first)
        and relaunch the Matrix App so the DevTools port closes. Leaves the
        app alone if the flags are absent and the port is already closed.
        Requires the sudo password."""
        conn = self.config.connection
        port = conn.matrix_app_debug_port
        self.log(f"=== OR {room.number}: Disabling Matrix App remote debugging ===", "info")
        if not self.creds.sudo_password:
            self.log("A sudo password is required to relaunch the Matrix App.", "error")
            return False

        script = "\n".join(self._matrix_app_session_script(conn.matrix_app_launcher_path) + [
            "if grep -Fq -- '--remote-debugging-port' \"$launcher\"; then",
            '  backup="${launcher}.matrix-deploy.bak.$(date +%Y%m%d%H%M%S)"',
            '  cp -- "$launcher" "$backup"',
            "  sed -i -E 's/ --remote-debugging-(address|port)=[0-9.]*//g' \"$launcher\"",
            "  if grep -Fq -- '--remote-debugging-port' \"$launcher\"; then mv -- \"$backup\" \"$launcher\"; echo 'Could not remove flags; launcher restored.' >&2; exit 7; fi",
            '  echo "DevTools flags removed. Backup: $backup"',
            f"elif ! ss -ltnH 'sport = :{port}' 2>/dev/null | grep -q .; then",
            "  echo 'Remote debugging is already off.'; exit 0",
            "fi",
        ] + self._matrix_app_relaunch_script(port))

        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(2)
            if not self._run_sudo_script(client, script):
                self.log("Failed to disable Matrix App debugging.", "error")
                return False
            self._advance()
            for _ in range(10):
                if self.is_cancelled():
                    return False
                if not self._devtools_answers(client, port):
                    self.log(f"OR {room.number}: Matrix App remote debugging is off.", "success")
                    self._advance()
                    return True
                time.sleep(1)
            self.log(f"DevTools is still answering on port {port} - the old app may not have exited.", "error")
            return False
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def export_support_bundle(
        self,
        room: Room,
        dest_dir: Path,
        skip_nms: bool = False,
        skip_intel: bool = False,
    ) -> Optional[Path]:
        """Run the room's built-in ``get-support`` collector (the same tool
        that produces the official SynergyLogs support bundle: matrix
        diagnostics, room configuration, the encrypted NMS support bundle,
        act-intel-diag output and the full systemd journal), then download the
        resulting ``.zip``. Requires the sudo password."""
        conn = self.config.connection
        self.log(f"=== OR {room.number}: Collecting support bundle (get-support) ===", "info")
        if not self.creds.sudo_password:
            self.log("A sudo password is required to run the device collector.", "error")
            return None

        # Write to a persistent per-run dir on the room (NOT /tmp - the
        # uncompressed journal is too big for the RAM-backed tmpfs, per
        # get-support's own guidance).
        remote_dir = f"/home/{conn.ssh_username}/matrix-deploy-support"
        get_cmd = ["get-support", "--output", shlex.quote(remote_dir), "-v"]
        if skip_nms:
            get_cmd.append("--skip-nms")
        if skip_intel:
            get_cmd.append("--skip-intel-diag")

        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return None
        try:
            self._begin(2)
            run_command(client, f"mkdir -p {shlex.quote(remote_dir)}")
            self.log("Running device collector - this can take a few minutes...", "detail")

            # Feed the sudo password via a pipe (echo | sudo -S), NOT a PTY -
            # with a PTY sudo prompts on the terminal, echoes the password and
            # ignores stdin. ``-v`` progress goes to stderr, so merge 2>&1 and
            # stream every line; the final .zip path is printed on stdout.
            full_cmd = f"{self._sudo_prefix()} {' '.join(get_cmd)} 2>&1"
            zip_remote: Optional[str] = None
            zip_re = re.compile(r"(/\S+\.zip)")

            def _on_line(line: str) -> None:
                nonlocal zip_remote
                line = line.strip()
                if not line:
                    return
                self.log(line, "detail")
                m = zip_re.search(line)
                if m and m.group(1).endswith(".zip"):
                    zip_remote = m.group(1)  # keep the last .zip seen

            exit_status = run_command(client, full_cmd, on_line=_on_line)
            if exit_status != 0:
                # get-support returns non-zero (e.g. 2) when some artifacts are
                # missing - typically the NMS bundle if barco-nms is down - but
                # it still produces a usable bundle. Warn and download it; the
                # collection_report.txt inside documents what was skipped.
                self.log(
                    f"get-support reported missing artifacts (exit {exit_status}); "
                    "downloading the partial bundle anyway (see collection_report.txt).",
                    "warning",
                )
            self._advance()

            if not zip_remote:
                # Fall back to the newest .zip in the output dir.
                found: List[str] = []
                run_command(
                    client,
                    f"ls -1t {shlex.quote(remote_dir)}/*.zip 2>/dev/null | head -1",
                    on_line=found.append,
                )
                zip_remote = found[0].strip() if found else None
            if not zip_remote:
                self.log("Could not determine the produced bundle path.", "error")
                return None

            # get-support runs as root, so the ZIP is root-owned; make it
            # readable by the SSH user before pulling it down via SCP.
            run_command(client, f"{self._sudo_prefix()} chmod a+r {shlex.quote(zip_remote)}")

            self.log(f"Downloading {zip_remote} ...", "detail")
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            local = dest_dir / f"or{room.number}-{Path(zip_remote).name}"
            try:
                download_file(client, zip_remote, str(local), self.progress, self.log)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Download failed: {exc}", "error")
                return None
            # Clean up the staging dir + archive on the room (root-owned; sudo).
            run_command(client, f"{self._sudo_prefix()} rm -rf {shlex.quote(remote_dir)}")
            self._advance()

            # List what's inside so the operator can confirm completeness.
            try:
                import zipfile
                with zipfile.ZipFile(local) as zf:
                    names = zf.namelist()
                size_mb = local.stat().st_size / (1024 * 1024)
                self.log(f"Bundle contents ({len(names)} entries, {size_mb:.1f} MB):", "detail")
                for name in names:
                    self.log(f"  - {name}", "detail")
            except Exception as exc:  # noqa: BLE001 - listing is best-effort
                self.log(f"(Could not list bundle contents: {exc})", "warning")

            self.log(f"OR {room.number}: support bundle saved to {local}", "success")
            return local
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def check_system_errors(self, room: Room, hours: int = 3) -> bool:
        """Connect to a room and report error-and-above messages (priority
        err/crit/alert/emerg) from the last ``hours`` hours, split into the
        kernel ring buffer and the full journal (every systemd unit).

        Kernel-only issues (watchdog resets, driver faults, hardware errors
        like the HDA codec probe failure) show up in "Kernel"; anything else
        misbehaving at the same time - matrix-api/barco-nms crashes, failed
        units, etc. - shows up in "All services", so a hardware fault can be
        correlated with whatever service issue it triggered instead of
        needing a second, separate lookup.
        """
        self.log(f"=== OR {room.number}: System Errors (last {self._since_label(hours)}) ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            prefix = self._read_sudo_prefix()
            since = shlex.quote(f"-{hours}h")
            sections = [
                ("Kernel", f"{prefix} journalctl -k --no-pager -p err --since {since}".strip()),
                ("All services", f"{prefix} journalctl --no-pager -p err --since {since}".strip()),
            ]
            out_lines: List[str] = []
            for label, cmd in sections:
                lines: List[str] = []
                run_command(client, cmd, get_pty=True, on_line=lines.append)
                if lines:
                    out_lines.append(f"OR {room.number}: --- {label} ---")
                    out_lines.extend(f"OR {room.number}: {line}" for line in lines)
            self._advance()
            if not out_lines:
                self.log(
                    f"OR {room.number}: no errors in the last {self._since_label(hours)}.",
                    "success",
                )
                return True
            self.log("\n".join(out_lines), "detail")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def run_custom_command(self, room: Room, command: str, use_sudo: bool = False) -> bool:
        """Connect to a room and run an arbitrary, user-supplied shell command,
        streaming its combined stdout/stderr to the log as a single block.

        Intended for ad-hoc diagnostics that don't have a dedicated button
        (e.g. ``journalctl``, ``dmesg``, ``rasdaemon``). Runs without a PTY so
        pager-invoking commands (``journalctl`` without ``--no-pager``, etc.)
        auto-detect a non-interactive stdout and print directly rather than
        hanging waiting for interactive pager input.
        """
        command = command.strip()
        if not command:
            self.log("No command entered.", "error")
            return False

        self.log(f"=== OR {room.number}: Run command ===", "info")
        self.log(f"OR {room.number}: $ {command}", "detail")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            full_cmd = f"{self._sudo_prefix()} {command}" if use_sudo else command
            out_lines: List[str] = []
            exit_status = run_command(client, full_cmd, on_line=out_lines.append)
            self._advance()
            if out_lines:
                self.log(
                    "\n".join(f"OR {room.number}: {line}" for line in out_lines),
                    "detail",
                )
            if exit_status != 0:
                self.log(
                    f"OR {room.number}: command exited with status {exit_status}",
                    "warning",
                )
            return exit_status == 0
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def fetch_nms_password(self, room: Room) -> Optional[str]:
        """Connect to a room and return the parsed ``barco_nms_password``
        (the default NMS/admin login password), or ``None`` on failure."""
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return None
        try:
            return self._fetch_room_password(client, room)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def get_nms_password(self, room: Room) -> bool:
        """Connect to a room, run ``sudo act-mfg-eeprom display``, and log the
        parsed ``barco_nms_password`` (the default NMS login password)."""
        self.log(f"=== OR {room.number}: NMS Password ===", "info")
        password = self.fetch_nms_password(room)
        if password:
            self.log(f"OR {room.number} ({room.name}): NMS password = {password}", "success")
            return True
        self.log(f"OR {room.number} ({room.name}): NMS password not found.", "error")
        return False

    def view_matrix_config(self, room: Room, dest_dir: Optional[Path] = None) -> bool:
        """Cat the matrix.api.config.json on a room, stream it to the log, and
        optionally save a raw copy to ``dest_dir``."""
        conn = self.config.connection
        self.log(f"=== OR {room.number}: {conn.remote_config_path} ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            prefix = self._read_sudo_prefix()
            cmd = f"{prefix} cat {shlex.quote(conn.remote_config_path)}".strip()
            collected: List[str] = []

            def _on_line(line: str) -> None:
                collected.append(line)
                self.log(line, "detail")

            exit_status = run_command(client, cmd, on_line=_on_line)
            self._advance()
            if exit_status != 0:
                self.log("Failed to read config file.", "error")
                return False

            if dest_dir is not None:
                dest_dir = Path(dest_dir)
                dest_dir.mkdir(parents=True, exist_ok=True)
                timestamp = time.strftime("%Y%m%d-%H%M%S")
                dest = dest_dir / f"or{room.number}-matrix.api.config-{timestamp}.json"
                dest.write_text("\n".join(collected) + "\n", encoding="utf-8")
                self.log(f"Saved raw config copy to {dest}", "success")

            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def read_matrix_config_text(self, room: Room) -> Optional[str]:
        """Return the raw text of the room's matrix.api.config.json for live
        editing (no JSON re-formatting). Returns ``None`` on failure."""
        conn = self.config.connection
        self.log(f"=== OR {room.number}: Loading {conn.remote_config_path} ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return None
        try:
            self._begin(1)
            prefix = self._read_sudo_prefix()
            cmd = f"{prefix} cat {shlex.quote(conn.remote_config_path)}".strip()
            collected: List[str] = []
            exit_status = run_command(client, cmd, on_line=collected.append)
            self._advance()
            if exit_status != 0:
                self.log("Failed to read config file.", "error")
                return None
            raw = "\n".join(collected)
            if not raw.strip():
                self.log("Config file read returned no output.", "error")
                return None
            self.log(f"OR {room.number}: config loaded ({len(raw)} bytes).", "success")
            return raw
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def deploy_matrix_config_text(self, room: Room, content: str) -> bool:
        """Validate ``content`` as JSON, upload it verbatim to the room, apply
        it to the remote config path, and restart matrix-api. Used by the live
        config editor (replaces the golden-template config generation flow)."""
        conn = self.config.connection
        self.log(f"=== OR {room.number}: Deploying edited config ===", "info")
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            self.log(f"Refusing to deploy invalid JSON: {exc}", "error")
            return False

        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(2)
            local_tmp = Path(tempfile.gettempdir()) / f"or{room.number}-edited.json"
            # Preserve the user's exact text (trailing newline for POSIX tools).
            local_tmp.write_text(content.rstrip("\n") + "\n", encoding="utf-8")

            remote_staging = f"/home/{conn.ssh_username}/or{room.number}.json"
            self.log("Uploading edited config...", "detail")
            try:
                upload_file(client, str(local_tmp), remote_staging)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Config upload failed: {exc}", "error")
                return False
            self._advance()  # uploaded

            self.log("Applying config and restarting matrix-api...", "detail")
            if not self._apply_config_and_restart_service(client, room):
                self.log("Config apply failed (service restart failed).", "error")
                return False
            self._advance()  # applied
            self.log(f"OR {room.number}: edited config deployed and matrix-api restarted.", "success")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def set_log_level(self, room: Room, level: str = "debug") -> bool:
        """Read the room's matrix.api.config.json, set every
        ``logConfig.streams[].level`` to ``level``, push it back, and restart
        matrix-api so the new logging level takes effect.

        Only the log level is touched; every other field in the existing
        config is preserved exactly as-is.
        """
        conn = self.config.connection
        self.log(
            f"=== OR {room.number}: Setting log level to '{level}' ===", "info"
        )
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False

        try:
            self._begin(3)

            # 1. Read the current config off the room.
            self.log("Reading current matrix.api.config.json...", "detail")
            prefix = self._read_sudo_prefix()
            cmd = f"{prefix} cat {shlex.quote(conn.remote_config_path)}".strip()
            collected: List[str] = []
            exit_status = run_command(client, cmd, on_line=collected.append)
            if exit_status != 0:
                self.log("Failed to read config file.", "error")
                return False

            raw = "\n".join(collected)
            if not raw.strip():
                self.log(
                    "Config file read returned no output (permission denied, "
                    "empty file, or a dropped SSH session are the usual causes).",
                    "error",
                )
                return False
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                self.log(f"Could not parse remote config as JSON: {exc}", "error")
                preview = raw if len(raw) <= 500 else raw[:500] + "... (truncated)"
                self.log(f"Raw output was: {preview!r}", "detail")
                return False
            self._advance()  # read

            # 2. Update logConfig.streams[].level in place.
            streams = data.get("logConfig", {}).get("streams")
            if not isinstance(streams, list) or not streams:
                self.log(
                    "Config has no logConfig.streams to update.", "error"
                )
                return False
            for stream in streams:
                if isinstance(stream, dict):
                    stream["level"] = level

            local_tmp = Path(tempfile.gettempdir()) / f"or{room.number}-loglevel.json"
            local_tmp.write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )

            remote_staging = f"/home/{conn.ssh_username}/or{room.number}.json"
            self.log("Uploading updated config...", "detail")
            try:
                upload_file(client, str(local_tmp), remote_staging)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Config upload failed: {exc}", "error")
                return False
            self._advance()  # uploaded

            # 3. Apply and restart matrix-api.
            self.log("Applying config and restarting matrix-api...", "detail")
            if not self._apply_config_and_restart_service(client, room):
                self.log("Config apply failed (service restart failed).", "error")
                return False

            self.log(
                f"OR {room.number}: log level set to '{level}' and matrix-api restarted.",
                "success",
            )
            self._advance()  # applied
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _read_remote_matrix_config(
        self, client: paramiko.SSHClient
    ) -> Optional[dict]:
        """Read and JSON-parse the room's matrix.api.config.json off the
        already-connected ``client``. Returns ``None`` (after logging the
        reason) on any failure."""
        conn = self.config.connection
        self.log("Reading current matrix.api.config.json...", "detail")
        prefix = self._read_sudo_prefix()
        cmd = f"{prefix} cat {shlex.quote(conn.remote_config_path)}".strip()
        collected: List[str] = []
        exit_status = run_command(client, cmd, on_line=collected.append)
        if exit_status != 0:
            self.log("Failed to read config file.", "error")
            return None

        raw = "\n".join(collected)
        if not raw.strip():
            self.log(
                "Config file read returned no output (permission denied, "
                "empty file, or a dropped SSH session are the usual causes).",
                "error",
            )
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            self.log(f"Could not parse remote config as JSON: {exc}", "error")
            preview = raw if len(raw) <= 500 else raw[:500] + "... (truncated)"
            self.log(f"Raw output was: {preview!r}", "detail")
            return None

    def _upload_and_apply_matrix_config(
        self, client: paramiko.SSHClient, room: Room, data: dict, tmp_name: str
    ) -> bool:
        """Write ``data`` to a local temp file, upload it to the room, and
        apply it + restart matrix-api. Does not advance/begin milestones -
        callers own their own step accounting."""
        conn = self.config.connection
        local_tmp = Path(tempfile.gettempdir()) / tmp_name
        local_tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        remote_staging = f"/home/{conn.ssh_username}/or{room.number}.json"
        self.log("Uploading updated config...", "detail")
        try:
            upload_file(client, str(local_tmp), remote_staging)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Config upload failed: {exc}", "error")
            return False

        self.log("Applying config and restarting matrix-api...", "detail")
        if not self._apply_config_and_restart_service(client, room):
            self.log("Config apply failed (service restart failed).", "error")
            return False
        return True

    @staticmethod
    def format_config_path(path: List[Any]) -> str:
        out = ""
        for key in path:
            out += f"[{key}]" if isinstance(key, int) else (f".{key}" if out else str(key))
        return out

    @staticmethod
    def _apply_config_change(data: Any, change: Dict[str, Any]) -> Optional[bool]:
        """Apply one ``{"op", "path", "value"}`` change to ``data`` in place.
        ``set``/``delete`` target the field at ``path``; ``add_item`` /
        ``remove_item`` add or remove ``value`` in the list at ``path`` (so a
        room's other list entries are kept). Returns True if something
        changed, False if it was already in the desired state, or None if the
        path doesn't fit this room's config (missing index, type mismatch)."""
        path, op = change["path"], change["op"]
        parent = data
        for key in path[:-1]:
            if isinstance(parent, dict) and isinstance(key, str):
                if key not in parent:
                    if op in ("delete", "remove_item"):
                        return False
                    parent[key] = {}
                parent = parent[key]
            elif isinstance(parent, list) and isinstance(key, int) and 0 <= key < len(parent):
                parent = parent[key]
            else:
                return None
        last = path[-1]
        if op in ("add_item", "remove_item"):
            if not (isinstance(parent, dict) and isinstance(last, str)):
                return None
            items = parent.get(last, _MISSING)
            if items is _MISSING:
                if op == "remove_item":
                    return False
                items = parent[last] = []
            if not isinstance(items, list):
                return None
            value = change.get("value")
            if op == "add_item":
                if value in items:
                    return False
                items.append(value)
                return True
            if value not in items:
                return False
            parent[last] = [v for v in items if v != value]
            return True
        if isinstance(parent, dict) and isinstance(last, str):
            if op == "delete":
                return parent.pop(last, _MISSING) is not _MISSING
            if last in parent and parent[last] == change.get("value"):
                return False
            parent[last] = change.get("value")
            return True
        if isinstance(parent, list) and isinstance(last, int) and 0 <= last < len(parent):
            if op == "delete":
                return None  # index deletes would shift other entries; not supported
            if parent[last] == change.get("value"):
                return False
            parent[last] = change.get("value")
            return True
        return None

    def patch_matrix_config(self, room: Room, changes: List[Dict[str, Any]]) -> bool:
        """Apply field-level ``changes`` (from the config editor's diff) to
        this room's OWN matrix.api.config.json, leaving every other field -
        including room-specific values - untouched, then push and restart
        matrix-api. Nothing is pushed if any change doesn't fit this room's
        config, or if the room already has every value (no needless restart)."""
        self.log(f"=== OR {room.number}: Applying {len(changes)} config change(s) ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(2)
            data = self._read_remote_matrix_config(client)
            if data is None:
                return False
            self._advance()  # read

            changed = 0
            for change in changes:
                label = self.format_config_path(change["path"])
                result = self._apply_config_change(data, change)
                if result is None:
                    self.log(
                        f"OR {room.number}: '{label}' doesn't exist in the same shape on this room - "
                        "skipped the room, nothing was changed.",
                        "error",
                    )
                    return False
                if result:
                    changed += 1
                    value = json.dumps(change.get("value"))
                    what = {"delete": "removed", "add_item": f"+ {value}", "remove_item": f"- {value}"}.get(
                        change["op"], f"= {value}"
                    )
                    self.log(f"  {label} {what}", "detail")
                else:
                    self.log(f"  {label} already up to date", "detail")

            if not changed:
                self.log(f"OR {room.number}: already matches - no push, no restart.", "success")
                self._advance()
                return True
            if not self._upload_and_apply_matrix_config(
                client, room, data, f"or{room.number}-patched.json"
            ):
                return False
            self._advance()  # applied
            self.log(
                f"OR {room.number}: {changed} change(s) applied and matrix-api restarted.", "success"
            )
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _merge_trusted_endpoints(
        self, room: Room, endpoints: List[str], context: str
    ) -> bool:
        """Read the room's matrix.api.config.json, merge ``endpoints`` into
        ``apiServer.trustedEndPoints`` (skipping any already present), push it
        back, and restart matrix-api so the change takes effect.

        Only ``apiServer.trustedEndPoints`` is touched; every other field in
        the existing config is preserved exactly as-is. No-op (still
        restarts) if every endpoint is already trusted. ``context`` is used
        only for logging (e.g. "trusted endpoint 'x'" or "interop origins").
        """
        self.log(f"=== OR {room.number}: Adding {context} ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False

        try:
            self._begin(3)

            data = self._read_remote_matrix_config(client)
            if data is None:
                return False
            self._advance()  # read

            # Merge endpoints into apiServer.trustedEndPoints in place.
            api_server = data.setdefault("apiServer", {})
            trusted = api_server.setdefault("trustedEndPoints", [])
            if not isinstance(trusted, list):
                self.log("apiServer.trustedEndPoints is not a list; aborting.", "error")
                return False
            added = [e for e in endpoints if e not in trusted]
            trusted.extend(added)
            if added:
                self.log(f"Adding: {', '.join(added)}", "detail")
            else:
                self.log("All endpoints already trusted.", "detail")

            if not self._upload_and_apply_matrix_config(
                client, room, data, f"or{room.number}-trustedendpoints.json"
            ):
                return False
            self._advance()  # uploaded/applied

            self.log(
                f"OR {room.number}: trustedEndPoints updated and matrix-api restarted.",
                "success",
            )
            self._advance()
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def add_trusted_endpoint(self, room: Room, endpoint: str) -> bool:
        """Add a single ad-hoc endpoint to apiServer.trustedEndPoints and
        restart matrix-api. See ``_merge_trusted_endpoints`` for details."""
        return self._merge_trusted_endpoints(room, [endpoint], f"trusted endpoint '{endpoint}'")

    # Static apiServer fields that must point at the web app's actual
    # installed dist/support-dump locations, plus the pairing key. Same
    # value on every room.
    WEB_APP_CONFIG_FIELDS = {
        "helpFolder": "/opt/matrix-api-app/dist/arthrex-synergy-matrix",
        "appFolder": "/opt/matrix-api-app/dist/arthrex-synergy-matrix",
        "supportBundlePath": "/temp/supportDump",
        "masterPairKey": "1234",
    }

    def configure_web_app(self, room: Room) -> bool:
        """One-shot web app setup: merges every room's externally-reachable
        API origin into apiServer.trustedEndPoints AND points
        apiServer.helpFolder/appFolder/supportBundlePath at the correct
        locations plus sets apiServer.masterPairKey, in a single
        read/upload/restart cycle.

        Neither half is useful on its own - the trusted origin lets a
        browser reach the API through the router's forwarded port without a
        403, and the folder paths are what that API then serves - so both
        are always applied together instead of as two separate actions.
        Every other existing field in the config is preserved as-is.
        """
        self.log(f"=== OR {room.number}: Web App Configuration ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False

        try:
            self._begin(3)

            data = self._read_remote_matrix_config(client)
            if data is None:
                return False
            self._advance()  # read

            api_server = data.setdefault("apiServer", {})

            # 1. Trusted origins - every room's externally-reachable API URL.
            router_ip = self.config.connection.router_ip
            endpoints = [r.external_api_url(router_ip) for r in self.config.rooms]
            trusted = api_server.setdefault("trustedEndPoints", [])
            if not isinstance(trusted, list):
                self.log("apiServer.trustedEndPoints is not a list; aborting.", "error")
                return False
            added = [e for e in endpoints if e not in trusted]
            trusted.extend(added)
            if added:
                self.log(f"Adding trusted origins: {', '.join(added)}", "detail")
            else:
                self.log("All trusted origins already present.", "detail")

            # 2. Web app dist/support-dump paths.
            changed = []
            for key, value in self.WEB_APP_CONFIG_FIELDS.items():
                old = api_server.get(key)
                if old != value:
                    changed.append(f"{key}: {old!r} -> {value!r}")
                api_server[key] = value
            if changed:
                self.log("Updating: " + "; ".join(changed), "detail")
            else:
                self.log("Web app paths already set to the desired values.", "detail")

            if not self._upload_and_apply_matrix_config(
                client, room, data, f"or{room.number}-webappconfig.json"
            ):
                return False
            self._advance()  # uploaded/applied

            self.log(
                f"OR {room.number}: web app configuration updated and matrix-api restarted.",
                "success",
            )
            self._advance()
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def reboot(self, room: Room) -> bool:
        """Connect to a room, trigger a reboot, and wait for it to come back."""
        self.log(f"=== OR {room.number}: Rebooting ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            sudo = self._sudo_prefix()
            self.log("Sending reboot command...", "detail")
            # exec_command returns immediately; the reboot will terminate the
            # SSH session from the remote side.
            client.exec_command(f"{sudo} reboot", get_pty=True)
            # Give the command a moment to start before we close the local handle.
            time.sleep(2)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

        self.log("Waiting for system to come back online...", "info")
        if wait_for_reboot(self._target(room), self.log, self.is_cancelled):
            self.log("System is back online.", "success")
            return True
        self.log("System did not come back online within timeout.", "error")
        return False

    def shutdown(self, room: Room) -> bool:
        """Connect to a room and trigger a shutdown (power off, no reboot)."""
        self.log(f"=== OR {room.number}: Shutting down ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            sudo = self._sudo_prefix()
            self.log("Sending shutdown command...", "detail")
            # exec_command returns immediately; the shutdown will terminate the
            # SSH session from the remote side.
            client.exec_command(f"{sudo} shutdown -h now", get_pty=True)
            # Give the command a moment to start before we close the local handle.
            time.sleep(2)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

        self.log(f"OR {room.number}: shutdown command sent.", "success")
        return True

    def deploy_golden_nms_config(self, room: Room, bandwidth: str) -> bool:
        """Push the bundled golden ``nms-config.json`` (MAX or LIMITED bandwidth)
        to a room and restart the service so it takes effect."""
        self.log(
            f"=== OR {room.number}: Setting videoSourceSharing bandwidth to {bandwidth} ===",
            "info",
        )
        try:
            golden_file = golden_nms_config_path(bandwidth)
        except ValueError as exc:
            self.log(str(exc), "error")
            return False
        if not golden_file.exists():
            self.log(f"Golden nms-config file not found: {golden_file}", "error")
            return False

        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False

        try:
            self._begin(2)
            conn = self.config.connection
            remote_staging = f"/home/{conn.ssh_username}/nms-config-or{room.number}.json"

            self.log(f"Uploading {golden_file.name}...", "detail")
            try:
                upload_file(client, str(golden_file), remote_staging)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Upload failed: {exc}", "error")
                return False
            self._advance()  # uploaded

            sudo = self._sudo_prefix()
            self.log("Applying nms-config.json and restarting service...", "detail")
            cmd = (
                f"{sudo} cp {shlex.quote(remote_staging)} {shlex.quote(conn.remote_nms_config_path)} "
                f"&& {sudo} systemctl restart {shlex.quote(conn.service_name)} "
                f"&& {sudo} systemctl --no-pager --full status {shlex.quote(conn.service_name)} -n 10"
            )
            exit_status = run_command(
                client, cmd, get_pty=True, on_line=lambda l: self.log(l, "detail")
            )
            if exit_status != 0:
                self.log("Failed to apply nms-config.json (service restart failed).", "error")
                return False
            self._advance()  # applied

            self.log(f"OR {room.number}: bandwidth set to {bandwidth}.", "success")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def deploy_nms_link_bandwidth(
        self, room: Room, bandwidth_kbps: int, remove_overlay: bool = False
    ) -> bool:
        """Render and push ``application-user.yml`` with the given interop link
        bandwidth (kbps, applied to both upload and download) and restart
        the barco-nms service so it takes effect.

        ``remove_overlay`` is independent of the bandwidth value - it only
        controls whether ``nexxis.overlay.noVideoOverlayId`` is included."""
        if not remove_overlay:
            self.log(
                f"=== OR {room.number}: Setting NMS interop bandwidth to {bandwidth_kbps} ===",
                "info",
            )
        try:
            content = render_nms_user_config(room, bandwidth_kbps, remove_overlay)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Failed to render application-user.yml: {exc}", "error")
            return False

        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False

        tmp_path: Optional[str] = None
        try:
            self._begin(2)
            conn = self.config.connection
            remote_staging = f"/home/{conn.ssh_username}/application-user-or{room.number}.yml"

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yml", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            self.log("Uploading application-user.yml...", "detail")
            try:
                upload_file(client, tmp_path, remote_staging)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Upload failed: {exc}", "error")
                return False
            self._advance()  # uploaded

            sudo = self._sudo_prefix()
            self.log("Applying application-user.yml and restarting barco-nms...", "detail")
            cmd = (
                f"{sudo} cp {shlex.quote(remote_staging)} {shlex.quote(conn.remote_nms_user_config_path)} "
                f"&& {sudo} systemctl reset-failed {shlex.quote(conn.nms_service_name)} "
                f"&& {sudo} systemctl restart {shlex.quote(conn.nms_service_name)} "
                f"&& {sudo} systemctl --no-pager --full status {shlex.quote(conn.nms_service_name)} -n 10"
            )
            exit_status = run_command(
                client, cmd, get_pty=True, on_line=lambda l: self.log(l, "detail")
            )
            if exit_status != 0:
                self.log("Failed to apply application-user.yml (service restart failed).", "error")
                return False
            self._advance()  # applied

            if remove_overlay:
                self.log(f"OR {room.number}: video overlay removed.", "success")
            else:
                self.log(f"OR {room.number}: NMS interop bandwidth set to {bandwidth_kbps}.", "success")
            return True
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _read_current_nms_bandwidth(self, room: Room) -> Optional[int]:
        """Best-effort read of the room's currently-applied interop bandwidth
        from the live ``application-user.yml``, so actions that only need to
        change one setting (e.g. the overlay flag) don't have to guess/reset
        the other."""
        try:
            client = connect(self._target(room))
        except SSHError:
            return None
        try:
            lines: List[str] = []
            prefix = self._read_sudo_prefix()
            cmd = f"{prefix} cat {shlex.quote(self.config.connection.remote_nms_user_config_path)}".strip()
            exit_status = run_command(client, cmd, on_line=lines.append)
            if exit_status != 0:
                return None
            match = re.search(r"upload:\s*(\d+)", "\n".join(lines))
            return int(match.group(1)) if match else None
        except Exception:  # noqa: BLE001
            return None
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def deploy_nms_remove_overlay(self, room: Room, bandwidth_kbps: int = 500000) -> bool:
        """Push application-user.yml with nexxis.overlay.noVideoOverlayId =
        matrixEmptyOverlay and restart barco-nms so the video overlay is
        removed, without changing the room's current interop bandwidth."""
        self.log(f"=== OR {room.number}: Removing video overlay ===", "info")
        current = self._read_current_nms_bandwidth(room)
        if current is not None:
            bandwidth_kbps = current
        else:
            self.log(
                f"Could not read current interop bandwidth; leaving it at {bandwidth_kbps}.",
                "warning",
            )
        return self.deploy_nms_link_bandwidth(room, bandwidth_kbps, remove_overlay=True)

    def deploy_matrix_api_certs(self, room: Room) -> bool:
        """Disable the cert-init/unseal units, generate a fresh self-signed
        matrix.api server cert/key pair, fix ownership/permissions, and
        restart the matrix-api service so it picks up the new cert."""
        self.log(f"=== OR {room.number}: Regenerating matrix-api certs ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            conn = self.config.connection
            sudo = self._sudo_prefix()
            cert_dir = "/usr/lib/node_modules/matrix.api"
            key_path = f"{cert_dir}/server-key.pem"
            cert_path = f"{cert_dir}/server-cert.pem"
            subj = "/C=US/O=Arthrex/OU=Engineering/CN=Arthrex Matrix API"
            san = (
                "subjectAltName=DNS:$(hostname),DNS:$(hostname -s),"
                "DNS:localhost,IP:127.0.0.1,IP:::1"
            )
            cmd = (
                f"{sudo} systemctl disable --now "
                f"matrix-api-certs-init.service matrix-api-certs-unseal.service "
                f"&& {sudo} install -d -m 0755 {shlex.quote(cert_dir)} "
                f"&& {sudo} openssl req -x509 -newkey rsa:2048 -nodes -days 3650 "
                f"-keyout {shlex.quote(key_path)} -out {shlex.quote(cert_path)} "
                f'-subj "{subj}" '
                f'-addext "{san}" '
                f'-addext "keyUsage=critical,digitalSignature,keyEncipherment" '
                f'-addext "extendedKeyUsage=serverAuth" '
                f"&& {sudo} chown act-app:act-app {shlex.quote(key_path)} {shlex.quote(cert_path)} "
                f"&& {sudo} chmod 600 {shlex.quote(key_path)} "
                f"&& {sudo} chmod 644 {shlex.quote(cert_path)} "
                f"&& {sudo} sed -i "
                f"'s/Requires=matrix-api-certs-unseal\\.service/#Requires=matrix-api-certs-unseal.service/' "
                f"/usr/lib/systemd/system/matrix-api.service "
                f"&& {sudo} systemctl restart {shlex.quote(conn.service_name)}"
            )
            exit_status = run_command(
                client, cmd, get_pty=True, on_line=lambda l: self.log(l, "detail")
            )
            self._advance()
            if exit_status != 0:
                self.log("Failed to regenerate matrix-api certs.", "error")
                return False
            self.log(f"OR {room.number}: matrix-api certs regenerated.", "success")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def fix_room_config_race(self, room: Room) -> bool:
        """Patch matrix-room-config-generator.service's ``After=`` ordering to
        also wait on barco-nms-network-init.service.

        Without this, matrix-room-config-generator can start before barco-nms
        has assigned the room's IP, causing it to grab the wrong/incorrect
        address (race condition). The sed is idempotent - a second run is a
        harmless no-op once the line has already been patched. Takes effect
        on the unit's next start (e.g. next reboot); does not restart
        anything itself.
        """
        self.log(
            f"=== OR {room.number}: Patching matrix-room-config-generator "
            "service ordering ===",
            "info",
        )
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            sudo = self._sudo_prefix()
            unit = "/usr/lib/systemd/system/matrix-room-config-generator.service"
            cmd = (
                f"{sudo} sed -i "
                f"'s/^After=barco-nms\\.service redis\\.service "
                f"act-kiosk-redis-svc\\.service$/After=barco-nms.service "
                f"barco-nms-network-init.service redis.service "
                f"act-kiosk-redis-svc.service/' {shlex.quote(unit)} "
                f"&& {sudo} systemctl daemon-reload "
                f"&& grep -n '^After=' {shlex.quote(unit)}"
            )
            exit_status = run_command(
                client, cmd, get_pty=True, on_line=lambda l: self.log(l, "detail")
            )
            self._advance()
            if exit_status != 0:
                self.log("Failed to patch matrix-room-config-generator.service.", "error")
                return False
            self.log(
                f"OR {room.number}: race-condition fix applied "
                "(takes effect next start/reboot).",
                "success",
            )
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def remove_known_hosts_entry(self, room: Room) -> bool:
        """Remove any cached SSH host key for this room's host:port from the
        local known_hosts file (equivalent to ``ssh-keygen -R "[host]:port"``).

        Useful when the remote host key has changed and a manual ``ssh``/``scp``
        connection from this machine fails with "REMOTE HOST IDENTIFICATION
        HAS CHANGED". This is a local-only operation; it does not connect to
        the room.
        """
        conn = self.config.connection
        port = room.ssh_port(conn.ssh_port_base)
        target = f"[{conn.router_ip}]:{port}"
        self.log(
            f"=== OR {room.number}: Removing cached SSH fingerprint for {target} ===",
            "info",
        )
        try:
            result = subprocess.run(
                ["ssh-keygen", "-R", target],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            self.log(
                "ssh-keygen not found on this system (requires the OpenSSH client).",
                "error",
            )
            return False
        except Exception as exc:  # noqa: BLE001
            self.log(f"Failed to run ssh-keygen: {exc}", "error")
            return False

        output = ((result.stdout or "") + (result.stderr or "")).strip()
        for line in output.splitlines():
            self.log(line, "detail")

        if result.returncode != 0:
            self.log(f"ssh-keygen exited with code {result.returncode}.", "error")
            return False

        self.log(f"OR {room.number}: fingerprint entry removed (if it existed).", "success")
        return True

    # -- Web app (Matrix Electron web app + matrix.api backend) -----------
    # Ported from the standalone matrix-electron-web-deployer tool: deploys
    # locally-built artifacts (see webapp_builder.build_repo for the build
    # step) rather than an SWU, and targets the same room/router topology.

    def deploy_web_app(self, room: Room, local_dist: Path, local_web: Path) -> bool:
        """Deploy locally-built backend dist + web assets to a room over SSH:
        upload, install under the app folder + node_modules, patch the
        systemd unit and matrix.api.config.json to point at the new dist
        build, then restart matrix-api."""
        conn = self.config.connection
        local_dist = Path(local_dist)
        local_web = Path(local_web)
        self.log(f"=== OR {room.number}: Web app deploy ===", "info")
        if not local_dist.exists():
            self.log(f"Local backend dist not found: {local_dist}", "error")
            return False
        if not local_web.exists():
            self.log(f"Local web assets not found: {local_web}", "error")
            return False

        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False

        remote_tmp = f"/tmp/or{room.number}-webapp-upgrade"
        try:
            self._begin(4)
            run_command(client, f"rm -rf {shlex.quote(remote_tmp)}")
            run_command(client, f"mkdir -p {shlex.quote(remote_tmp)}")

            self.log("Uploading web assets...", "detail")
            upload_dir(client, str(local_web), f"{remote_tmp}/web", self.log)
            self._advance()  # web assets uploaded

            self.log("Uploading backend dist...", "detail")
            upload_dir(client, str(local_dist), f"{remote_tmp}/dist", self.log)
            self._advance()  # backend dist uploaded

            self.log("Installing files and patching config...", "detail")
            app_folder = conn.remote_webapp_app_folder
            node_module = conn.remote_webapp_node_module
            install_cmd = (
                f"mkdir -p {app_folder}/dist && "
                f"rm -rf {app_folder}/dist/arthrex-synergy-matrix && "
                f"cp -r {remote_tmp}/web {app_folder}/dist/arthrex-synergy-matrix && "
                f"chmod -R 755 {app_folder}/ && "
                f"rm -rf {node_module}/dist && "
                f"cp -r {remote_tmp}/dist {node_module}/dist && "
                f"chmod -R 755 {node_module}/dist/ && "
                f"sed -i 's|index.js|dist/server.js|g' {conn.webapp_service_unit_path} && "
                f"sed -i 's|\"appFolder\": \"{app_folder}\"|\"appFolder\": \"{app_folder}/dist/arthrex-synergy-matrix\"|g' {conn.remote_config_path} && "
                f"sed -i 's|\"helpFolder\": \"{app_folder}\"|\"helpFolder\": \"{app_folder}/dist/arthrex-synergy-matrix\"|g' {conn.remote_config_path} && "
                f"sed -i 's|https://localhost:|https://{conn.router_ip}:|g' {conn.remote_config_path} && "
                f"systemctl daemon-reload && "
                f"rm -rf {remote_tmp}"
            )
            sudo = self._sudo_prefix()
            exit_status = run_command(
                client, f"{sudo} bash -lc {shlex.quote(install_cmd)}", get_pty=True,
                on_line=lambda l: self.log(l, "detail"),
            )
            self._advance()  # installed/patched
            if exit_status != 0:
                self.log("Install/patch step failed.", "error")
                return False

            self.log("Restarting matrix-api...", "detail")
            if not self._restart_service(client, room, conn.service_name):
                self.log("Service restart failed.", "error")
                return False
            self._advance()  # restarted

            self.log(
                f"OR {room.number}: web app deployed. "
                f"Test: https://{conn.router_ip}:100{room.number:02d}/app/",
                "success",
            )
            return True
        finally:
            run_command(client, f"rm -rf {shlex.quote(remote_tmp)}")
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def reset_web_app(self, room: Room) -> bool:
        """Undo a previous web app deploy: remove staged/deployed files and
        flip the systemd unit back to the original entrypoint (index.js), so
        the room is ready for a fresh deploy."""
        conn = self.config.connection
        self.log(f"=== OR {room.number}: Web app reset ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(2)
            sudo = self._sudo_prefix()

            cleanup_cmd = (
                f"rm -rf /tmp/or{room.number}-* /tmp/v14-dist-fix-* /tmp/matrix-* "
                f"{conn.remote_webapp_app_folder}/* && echo CLEANUP_OK"
            )
            cleanup_out: List[str] = []
            run_command(
                client, f"{sudo} bash -lc {shlex.quote(cleanup_cmd)}", get_pty=True,
                on_line=cleanup_out.append,
            )
            self.log("\n".join(cleanup_out), "detail")
            self._advance()  # cleaned up
            if not any("CLEANUP_OK" in l for l in cleanup_out):
                self.log("Cleanup may have failed.", "warning")

            service_cmd = (
                f"sed -i 's|dist/server.js|index.js|g' {conn.webapp_service_unit_path} && "
                f"systemctl daemon-reload && echo SERVICE_RESET_OK"
            )
            service_out: List[str] = []
            run_command(
                client, f"{sudo} bash -lc {shlex.quote(service_cmd)}", get_pty=True,
                on_line=service_out.append,
            )
            self.log("\n".join(service_out), "detail")
            self._advance()  # service reset
            if not any("SERVICE_RESET_OK" in l for l in service_out):
                self.log("Systemd service reset may have failed.", "warning")

            self.log(f"OR {room.number}: web app reset complete; ready for a fresh deploy.", "success")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def diagnose_web_app(self, room: Room) -> bool:
        """Read-only diagnostic dump for the deployed web assets: directory
        listings, matrix.api.config.json appFolder/helpFolder, matrix-api
        service status/journal, and AppArmor denials."""
        conn = self.config.connection
        self.log(f"=== OR {room.number}: Web app diagnostics ===", "info")
        try:
            client = connect(self._target(room))
        except SSHError as exc:
            self.log(str(exc), "error")
            return False
        try:
            self._begin(1)
            prefix = self._read_sudo_prefix()
            web_root = f"{conn.remote_webapp_app_folder}/dist/arthrex-synergy-matrix"
            sections = [
                ("Web assets root listing", f"ls -la {shlex.quote(web_root)}/"),
                ("app/ subfolder listing", f"ls -la {shlex.quote(web_root)}/app/ 2>&1"),
                ("Recursive tree (depth-limited)",
                 f"find {shlex.quote(web_root)} -maxdepth 3 -exec ls -ld {{}} \\;"),
                ("matrix.api.config.json appFolder/helpFolder",
                 f"grep -E 'appFolder|helpFolder' {shlex.quote(conn.remote_config_path)}"),
                ("matrix-api service status",
                 f"systemctl status {shlex.quote(conn.service_name)} --no-pager -l | head -20"),
                ("matrix-api recent journal (last 60 lines)",
                 f"{prefix} journalctl -u {shlex.quote(conn.service_name)} -n 60 --no-pager".strip()),
                ("AppArmor status", f"{prefix} aa-status 2>&1 | head -30".strip()),
                ("Recent AppArmor DENIED entries (dmesg)",
                 f"{prefix} dmesg 2>&1 | grep -i apparmor | tail -30".strip()),
            ]
            out_lines: List[str] = []
            for title, cmd in sections:
                lines: List[str] = []
                run_command(client, cmd, get_pty=True, on_line=lines.append)
                out_lines.append(f"OR {room.number}: --- {title} ---")
                out_lines.extend(f"OR {room.number}: {line}" for line in lines)
            self._advance()
            self.log("\n".join(out_lines), "detail")
            return True
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _sudo_prefix(self) -> str:
        """Return a sudo invocation that supplies the password when available."""
        if self.creds.sudo_password:
            # -S reads the password from stdin; -p '' suppresses the prompt text.
            quoted = shlex.quote(self.creds.sudo_password)
            return f"echo {quoted} | sudo -S -p ''"
        return "sudo"
