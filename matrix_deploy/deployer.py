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
from typing import Callable, List, Optional

import paramiko

from .config import AppConfig, Room, golden_nms_config_path, render_nms_user_config
from .config_builder import build_room_config
from .ssh_client import (
    SSHError,
    SSHTarget,
    connect,
    download_file,
    get_disk_free_kb,
    run_command,
    upload_file,
    wait_for_reboot,
)

Logger = Callable[[str, str], None]
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
    do_config: bool
    swu_file: Optional[Path] = None
    config_file: Optional[Path] = None  # already-generated per-room JSON
    template_path: Optional[Path] = None  # used to generate config if config_file is not provided
    output_dir: Optional[Path] = None  # used to generate config if config_file is not provided


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

        Always 1 for the connect step; SWU adds upload/install/online (3);
        config adds upload/apply (2).
        """
        total = 1
        if request.do_swu:
            total += 3
        if request.do_config:
            total += 2
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

        # Fetch the room's default sudo password from the EEPROM.  This is
        # unique per room and is also used as the NMS password in the
        # generated matrix.api config.
        room_password = self._fetch_room_password(client, room)
        if request.do_config and not room_password:
            self.log(
                "Could not determine per-room NMS password and config deployment was requested.",
                "error",
            )
            return False

        config_file = request.config_file
        if request.do_config:
            if config_file is not None:
                pass  # use pre-generated config
            elif request.template_path and request.output_dir:
                try:
                    config_file = build_room_config(
                        self.config,
                        room,
                        request.template_path,
                        request.output_dir,
                        room_password,
                    )
                    self.log(f"Generated config: {config_file.name}", "detail")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"Config generation failed for OR {room.number}: {exc}", "error")
                    return False
            else:
                self.log("Config deployment requested but no template or output directory provided.", "error")
                return False

        try:
            if request.do_swu:
                if not self._deploy_swu(client, room, request.swu_file):
                    return False
                # After a reboot the old client is dead - reconnect for config.
                client.close()
                if request.do_config:
                    self.log("Reconnecting after reboot...", "info")
                    try:
                        client = connect(self._target(room))
                    except SSHError as exc:
                        self.log(str(exc), "error")
                        return False
                    # Re-fetch the password after reboot in case the old
                    # connection state was lost.
                    room_password = self._fetch_room_password(client, room)

            if request.do_config:
                if not self._deploy_config(client, room, config_file):
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

    def _deploy_config(
        self,
        client: paramiko.SSHClient,
        room: Room,
        config_file: Optional[Path],
    ) -> bool:
        if not config_file or not Path(config_file).exists():
            self.log(f"Config file not found: {config_file}", "error")
            return False
        config_file = Path(config_file)

        self.log("--- Config Deployment ---", "info")
        conn = self.config.connection
        remote_staging = f"/home/{conn.ssh_username}/or{room.number}.json"

        self.log("Uploading config...", "detail")
        try:
            upload_file(client, str(config_file), remote_staging)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Config upload failed: {exc}", "error")
            return False
        self._advance()  # config uploaded

        self.log("Applying config and restarting service...", "detail")
        if not self._apply_config_and_restart_service(client, room):
            self.log("Config apply failed (service restart failed).", "error")
            return False

        self.log("Config applied and service restarted.", "success")
        self._advance()  # config applied
        return True

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

    def fetch_service_logs(
        self, room: Room, dest_dir: Path, lines: int = 2000
    ) -> Optional[Path]:
        """Collect journald logs for matrix-api and barco-nms and save them to a
        timestamped text file in ``dest_dir``. Returns the file path on success."""
        conn = self.config.connection
        services = [conn.service_name, conn.nms_service_name]
        self.log(f"=== OR {room.number}: Collecting logs ({', '.join(services)}) ===", "info")
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
                self.log(f"Reading last {lines} log lines for {service}...", "detail")
                collected: List[str] = []
                cmd = (
                    f"{prefix} journalctl -u {shlex.quote(service)} --no-pager -n {int(lines)}"
                ).strip()
                run_command(
                    client, cmd, get_pty=True, on_line=lambda l: collected.append(l)
                )
                header = f"{'=' * 70}\n{service} (last {lines} lines)\n{'=' * 70}"
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
            )
            dest.write_text(banner + "\n" + "\n\n".join(sections) + "\n", encoding="utf-8")
            self.log(f"Saved logs to {dest}", "success")
            return dest
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def fetch_full_journal(self, room: Room, dest_dir: Path) -> Optional[Path]:
        """Download the complete systemd journal (unfiltered - every unit,
        every priority, no time window) to a timestamped text file in
        ``dest_dir``. Returns the file path on success; nothing is printed
        to the log terminal besides progress/status.

        The journal is dumped to a temp file on the room and pulled down
        via SCP (with a progress callback) instead of being streamed
        line-by-line through the SSH channel - for a large journal that is
        both much faster and gives real transfer progress instead of an
        indefinite-looking wait.
        """
        conn = self.config.connection
        self.log(f"=== OR {room.number}: Downloading full journal ===", "info")
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
                f"{prefix} journalctl --no-pager > {shlex.quote(remote_tmp)} 2>&1"
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
                f"Generated: {timestamp}\n\n"
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
        self.log(f"=== OR {room.number}: System Errors (last {hours}h) ===", "info")
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
                    f"OR {room.number}: no errors in the last {hours}h.",
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

            exit_status = run_command(client, cmd, get_pty=True, on_line=_on_line)
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
            exit_status = run_command(
                client, cmd, get_pty=True, on_line=collected.append
            )
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
        exit_status = run_command(
            client, cmd, get_pty=True, on_line=collected.append
        )
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

    def _sudo_prefix(self) -> str:
        """Return a sudo invocation that supplies the password when available."""
        if self.creds.sudo_password:
            # -S reads the password from stdin; -p '' suppresses the prompt text.
            quoted = shlex.quote(self.creds.sudo_password)
            return f"echo {quoted} | sudo -S -p ''"
        return "sudo"
