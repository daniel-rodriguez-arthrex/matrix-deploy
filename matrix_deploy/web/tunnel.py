"""Local SSH port-forwards into operating rooms (``ssh -L`` equivalent).

Each room is reached through the router's forwarded SSH port; from the room a
service (e.g. the Matrix App's DevTools port) is reachable on
``127.0.0.1:<port>``. A
tunnel opens a ``direct-tcpip`` channel over that SSH connection and exposes it
as a local ``127.0.0.1:<local_port>`` the user can hit from their browser.

Localhost-only, single-user model (same as the rest of the web server).
"""

from __future__ import annotations

import http.server
import re
import socket
import socketserver
import threading
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..config import AppConfig, Room
from ..deployer import DeploymentCredentials
from ..ssh_client import SSHError, SSHTarget, connect

_WS_URL_RE = re.compile(rb"ws://(?:127\.0\.0\.1|localhost):\d+")


def _recv_all(sock: socket.socket) -> bytes:
    """Read from ``sock`` until EOF (upstream sends Connection: close)."""
    chunks = []
    while True:
        try:
            b = sock.recv(65536)
        except OSError:
            break
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks)


def make_devtools_proxy(tunnel_host: str, tunnel_port: int):
    """Start a local HTTP/WS proxy in front of a raw CDP tunnel.

    Mirrors the matrix-lab extension's ``createDevToolsProxy``:
      * ``/json*`` response bodies get their ``ws://host:port`` rewritten to
        point back at this proxy, and
      * WebSocket upgrades have their ``Origin`` header stripped and ``Host``
        normalized, which is what makes Chromium's DevTools accept a tunneled
        connection instead of dropping it ("WebSocket disconnected").

    Returns ``(server, local_port)``; ``server.shutdown()`` stops it.
    """
    upstream_host_hdr = f"{tunnel_host}:{tunnel_port}"
    state: Dict[str, int] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence
            return

        def _forward_headers(self):
            out = []
            for key, val in self.headers.items():
                lk = key.lower()
                if lk in ("origin", "accept-encoding", "connection", "host", "content-length"):
                    continue
                out.append(f"{key}: {val}")
            out.append(f"Host: {upstream_host_hdr}")
            return out

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def _route(self):
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self._proxy_ws()
            else:
                self._proxy_http()

        def _proxy_http(self):
            try:
                up = socket.create_connection((tunnel_host, tunnel_port), timeout=10)
            except OSError:
                self.send_error(502, "Tunnel unavailable")
                return
            body = b""
            cl = self.headers.get("Content-Length")
            if cl:
                try:
                    body = self.rfile.read(int(cl))
                except (ValueError, OSError):
                    body = b""
            req = [f"{self.command} {self.path} HTTP/1.1"] + self._forward_headers()
            req.append("Connection: close")
            up.sendall(("\r\n".join(req) + "\r\n\r\n").encode("latin1") + body)
            resp = _recv_all(up)
            up.close()

            sep = resp.find(b"\r\n\r\n")
            if sep == -1:
                self.send_error(502, "Bad upstream response")
                return
            head, payload = resp[:sep], resp[sep + 4:]
            if self.path.startswith("/json"):
                payload = _WS_URL_RE.sub(f"ws://127.0.0.1:{state['port']}".encode(), payload)

            lines = head.split(b"\r\n")
            try:
                code = int(lines[0].split(b" ")[1])
            except (IndexError, ValueError):
                code = 200
            self.send_response_only(code)
            for line in lines[1:]:
                if b":" not in line:
                    continue
                k, v = line.split(b":", 1)
                if k.strip().lower() in (b"content-length", b"connection", b"transfer-encoding"):
                    continue
                self.send_header(k.strip().decode("latin1"), v.strip().decode("latin1"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        def _proxy_ws(self):
            try:
                up = socket.create_connection((tunnel_host, tunnel_port), timeout=10)
            except OSError:
                self.send_error(502, "Tunnel unavailable")
                return
            req = [f"{self.command} {self.path} HTTP/1.1"] + self._forward_headers()
            req.append("Connection: Upgrade")
            up.sendall(("\r\n".join(req) + "\r\n\r\n").encode("latin1"))
            client = self.connection

            def pump(a, b):
                try:
                    while True:
                        data = a.recv(65536)
                        if not data:
                            break
                        b.sendall(data)
                except OSError:
                    pass
                for c in (a, b):
                    try:
                        c.close()
                    except OSError:
                        pass

            t = threading.Thread(target=pump, args=(up, client), daemon=True)
            t.start()
            pump(client, up)
            # Prevent BaseHTTPRequestHandler from touching the hijacked socket.
            self.close_connection = True

    server = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state["port"]


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _pump(read, write):
    """Copy bytes from ``read(n)`` to ``write(data)`` until EOF/error."""
    try:
        while True:
            data = read(4096)
            if not data:
                break
            write(data)
    except Exception:  # noqa: BLE001 - either end closing is normal
        pass


def _make_handler(transport, chain_host: str, chain_port: int):
    class _Handler(socketserver.BaseRequestHandler):
        def handle(self):  # noqa: D401
            try:
                chan = transport.open_channel(
                    "direct-tcpip",
                    (chain_host, chain_port),
                    self.request.getpeername(),
                )
            except Exception:  # noqa: BLE001 - connection setup can fail
                return
            if chan is None:
                return
            sock = self.request

            # Two blocking pump threads instead of select(): paramiko channels
            # are NOT reliably selectable on Windows, which breaks long-lived
            # connections (WebSockets, Redis) even though short HTTP requests
            # happen to work. Mirrors ssh2's socket.pipe(stream).pipe(socket).
            def close_both():
                for c in (chan, sock):
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass

            def sock_to_chan():
                _pump(sock.recv, chan.sendall)
                close_both()

            def chan_to_sock():
                _pump(chan.recv, sock.sendall)
                close_both()

            t = threading.Thread(target=chan_to_sock, daemon=True)
            t.start()
            sock_to_chan()
            t.join(timeout=5)

    return _Handler


def build_inspector_url(html_port: int, ws_port: int, targets: list) -> Optional[str]:
    """Build a Chrome DevTools inspector URL. The inspector HTML/assets are
    served from ``html_port`` (the transparent raw tunnel, which handles
    HTTP keep-alive fine), while the debugger WebSocket points at ``ws_port``
    (the Origin-stripping proxy, required for Chromium to accept it). Returns
    ``None`` if no usable page target is present."""
    from urllib.parse import urlparse

    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    candidates = pages or [t for t in targets if isinstance(t, dict)]
    for t in candidates:
        ws = t.get("webSocketDebuggerUrl")
        if not ws:
            continue
        ws_path = urlparse(ws).path
        if not ws_path:
            continue
        return (
            f"http://127.0.0.1:{html_port}/devtools/inspector.html"
            f"?ws=127.0.0.1:{ws_port}{ws_path}"
        )
    return None


def capture_cdp_screenshot(local_port: int, targets: list, timeout: float = 20.0) -> bytes:
    """Grab a PNG of the first page target over the Chrome DevTools Protocol
    (``Page.captureScreenshot``) through a local tunnel to the CDP port.
    No Origin header is sent, so Chromium accepts the tunneled connection."""
    import base64
    import json
    from urllib.parse import urlparse

    from websockets.sync.client import connect as ws_connect

    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    ws_path = next(
        (urlparse(t["webSocketDebuggerUrl"]).path for t in pages if t.get("webSocketDebuggerUrl")),
        None,
    )
    if not ws_path:
        raise RuntimeError("No Matrix App page target found to screenshot.")
    with ws_connect(
        f"ws://127.0.0.1:{local_port}{ws_path}", open_timeout=timeout, max_size=None
    ) as ws:
        ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
        while True:
            msg = json.loads(ws.recv(timeout=timeout))
            if msg.get("id") != 1:
                continue  # unrelated CDP event
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message", str(msg["error"])))
            return base64.b64decode(msg["result"]["data"])


def start_forward_server(transport, chain_host: str, chain_port: int):
    """Start a threaded local forward server that pipes each accepted
    connection over ``transport`` to ``chain_host:chain_port``. Returns
    ``(server, thread, local_port)``. ``transport`` only needs an
    ``open_channel(kind, dest, peer)`` method, which makes this testable
    with a fake transport."""
    handler = _make_handler(transport, chain_host, chain_port)
    server = _ForwardServer(("127.0.0.1", 0), handler)
    local_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, local_port


@dataclass
class Tunnel:
    id: str
    room_number: int
    target_key: str
    label: str
    local_port: int
    url: Optional[str]


class TunnelManager:
    def __init__(self) -> None:
        self._tunnels: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def open_port(
        self,
        config: AppConfig,
        creds: DeploymentCredentials,
        room: Room,
        chain_port: int,
        label: str,
        scheme: str = "http",
        path: str = "",
    ) -> Tunnel:
        """Open a raw local forward to ``127.0.0.1:chain_port`` on the room.
        Used for ad-hoc services (e.g. the Matrix App CDP port 9222)."""
        conn = config.connection
        ssh_target = SSHTarget(
            host=conn.router_ip,
            port=room.ssh_port(conn.ssh_port_base),
            username=conn.ssh_username,
            password=creds.ssh_password,
        )
        client = connect(ssh_target)
        transport = client.get_transport()
        server, thread, local_port = start_forward_server(transport, "127.0.0.1", chain_port)
        url = f"{scheme}://127.0.0.1:{local_port}{path}" if scheme in ("http", "https") else None
        tunnel_id = str(uuid.uuid4())
        with self._lock:
            self._tunnels[tunnel_id] = {
                "client": client, "server": server, "thread": thread,
                "info": Tunnel(
                    id=tunnel_id, room_number=room.number, target_key="raw",
                    label=label, local_port=local_port, url=url,
                ),
            }
        return self._tunnels[tunnel_id]["info"]

    def attach_proxy(self, tunnel_id: str, proxy_server, inspector_url: str) -> None:
        """Attach a DevTools HTTP/WS proxy (and its inspector URL) to a tunnel
        so it's torn down together with the tunnel."""
        with self._lock:
            rec = self._tunnels.get(tunnel_id)
            if rec:
                rec["proxy"] = proxy_server
                rec["info"].url = inspector_url

    def close(self, tunnel_id: str) -> bool:
        with self._lock:
            rec = self._tunnels.pop(tunnel_id, None)
        if rec is None:
            return False
        proxy = rec.get("proxy")
        if proxy is not None:
            try:
                proxy.shutdown()
                proxy.server_close()
            except Exception:  # noqa: BLE001
                pass
        try:
            rec["server"].shutdown()
            rec["server"].server_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            rec["client"].close()
        except Exception:  # noqa: BLE001
            pass
        return True

    def list(self) -> List[Tunnel]:
        with self._lock:
            return [rec["info"] for rec in self._tunnels.values()]

    def close_all(self) -> None:
        for tid in list(self._tunnels.keys()):
            self.close(tid)
