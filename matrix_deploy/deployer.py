"""Core deployment logic - Qt-free so it is testable and CLI-reusable.

A ``Deployer`` performs SWU updates and/or config deployment to a single room.
It reports progress through plain callbacks (``log`` / ``progress``) and supports
cooperative cancellation via an ``is_cancelled`` callable.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
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
    ):
        self.config = config
        self.creds = creds
        self.log = log
        self.progress = progress
        self.is_cancelled = is_cancelled
        self.milestone = milestone or (lambda done, total: None)
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

        self.log(f"Uploading {swu_file.name}...", "detail")
        try:
            upload_file(client, str(swu_file), remote_path, self.progress)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Upload failed: {exc}", "error")
            return False
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

        run_command(
            client,
            f"swupdate-client -v {shlex.quote(remote_path)}",
            get_pty=True,
            on_line=on_line,
        )

        # Clean up uploaded file (best effort; ignore errors).
        run_command(client, f"rm -f {shlex.quote(remote_path)}")

        if not success["ok"]:
            self.log("SWU update failed - no success message received.", "error")
            return False

        self.log("SWU update successful - system will reboot.", "success")
        self._advance()  # SWU installed
        if not wait_for_reboot(self._target(room), self.log, self.is_cancelled):
            self.log("System did not come back online within timeout.", "error")
            return False
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

    def deploy_nms_link_bandwidth(self, room: Room, bandwidth_kbps: int) -> bool:
        """Render and push ``application-user.yml`` with the given interop link
        bandwidth (kbps, applied to both upload and download) and restart
        the barco-nms service so it takes effect."""
        self.log(
            f"=== OR {room.number}: Setting NMS interop bandwidth to {bandwidth_kbps} ===",
            "info",
        )
        try:
            content = render_nms_user_config(room, bandwidth_kbps)
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

    def deploy_nms_remove_overlay(self, room: Room, bandwidth_kbps: int = 500000) -> bool:
        """Push application-user.yml (which sets nexxis.overlay.noVideoOverlayId =
        myEmptyOverlay) and restart barco-nms so the video overlay is removed."""
        self.log(f"=== OR {room.number}: Removing video overlay ===", "info")
        return self.deploy_nms_link_bandwidth(room, bandwidth_kbps)

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
