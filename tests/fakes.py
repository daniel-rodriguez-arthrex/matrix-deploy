"""Stand-ins for paramiko clients/channels, interactive CLI shells and SCP.

Nothing here opens a socket: room, router and switch sessions are scripted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union


class FakeChannel:
    def __init__(self, data: bytes = b"", exit_status: int = 0):
        self._data = data
        self._pos = 0
        self.exit_status = exit_status
        self.closed = False
        self.timeout = None
        self.write_shut = False

    def recv_ready(self) -> bool:
        return self._pos < len(self._data)

    def recv(self, n: int) -> bytes:
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def exit_status_ready(self) -> bool:
        return not self.recv_ready()

    def recv_exit_status(self) -> int:
        return self.exit_status

    def settimeout(self, t) -> None:
        self.timeout = t

    def shutdown_write(self) -> None:
        self.write_shut = True

    def close(self) -> None:
        self.closed = True


class FakeStream:
    def __init__(self, text: str, channel: FakeChannel):
        self._text = text
        self.channel = channel

    def __iter__(self):
        return iter(self._text.splitlines(keepends=True))

    def read(self) -> bytes:
        return self._text.encode()


class FakeStdin:
    def __init__(self, channel: FakeChannel):
        self.data = ""
        self.channel = channel

    def write(self, s: str) -> None:
        self.data += s

    def flush(self) -> None:
        pass


class FakeTransport:
    def __init__(self, active: bool = True):
        self.active = active

    def is_active(self) -> bool:
        return self.active


Response = Union[str, Tuple[str, int], Tuple[str, str, int], BaseException]


def rules(*pairs: Tuple[str, Response], default: Response = "") -> Callable[[str], Response]:
    """Responder: the first regex (searched in the command) that matches wins."""
    compiled = [(re.compile(p), r) for p, r in pairs]

    def respond(cmd: str) -> Response:
        for rx, r in compiled:
            if rx.search(cmd):
                return r
        return default

    return respond


class FakeSSHClient:
    """``exec_command`` answers from ``responder(command)``: a str (stdout,
    exit 0), ``(stdout, exit)``, ``(stdout, stderr, exit)`` or an exception."""

    def __init__(self, responder: Optional[Callable[[str], Response]] = None, sudo_password: Optional[str] = None,
                 shell: Optional[Callable[[], "FakeShell"]] = None):
        self.responder = responder or (lambda cmd: "")
        self.md_sudo_password = sudo_password
        self.commands: List[str] = []
        self.stdins: List[FakeStdin] = []
        self.closed = False
        self.transport = FakeTransport()
        self.shell_factory = shell
        self.shells: List[FakeShell] = []

    def exec_command(self, command: str, get_pty: bool = False):
        self.commands.append(command)
        r = self.responder(command)
        if isinstance(r, BaseException):
            raise r
        out, err, code = (r, "", 0) if isinstance(r, str) else ((r[0], "", r[1]) if len(r) == 2 else r)
        ch = FakeChannel(out.encode(), code)
        stdin = FakeStdin(ch)
        self.stdins.append(stdin)
        return stdin, FakeStream(out, ch), FakeStream(err, FakeChannel(err.encode(), code))

    def get_transport(self):
        return self.transport

    def invoke_shell(self, width: int = 80):
        shell = self.shell_factory()
        self.shells.append(shell)
        return shell

    def close(self) -> None:
        self.closed = True


class FakeShell:
    """Interactive CLI: ``handler(cmd)`` returns what the device prints
    (ending at its prompt) for each line sent."""

    def __init__(self, handler: Callable[[str], str], banner: str = "Router#"):
        self.handler = handler
        self.buf = banner.encode()
        self.sent: List[str] = []
        self.closed = False

    def send(self, s: str) -> None:
        cmd = s.rstrip("\n")
        self.sent.append(cmd)
        self.buf += self.handler(cmd).encode()

    def recv_ready(self) -> bool:
        return bool(self.buf)

    def recv(self, n: int) -> bytes:
        chunk, self.buf = self.buf[:n], self.buf[n:]
        return chunk

    def settimeout(self, t) -> None:
        pass

    def exit_status_ready(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


def cisco_handler(responses: Optional[Dict[str, str]] = None, hostname: str = "Router",
                  write_ok: bool = True, enter_config: bool = True) -> Callable[[str], str]:
    responses = responses or {}
    state = {"config": False}

    def handle(cmd: str) -> str:
        prompt = f"{hostname}(config)#" if state["config"] else f"{hostname}#"
        if cmd in responses:
            return f"{cmd}\n{responses[cmd]}"
        if cmd == "configure terminal":
            state["config"] = enter_config
            return f"{cmd}\nEnter configuration commands, one per line.\n" + (f"{hostname}(config)#" if enter_config else f"{hostname}#")
        if cmd == "end":
            state["config"] = False
            return f"{cmd}\n{hostname}#"
        if cmd == "write memory":
            return f"{cmd}\nBuilding configuration...\n" + ("[OK]\n" if write_ok else "") + f"{hostname}#"
        return f"{cmd}\n{prompt}"

    return handle


class FakeSCP:
    """Replaces ``scp.SCPClient``; every instance is recorded on the class."""

    instances: List["FakeSCP"] = []
    files: Dict[str, bytes] = {}
    fail: Optional[BaseException] = None

    def __init__(self, transport, progress=None):
        self.progress = progress
        self.puts: List[tuple] = []
        self.gets: List[tuple] = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _tick(self):
        if self.progress:
            self.progress(b"f", 10, 5)
            self.progress(b"f", 10, 10)

    def _maybe_fail(self):
        """``fail``: an exception (every call fails) or a list of exceptions /
        None consumed one per call."""
        f = type(self).fail
        if isinstance(f, list):
            f = f.pop(0) if f else None
        if f:
            raise f

    def put(self, local, remote, recursive=False):
        self._maybe_fail()
        self.puts.append((local, remote, recursive))
        self._tick()

    def putfo(self, fo, remote, mode=None):
        self._maybe_fail()
        self.puts.append((fo.read().decode(), remote, mode))

    def get(self, remote, local):
        self._maybe_fail()
        self.gets.append((remote, local))
        Path(local).write_bytes(type(self).files.get(remote, b"journal line\n"))
        self._tick()
