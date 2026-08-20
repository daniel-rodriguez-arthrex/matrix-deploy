"""Shared SSH/SCP helpers built on paramiko.

All deployment logic uses this module so connection handling lives in one place.
"""

from __future__ import annotations

import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional

import paramiko
from scp import SCPClient

# Logger callback signature: log(message: str, level: str)
# level is one of: info, success, warning, error, detail
Logger = Callable[[str, str], None]
# Progress callback signature: progress(sent: int, total: int)
Progress = Callable[[int, int], None]


@dataclass
class SSHTarget:
    host: str
    port: int
    username: str
    password: Optional[str] = None


class SSHError(Exception):
    """Raised when an SSH operation fails."""


# Requesting a PTY (needed for sudo password prompts and colorized/paged
# tools like swupdate-client) makes remote programs think they're talking to
# a real terminal, so things like ``systemctl status`` emit ANSI color codes
# and OSC 8 hyperlink escapes. Our log views are plain text, so strip both
# before handing lines to callers instead of showing the raw escape bytes.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")
_OSC_HYPERLINK_RE = re.compile(r"\x1b\]8;[^\x1b]*\x1b\\")


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI color/style codes and OSC 8 hyperlink escapes."""
    text = _OSC_HYPERLINK_RE.sub("", text)
    return _ANSI_CSI_RE.sub("", text)


def connect(target: SSHTarget, timeout: int = 10) -> paramiko.SSHClient:
    """Connect using an SSH key first, falling back to password.

    Raises ``SSHError`` with a descriptive message if both methods fail.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_error: Optional[Exception] = None
    try:
        client.connect(
            hostname=target.host,
            port=target.port,
            username=target.username,
            timeout=timeout,
            look_for_keys=True,
            allow_agent=True,
        )
        return client
    except Exception as exc:  # noqa: BLE001 - we re-raise with context below
        key_error = exc

    if not target.password:
        raise SSHError(
            f"SSH key auth failed for {target.username}@{target.host}:{target.port} "
            f"and no password was provided ({key_error})"
        )

    try:
        client.connect(
            hostname=target.host,
            port=target.port,
            username=target.username,
            password=target.password,
            timeout=timeout,
        )
        return client
    except Exception as exc:  # noqa: BLE001
        raise SSHError(
            f"SSH auth failed for {target.username}@{target.host}:{target.port}: {exc}"
        ) from exc


def run_command(
    client: paramiko.SSHClient,
    command: str,
    get_pty: bool = False,
    on_line: Optional[Callable[[str], None]] = None,
) -> int:
    """Run a remote command, optionally streaming stdout line-by-line.

    Returns the remote exit status. stderr is merged into the stream when
    ``get_pty`` is True; otherwise it is appended after stdout.
    """
    stdin, stdout, stderr = client.exec_command(command, get_pty=get_pty)
    if on_line is not None:
        for raw in stdout:
            # PTY sessions terminate lines with "\r\n"; strip both so a
            # trailing "\r" doesn't render as a blank line downstream.
            line = raw.rstrip("\r\n")
            if get_pty:
                # PTY output may carry ANSI color/hyperlink escapes (e.g.
                # from `systemctl status`) that render as garbage in our
                # plain-text log views.
                line = strip_ansi(line)
            on_line(line)
    exit_status = stdout.channel.recv_exit_status()
    if on_line is not None and not get_pty:
        err = stderr.read().decode(errors="replace").strip()
        if err:
            on_line(err)
    return exit_status


def upload_file(
    client: paramiko.SSHClient,
    local_path: str,
    remote_path: str,
    progress: Optional[Progress] = None,
    log: Optional[Logger] = None,
) -> None:
    """Upload a file via SCP with an optional, rate-limited progress callback.

    ``scp`` invokes its progress callback once per internal read chunk (as
    small as 16KB), which can fire tens of thousands of times for a large
    SWU file. Forwarding every call straight through (e.g. to a Qt signal)
    floods the receiver - especially with several rooms uploading in
    parallel - and can make the UI appear to hang even though the transfer
    itself is progressing fine. Throttle to a sane update rate instead.

    When ``log`` is provided, the total elapsed time and average throughput
    are reported once the transfer finishes, so we can tell whether a slow
    deploy is the SCP transfer itself (vs. the later swupdate install step).
    """
    last_emit = 0.0

    def _cb(filename, size, sent):  # noqa: ANN001 - paramiko/scp signature
        nonlocal last_emit
        if progress is None:
            return
        now = time.monotonic()
        if sent >= size or now - last_emit >= 0.15:
            last_emit = now
            progress(sent, size)

    try:
        size_bytes = os.path.getsize(local_path)
    except OSError:
        size_bytes = 0

    start = time.monotonic()
    with SCPClient(client.get_transport(), progress=_cb if progress else None) as scp:
        scp.put(local_path, remote_path)
    elapsed = time.monotonic() - start

    if log is not None:
        size_mb = size_bytes / (1024 * 1024)
        rate = (size_mb / elapsed) if elapsed > 0 else 0.0
        log(
            f"Transferred {size_mb:.1f} MB in {elapsed:.1f}s "
            f"({rate:.2f} MB/s).",
            "detail",
        )


def download_file(
    client: paramiko.SSHClient,
    remote_path: str,
    local_path: str,
    progress: Optional[Progress] = None,
    log: Optional[Logger] = None,
) -> None:
    """Download a file via SCP with an optional, rate-limited progress
    callback. Mirrors ``upload_file``'s throttling/logging behavior."""
    last_emit = 0.0

    def _cb(filename, size, sent):  # noqa: ANN001 - paramiko/scp signature
        nonlocal last_emit
        if progress is None:
            return
        now = time.monotonic()
        if sent >= size or now - last_emit >= 0.15:
            last_emit = now
            progress(sent, size)

    start = time.monotonic()
    with SCPClient(client.get_transport(), progress=_cb if progress else None) as scp:
        scp.get(remote_path, local_path)
    elapsed = time.monotonic() - start

    if log is not None:
        try:
            size_mb = os.path.getsize(local_path) / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        rate = (size_mb / elapsed) if elapsed > 0 else 0.0
        log(
            f"Downloaded {size_mb:.1f} MB in {elapsed:.1f}s ({rate:.2f} MB/s).",
            "detail",
        )


def get_disk_free_kb(client: paramiko.SSHClient, path: str = "/tmp") -> Optional[int]:
    """Return available space (in KiB) on the filesystem containing ``path``.

    Runs ``df -Pk <path>`` (POSIX output format, so column layout is stable
    across BusyBox/coreutils) and parses the "Available" column from the
    second line. Returns ``None`` if the command fails or output can't be
    parsed.
    """
    stdin, stdout, stderr = client.exec_command(f"df -Pk {path}")
    output = stdout.read().decode(errors="replace").strip()
    exit_status = stdout.channel.recv_exit_status()
    if exit_status != 0 or not output:
        return None
    lines = output.splitlines()
    if len(lines) < 2:
        return None
    fields = lines[-1].split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[3])
    except ValueError:
        return None


def port_is_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def wait_for_reboot(
    target: SSHTarget,
    log: Logger,
    is_cancelled: Callable[[], bool],
    max_wait_minutes: int = 5,
    settle_seconds: int = 10,
) -> bool:
    """Wait for a host to reboot and accept SSH again.

    1. Sleep ``settle_seconds`` so the box has time to start going down.
    2. Poll the SSH port; once open, verify a real SSH handshake succeeds.

    Returns True when SSH is reachable again, False on timeout/cancel.
    """
    log("Waiting for system to start rebooting...", "detail")
    for _ in range(settle_seconds):
        if is_cancelled():
            return False
        time.sleep(1)

    log("Waiting for system to come back online...", "detail")
    max_attempts = max_wait_minutes * 12  # poll every 5 seconds

    for attempt in range(max_attempts):
        if is_cancelled():
            return False

        if port_is_open(target.host, target.port):
            time.sleep(5)  # give sshd a moment to fully accept logins
            try:
                client = connect(target, timeout=5)
                client.close()
                return True
            except SSHError:
                pass  # keep polling

        if attempt % 6 == 0:
            elapsed_min = (attempt * 5) // 60
            log(f"  Still waiting... ({elapsed_min} min elapsed)", "detail")

        time.sleep(5)

    return False
