"""Shared SSH/SCP helpers built on paramiko.

All deployment logic uses this module so connection handling lives in one place.
"""

from __future__ import annotations

import io
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

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
    # Fed to commands that use ``sudo_prefix`` (see ``exec_command``).
    sudo_password: Optional[str] = None


class SSHError(Exception):
    """Raised when an SSH operation fails."""


# -- sudo without the password on the command line ----------------------
# ``echo <pw> | sudo -S`` puts the password in the remote shell's command
# line, which any account on the room PC can read (``ps`` / /proc/*/cmdline).
# Instead commands reference a shell variable that the remote shell fills
# from the first line of stdin with the ``read`` builtin.
SUDO_PW_VAR = "MD_SUDO_PW"
_SUDO_PW_REF = f'"${SUDO_PW_VAR}"'


def sudo_prefix(password: Optional[str]) -> str:
    """``sudo`` for a command run through ``exec_command``/``run_command``."""
    return f"printf '%s\\n' {_SUDO_PW_REF} | sudo -S -p ''" if password else "sudo"


def exec_command(client: paramiko.SSHClient, command: str, get_pty: bool = False):
    """``client.exec_command`` that supplies the sudo password over stdin when
    ``command`` uses ``sudo_prefix``. The password comes from the
    ``SSHTarget.sudo_password`` the client was connected with."""
    secret = getattr(client, "md_sudo_password", None)
    uses_secret = _SUDO_PW_REF in command
    if uses_secret:
        if not secret:
            raise SSHError("This command needs the sudo password, but none was provided.")
        command = f"IFS= read -r {SUDO_PW_VAR}; {command}"
    stdin, stdout, stderr = client.exec_command(command, get_pty=get_pty)
    if uses_secret:
        stdin.write(secret + "\n")
        stdin.flush()
    return stdin, stdout, stderr


def redact(client: paramiko.SSHClient, text: str) -> str:
    """Hide the sudo password in output (a PTY echoes what stdin sent)."""
    secret = getattr(client, "md_sudo_password", None)
    return text.replace(secret, "<hidden>") if secret and secret in text else text


# -- host key pinning ---------------------------------------------------
# Trust on first use: the first key seen for host:port is saved; a later
# different key is refused (a re-imaged room, or someone impersonating it to
# collect the passwords we're about to send). Per Windows user, outside the
# app folder so it never ships.
KNOWN_HOSTS = Path(os.environ.get("APPDATA") or Path.home()) / "MatrixFacilityManager" / "known_hosts"
_KNOWN_HOSTS_LOCK = threading.Lock()


def _known_hosts() -> paramiko.HostKeys:
    keys = paramiko.HostKeys()
    if KNOWN_HOSTS.exists():
        keys.load(str(KNOWN_HOSTS))
    return keys


def _save_known_hosts(keys: paramiko.HostKeys) -> None:
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = KNOWN_HOSTS.with_suffix(".tmp")
    keys.save(str(tmp))
    os.replace(tmp, KNOWN_HOSTS)


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        with _KNOWN_HOSTS_LOCK:
            keys = _known_hosts()
            keys.add(hostname, key.get_name(), key)
            _save_known_hosts(keys)
        client.get_host_keys().add(hostname, key.get_name(), key)


def forget_host_key(host: str, port: int) -> bool:
    """Drop the pinned key for host:port (after a room was re-imaged).
    Returns True if one was saved."""
    name = host if port == 22 else f"[{host}]:{port}"
    with _KNOWN_HOSTS_LOCK:
        keys = _known_hosts()
        if name not in keys:
            return False
        del keys[name]
        _save_known_hosts(keys)
    return True


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


# SHA-1 in SSH: the old "ssh-rsa" signature (GHSA-r374-rxx8-8654) and SHA-1
# key exchanges. Refused by default (sha1="refuse"). The rooms run modern
# OpenSSH (ECDSA host keys, SHA-2) and carry the secrets (sudo, NMS passwords,
# room configs). Only the lab network gear opts in:
#   "fallback": SHA-2 first, SHA-1 only if the device offers nothing else
#               (routers: QA1's signs with rsa-sha2-512, older Cisco IOS may not).
#   "allow":    straight to SHA-1 (EXOS switches: ssh-rsa only; skipping the
#               SHA-2 attempt saves a handshake per switch read).
_NO_SHA1 = {
    "keys": ["ssh-rsa"],
    "pubkeys": ["ssh-rsa"],
    "kex": ["diffie-hellman-group1-sha1", "diffie-hellman-group14-sha1", "diffie-hellman-group-exchange-sha1"],
}


def connect(target: SSHTarget, timeout: int = 10, try_keys: bool = True, sha1: str = "refuse") -> paramiko.SSHClient:
    """Connect with the password if one is given, falling back to SSH keys.

    Password first because the labs use passwords: trying local keys first
    cost an extra failed handshake (2-4 s per room). ``try_keys=False`` never
    tries keys: network gear (Cisco) drops the session after a few failed
    attempts, which local keys use up. ``sha1`` ("refuse" / "fallback" /
    "allow") is only loosened for the lab router and switches (see ``_NO_SHA1``).

    Raises ``SSHError`` with a descriptive message if every method fails.
    """
    def attempt(use_keys: bool, strict: bool = sha1 != "allow") -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        with _KNOWN_HOSTS_LOCK:
            if KNOWN_HOSTS.exists():
                client.load_host_keys(str(KNOWN_HOSTS))
        client.set_missing_host_key_policy(_TrustOnFirstUse())
        try:
            client.connect(hostname=target.host, port=target.port, username=target.username,
                           password=None if use_keys else target.password, timeout=timeout,
                           look_for_keys=use_keys, allow_agent=use_keys,
                           disabled_algorithms=_NO_SHA1 if strict else None)
            client.md_sudo_password = target.sudo_password
            return client
        except paramiko.ssh_exception.IncompatiblePeer as exc:
            if strict and sha1 == "fallback":
                return attempt(use_keys, strict=False)  # older device: SHA-1 is all it has
            raise SSHError(
                f"Refused {target.host}:{target.port}: it only offers outdated SHA-1 SSH signatures or key exchange. "
                "The rooms run modern SSH, so this isn't how a room normally answers. Don't connect; tell the team."
            ) from exc
        except paramiko.BadHostKeyException as exc:
            raise SSHError(
                f"Refused {target.host}:{target.port}: its SSH host key changed since this computer last "
                "connected. If the room was re-imaged or the device replaced, use Actions > Remove "
                "Fingerprint for it (or delete its line in "
                f"{KNOWN_HOSTS}). Otherwise something may be impersonating it - don't connect."
            ) from exc
        except (OSError, EOFError) as exc:
            # Timeout / refused / unreachable: another method would just
            # wait out the same timeout again.
            raise SSHError(f"No answer from {target.host}:{target.port} ({exc or 'connection closed'})") from exc

    password_error: Optional[Exception] = None
    if target.password:
        try:
            return attempt(use_keys=False)
        except SSHError:
            raise
        except Exception as exc:  # noqa: BLE001 - keys may still work
            password_error = exc
    if not try_keys:
        raise SSHError(f"SSH auth failed for {target.username}@{target.host}:{target.port}: "
                       f"{password_error or 'no password provided'}")
    try:
        return attempt(use_keys=True)
    except SSHError:
        raise
    except Exception as exc:  # noqa: BLE001
        detail = f"password: {password_error}; key: {exc}" if password_error else f"key auth failed and no password was provided ({exc})"
        raise SSHError(f"SSH auth failed for {target.username}@{target.host}:{target.port}: {detail}") from exc


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
    stdin, stdout, stderr = exec_command(client, command, get_pty=get_pty)
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
            line = redact(client, line)
            if line.strip() != "<hidden>":  # the PTY's echo of the password line
                on_line(line)
    exit_status = stdout.channel.recv_exit_status()
    if on_line is not None and not get_pty:
        err = stderr.read().decode(errors="replace").strip()
        if err:
            on_line(redact(client, err))
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


def upload_text(client: paramiko.SSHClient, text: str, remote_path: str, mode: str = "0600") -> None:
    """Upload ``text`` straight from memory (no local temp file) as an
    owner-only file. Room configs hold API keys and passwords."""
    with SCPClient(client.get_transport()) as scp:
        scp.putfo(io.BytesIO(text.encode("utf-8")), remote_path, mode=mode)


def upload_dir(
    client: paramiko.SSHClient,
    local_dir: str,
    remote_path: str,
    log: Optional[Logger] = None,
) -> None:
    """Recursively upload a local directory via SCP.

    Used by the web app deploy flow to push a full ``dist``/web-assets
    folder in one shot instead of walking it file-by-file over SFTP.
    """
    start = time.monotonic()
    with SCPClient(client.get_transport()) as scp:
        scp.put(local_dir, remote_path, recursive=True)
    elapsed = time.monotonic() - start
    if log is not None:
        log(f"Uploaded {local_dir} in {elapsed:.1f}s.", "detail")


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


def run_cisco_config_commands(
    client: paramiko.SSHClient,
    commands: list,
    enable_password: Optional[str] = None,
    log: Optional[Logger] = None,
) -> str:
    """Run config commands on a Cisco IOS-style device over an interactive
    SSH shell.

    ``exec_command`` only runs one-shot EXEC commands and can't reach
    privileged/config mode on IOS, so this drives an ``invoke_shell`` session
    instead: enters privileged EXEC (sending ``enable_password`` if the
    device prompts for one), then ``configure terminal``, each command in
    turn, ``end``, and ``write memory``. Returns the full captured transcript
    and streams each line to ``log`` as it arrives.

    Raises ``SSHError`` if the device asks for an enable password that
    wasn't provided.
    """
    return _cisco_session(client, commands, enable_password, log, configure=True)


def run_cisco_show(
    client: paramiko.SSHClient,
    commands: list,
    enable_password: Optional[str] = None,
) -> str:
    """Read-only counterpart of ``run_cisco_config_commands``: enters
    privileged EXEC (``show running-config`` needs it), runs each ``show``
    command and returns the transcript. Never enters config mode."""
    return _cisco_session(client, commands, enable_password, None, configure=False)


# The CLI prompt as the last line of output: Cisco "Router#" / "Router>",
# EXOS "* QALAB-Switch1.3 # ". Banner art like "####" doesn't match.
_PROMPT_RE = re.compile(r"(?:^|\n)\*?\s*[\w.\-()/:]+ ?[#>]\s*$")
_PASSWORD_PROMPT_RE = re.compile(r"[Pp]assword:\s*$")


def _read_until_prompt(chan, timeout: float, also: Optional["re.Pattern[str]"] = None) -> str:
    """Read until the output ends at a CLI prompt (or matches ``also``).
    Polls quickly instead of sleeping a fixed time per command; returns what
    arrived if the prompt doesn't show up within ``timeout``."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chan.recv_ready():
            buf += chan.recv(65536)
            text = strip_ansi(buf.decode(errors="replace"))
            if _PROMPT_RE.search(text) or (also and also.search(text)):
                return text
        elif chan.closed or chan.exit_status_ready():
            break
        else:
            time.sleep(0.05)
    return strip_ansi(buf.decode(errors="replace"))


def run_cli_show(client: paramiko.SSHClient, commands: list, setup: tuple = (), timeout: float = 20) -> Dict[str, str]:
    """Run read-only commands in a network device's interactive CLI (e.g.
    Extreme EXOS) and return ``{command: output}``. ``setup`` commands (like
    ``disable clipaging``) run first and aren't returned. Each command waits
    until the prompt comes back."""
    chan = client.invoke_shell(width=250)
    chan.settimeout(timeout)

    def _run(cmd: str) -> str:
        if cmd:
            chan.send(cmd + "\n")
        return _read_until_prompt(chan, timeout)

    try:
        _run("")
        for cmd in setup:
            _run(cmd)
        return {cmd: _run(cmd) for cmd in commands}
    finally:
        chan.close()


def _cisco_session(client, commands, enable_password, log, configure: bool) -> str:
    chan = client.invoke_shell()
    chan.settimeout(10)
    transcript: list = []

    def _drain(wait: float) -> str:
        time.sleep(wait)
        buf = b""
        while True:
            got = False
            try:
                while chan.recv_ready():
                    buf += chan.recv(65536)
                    got = True
            except Exception:  # noqa: BLE001
                break
            if not got:
                break
        return strip_ansi(buf.decode(errors="replace"))

    # Read-only sessions wait for the prompt instead of fixed sleeps (~3.5 s
    # saved per router read). Config sessions keep the proven fixed timing.
    fast = not configure

    def _send(cmd: str, wait: float = 0.8, until_prompt: bool = False) -> str:
        chan.send(cmd + "\n")
        if fast:
            out = _read_until_prompt(chan, 20, _PASSWORD_PROMPT_RE)
        else:
            out = _drain(wait)
            # Long show output can trickle in; keep reading until the prompt is back.
            deadline = time.time() + 20
            while until_prompt and not out.rstrip().endswith(("#", ">")) and time.time() < deadline:
                out += _drain(0.5)
        transcript.append(out)
        if log:
            for line in out.splitlines():
                line = line.strip()
                if line:
                    log(line, "detail")
        return out

    try:
        banner = _read_until_prompt(chan, 10) if fast else _drain(1.2)
        transcript.append(banner)
        if log:
            for line in banner.splitlines():
                if line.strip():
                    log(line.strip(), "detail")

        enable_out = _send("enable")
        if "password" in enable_out.lower():
            if not enable_password:
                raise SSHError("Router prompted for an enable password, but none was provided.")
            _send(enable_password)

        _send("terminal length 0")
        if configure:
            _send("configure terminal")
        for command in commands:
            _send(command, until_prompt=not configure)
        if configure:
            _send("end")
            _send("write memory", wait=2.0)
    finally:
        chan.close()

    return "".join(transcript)

    return False
