"""Local DevTools tunnels into operating rooms (like ``ssh -L``, but guarded).

Each room is reached through the router's forwarded SSH port; from the room
the Matrix App's DevTools (CDP) port is reachable on ``127.0.0.1:<port>``.
A tunnel is a small local HTTP/WebSocket proxy on ``127.0.0.1:<random>``
that opens a ``direct-tcpip`` SSH channel per request.

DevTools gives full control of the room's app, and any program (or other
Windows user) on this computer can reach 127.0.0.1, so the proxy only answers
requests that carry the tunnel's random token: as the first path segment
(``/<token>/devtools/...``) or as the per-port cookie that the first such
request sets. There is no unauthenticated raw TCP forward.
"""

from __future__ import annotations

import hmac
import http.server
import re
import secrets
import socketserver
import threading
import uuid
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Dict, List, Optional, Tuple

_WS_URL_RE = re.compile(rb"ws://(?:127\.0\.0\.1|localhost):\d+")


def _recv_response(sock) -> bytes:
    """Read one HTTP response from ``sock`` (socket or SSH channel): the
    headers, then ``Content-Length`` bytes of body (until EOF if there is no
    length). Newer Chromium keeps the connection open despite our
    ``Connection: close``, so waiting for EOF alone would hang every request."""
    buf = b""
    want = None
    while want is None or len(buf) < want:
        try:
            b = sock.recv(65536)
        except OSError:
            break
        if not b:
            break
        buf += b
        if want is None and b"\r\n\r\n" in buf:
            head, _ = buf.split(b"\r\n\r\n", 1)
            m = re.search(rb"(?im)^content-length:\s*(\d+)\s*$", head)
            if m:
                want = len(head) + 4 + int(m.group(1))
    return buf


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_devtools_proxy(transport, chain_port: int) -> Tuple[http.server.HTTPServer, int, str]:
    """Start a token-protected local HTTP/WS proxy to ``127.0.0.1:chain_port``
    on the room, over ``transport`` (an SSH transport; only needs
    ``open_channel``).

    Mirrors the matrix-lab extension's ``createDevToolsProxy``:
      * ``/json*`` response bodies get their ``ws://host:port`` rewritten to
        point back at this proxy (token included), and
      * WebSocket upgrades have their ``Origin`` header stripped and ``Host``
        normalized, which is what makes Chromium's DevTools accept a tunneled
        connection instead of dropping it ("WebSocket disconnected").

    Returns ``(server, local_port, token)``; ``server.shutdown()`` stops it.
    """
    upstream_host_hdr = f"127.0.0.1:{chain_port}"
    token = secrets.token_urlsafe(24)
    state: Dict[str, int] = {}

    def open_upstream():
        chan = transport.open_channel("direct-tcpip", ("127.0.0.1", chain_port), ("127.0.0.1", 0), timeout=10)
        chan.settimeout(10)
        return chan

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence
            return

        def _cookie_name(self) -> str:
            return f"md_devtools_{state['port']}"

        def _forward_headers(self):
            out = []
            for key, val in self.headers.items():
                lk = key.lower()
                # cookie: 127.0.0.1 cookies (the app's session, this token) aren't for the room.
                if lk in ("origin", "accept-encoding", "connection", "host", "content-length", "cookie"):
                    continue
                out.append(f"{key}: {val}")
            out.append(f"Host: {upstream_host_hdr}")
            return out

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def _authorized_path(self) -> Optional[Tuple[str, bool]]:
            """``(upstream path, set_cookie)`` if the request carries the token."""
            prefix = f"/{token}"
            if self.path == prefix or self.path.startswith(prefix + "/") or self.path.startswith(prefix + "?"):
                return self.path[len(prefix):] or "/", True
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
            except CookieError:
                pass
            got = cookies.get(self._cookie_name())
            if got and hmac.compare_digest(got.value, token):
                return self.path, False
            return None

        def _route(self):
            # The Origin is stripped before forwarding, which also removes
            # Chromium's own guard, so check it here: only the inspector page
            # (served by this proxy) or non-browser clients may connect, and
            # only under a local Host name (no DNS rebinding). Every request
            # also needs the token.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            origin = self.headers.get("Origin")
            allowed = {f"http://{h}:{state['port']}" for h in ("127.0.0.1", "localhost")}
            auth = self._authorized_path()
            if host not in ("127.0.0.1", "localhost") or (origin is not None and origin.lower() not in allowed) or not auth:
                self.send_error(403, "Refused")
                return
            path, set_cookie = auth
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self._proxy_ws(path)
            else:
                self._proxy_http(path, set_cookie)

        def _proxy_http(self, path: str, set_cookie: bool):
            try:
                up = open_upstream()
            except Exception:  # noqa: BLE001 - port closed / SSH gone
                self.send_error(502, "Tunnel unavailable")
                return
            body = b""
            cl = self.headers.get("Content-Length")
            if cl:
                try:
                    body = self.rfile.read(int(cl))
                except (ValueError, OSError):
                    body = b""
            req = [f"{self.command} {path} HTTP/1.1"] + self._forward_headers()
            req.append("Connection: close")
            try:
                up.sendall(("\r\n".join(req) + "\r\n\r\n").encode("latin1") + body)
                resp = _recv_response(up)
            finally:
                up.close()

            sep = resp.find(b"\r\n\r\n")
            if sep == -1:
                self.send_error(502, "Bad upstream response")
                return
            head, payload = resp[:sep], resp[sep + 4:]
            if path.startswith("/json"):
                payload = _WS_URL_RE.sub(f"ws://127.0.0.1:{state['port']}/{token}".encode(), payload)

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
                if k.strip().lower() in (b"content-length", b"connection", b"transfer-encoding", b"set-cookie"):
                    continue
                self.send_header(k.strip().decode("latin1"), v.strip().decode("latin1"))
            if set_cookie:
                # Lets the inspector's absolute-path requests and its WebSocket through.
                self.send_header("Set-Cookie", f"{self._cookie_name()}={token}; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        def _proxy_ws(self, path: str):
            try:
                up = open_upstream()
            except Exception:  # noqa: BLE001
                self.send_error(502, "Tunnel unavailable")
                return
            up.settimeout(None)
            req = [f"{self.command} {path} HTTP/1.1"] + self._forward_headers()
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

            # Two blocking pump threads instead of select(): paramiko channels
            # are NOT reliably selectable on Windows, which breaks long-lived
            # WebSockets even though short HTTP requests happen to work.
            t = threading.Thread(target=pump, args=(up, client), daemon=True)
            t.start()
            pump(client, up)
            # Prevent BaseHTTPRequestHandler from touching the hijacked socket.
            self.close_connection = True

    server = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state["port"], token


def build_inspector_url(base: str, targets: list) -> Optional[str]:
    """Chrome DevTools inspector URL, served through the proxy at ``base``
    (``http://127.0.0.1:<port>/<token>``). ``targets`` come from the proxy's
    ``/json/list``, so their debugger URLs already point back at it. Returns
    ``None`` if no usable page target is present."""
    from urllib.parse import urlparse

    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    candidates = pages or [t for t in targets if isinstance(t, dict)]
    for t in candidates:
        ws = urlparse(t.get("webSocketDebuggerUrl") or "")
        if not ws.path or not ws.netloc:
            continue
        return f"{base}/devtools/inspector.html?ws={ws.netloc}{ws.path}"
    return None


def first_page(targets: list) -> Optional[dict]:
    """The first ``page`` target that has a debugger URL, if any."""
    return next(
        (t for t in targets if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")),
        None,
    )


def cdp_call(targets: list, method: str, params: Optional[dict] = None, timeout: float = 20.0) -> dict:
    """Run one Chrome DevTools Protocol command on the first page target
    (through the tunnel proxy its debugger URL points at) and return its
    result. No Origin header is sent, so Chromium accepts the connection."""
    import json

    from websockets.sync.client import connect as ws_connect

    page = first_page(targets)
    if not page:
        raise RuntimeError("No app page target found.")
    url = page["webSocketDebuggerUrl"]
    if not url.startswith("ws://127.0.0.1:"):
        raise RuntimeError("Unexpected DevTools debugger URL.")
    # Short close_timeout: Chromium doesn't drop the connection after the
    # close handshake, and the default 10 s wait made every call that slow.
    with ws_connect(url, open_timeout=timeout, close_timeout=1, max_size=None) as ws:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv(timeout=timeout))
            if msg.get("id") != 1:
                continue  # unrelated CDP event
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message", str(msg["error"])))
            return msg.get("result", {})


def largest_page_first(targets: list) -> list:
    """Reorder ``targets`` so the page with the biggest viewport comes first.
    Apps like the Matrix Admin App expose several pages (a 50px header strip,
    an NMS Demonstrator view, the main UI); the first one listed isn't
    necessarily the one on screen. Pages that can't be measured sort last."""
    def area(t: dict) -> int:
        if t.get("type") != "page" or not t.get("webSocketDebuggerUrl"):
            return -1
        try:
            r = cdp_call([t], "Runtime.evaluate",
                         {"expression": "innerWidth * innerHeight", "returnByValue": True}, timeout=5)
            return int(r.get("result", {}).get("value") or 0)
        except Exception:  # noqa: BLE001
            return 0

    pages = [t for t in targets if isinstance(t, dict)]
    if sum(1 for t in pages if t.get("type") == "page") < 2:
        return targets
    return sorted(pages, key=area, reverse=True)


def capture_cdp_screenshot(targets: list, timeout: float = 20.0) -> bytes:
    """Grab a PNG of the first page target (``Page.captureScreenshot``)."""
    import base64

    result = cdp_call(targets, "Page.captureScreenshot", {"format": "png"}, timeout)
    return base64.b64decode(result["data"])


@dataclass
class Tunnel:
    id: str
    room_number: int
    target_key: str
    label: str
    local_port: int
    # Token-protected base URL (``http://127.0.0.1:<port>/<token>``); replaced
    # by the inspector URL once one is known.
    url: Optional[str]
    base: str = ""


class TunnelManager:
    def __init__(self) -> None:
        self._tunnels: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def open_port(self, room_number: int, chain_port: int, label: str, client) -> Tunnel:
        """Open a token-protected DevTools proxy to ``127.0.0.1:chain_port``
        on the room over an open SSH ``client``; the tunnel then owns (and
        closes) it."""
        server, local_port, token = make_devtools_proxy(client.get_transport(), chain_port)
        base = f"http://127.0.0.1:{local_port}/{token}"
        tunnel_id = str(uuid.uuid4())
        info = Tunnel(id=tunnel_id, room_number=room_number, target_key="devtools",
                      label=label, local_port=local_port, url=base, base=base)
        with self._lock:
            self._tunnels[tunnel_id] = {"client": client, "server": server, "info": info}
        return info

    def set_url(self, tunnel_id: str, url: str) -> None:
        with self._lock:
            rec = self._tunnels.get(tunnel_id)
            if rec:
                rec["info"].url = url

    def close(self, tunnel_id: str) -> bool:
        with self._lock:
            rec = self._tunnels.pop(tunnel_id, None)
        if rec is None:
            return False
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
