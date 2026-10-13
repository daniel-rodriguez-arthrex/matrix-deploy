"""The token-protected DevTools proxy, against a scripted room-side channel."""

import http.client
import socket
import threading

import pytest

from matrix_deploy.web import tunnel


class Upstream:
    """The room's CDP port as seen through an SSH ``direct-tcpip`` channel."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.opened = []

    def open_channel(self, kind, dest, src, timeout):
        self.opened.append((kind, dest))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


JSON = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 63\r\nSet-Cookie: x=y\r\n"
        b"Connection: keep-alive\r\nnot-a-header\r\n\r\n"
        b'[{"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/p/1"}]')


@pytest.fixture
def proxy():
    servers = []

    def start(*responses):
        transport = Transport(*responses)
        server, port, token = tunnel.make_devtools_proxy(transport, 9222)
        servers.append(server)
        return transport, port, token

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def request(port, path, method="GET", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def test_json_rewrite_and_cookie(proxy):
    up = Upstream([JSON[:40], JSON[40:]])
    transport, port, token = proxy(up)
    resp, data = request(port, f"/{token}/json/list", headers={"Origin": f"http://127.0.0.1:{port}", "Cookie": "md_session_8420=s"})
    assert resp.status == 200
    assert data.decode() == f'[{{"webSocketDebuggerUrl": "ws://127.0.0.1:{port}/{token}/devtools/p/1"}}]'
    cookie = resp.getheader("Set-Cookie")
    assert cookie.startswith(f"md_devtools_{port}={token}") and "x=y" not in cookie
    sent = up.sent.decode()
    assert sent.startswith("GET /json/list HTTP/1.1") and "Host: 127.0.0.1:9222" in sent
    assert "Origin" not in sent and "md_session" not in sent and "Connection: close" in sent
    assert transport.opened == [("direct-tcpip", ("127.0.0.1", 9222))] and up.closed


def test_cookie_auth_post_and_status(proxy):
    up = Upstream([b"HTTP/1.1 404 Not Found\r\nContent-Length: 2\r\n\r\nno"])
    _, port, token = proxy(up)
    resp, data = request(port, "/json/new?x", method="POST", body=b"abc",
                         headers={"Cookie": f"md_devtools_{port}={token}", "Content-Length": "3"})
    assert resp.status == 404 and data == b"no" and resp.getheader("Set-Cookie") is None
    assert up.sent.endswith(b"\r\n\r\nabc")


@pytest.mark.parametrize("path, headers", [
    ("/json/list", {}),
    ("/{token}/json", {"Host": "evil.example"}),
    ("/{token}/json", {"Origin": "http://evil.example"}),
    ("/json", {"Cookie": "md_devtools_1=wrong; bad\x7f"}),
])
def test_refused(proxy, path, headers):
    _, port, token = proxy()
    resp, _ = request(port, path.format(token=token), headers=headers)
    assert resp.status == 403


def test_upstream_failures(proxy):
    _, port, token = proxy(OSError("port closed"), Upstream([b"garbage without headers"]),
                           Upstream([b"HTTP/1.1\r\n\r\nbody"]), OSError("closed"))
    assert request(port, f"/{token}")[0].status == 502
    assert request(port, f"/{token}/x")[0].status == 502
    resp, data = request(port, f"/{token}?q")
    assert resp.status == 200 and data == b"body"
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET /{token}/devtools HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n\r\n".encode())
    assert b"502" in s.recv(1024)
    s.close()


def test_websocket_pump(proxy):
    up = Upstream([b"HTTP/1.1 101 Switching Protocols\r\n\r\n", b"frame"])
    _, port, token = proxy(up)
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET /{token}/devtools/page/1 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
              f"Origin: http://localhost:{port}\r\n\r\n".encode())
    want = b"HTTP/1.1 101 Switching Protocols\r\n\r\nframe"
    got = b""
    while len(got) < len(want):
        got += s.recv(1024)
    s.close()  # the browser hanging up ends the other pump
    assert got == want
    sent = up.sent.decode()
    assert sent.startswith("GET /devtools/page/1 HTTP/1.1") and "Connection: Upgrade" in sent and "Origin" not in sent


def test_recv_response():
    class Sock:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def recv(self, n):
            c = self.chunks.pop(0)
            if isinstance(c, Exception):
                raise c
            return c

    full = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc"
    assert tunnel._recv_response(Sock([full[:10], full[10:], b"never read"])) == full
    assert tunnel._recv_response(Sock([b"HTTP/1.1 200 OK\r\n\r\nall", b" of it", b""])) == b"HTTP/1.1 200 OK\r\n\r\nall of it"
    assert tunnel._recv_response(Sock([b"part", OSError()])) == b"part"


def test_target_helpers(monkeypatch):
    page = {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:5/tok/devtools/page/1"}
    other = {"type": "service_worker", "webSocketDebuggerUrl": "ws://127.0.0.1:5/tok/devtools/sw"}
    assert tunnel.build_inspector_url("http://b", [page]) == "http://b/devtools/inspector.html?ws=127.0.0.1:5/tok/devtools/page/1"
    assert tunnel.build_inspector_url("http://b", ["junk", other]).endswith("devtools/sw")
    assert tunnel.build_inspector_url("http://b", [{"type": "page"}]) is None
    assert tunnel.first_page([other, page]) is page and tunnel.first_page([]) is None

    areas = {"a": 10, "b": 500}

    def fake_cdp(targets, method, params=None, timeout=20.0):
        t = targets[0]
        if t["id"] == "c":
            raise RuntimeError("gone")
        return {"result": {"value": areas[t["id"]]}}

    monkeypatch.setattr(tunnel, "cdp_call", fake_cdp)
    pages = [dict(page, id=i) for i in ("a", "b", "c")] + [dict(other, id="w")]
    assert [t["id"] for t in tunnel.largest_page_first(pages)] == ["b", "a", "c", "w"]
    assert tunnel.largest_page_first([page]) == [page]
    monkeypatch.setattr(tunnel, "cdp_call", lambda t, m, p, timeout: {"data": "aGk="})
    assert tunnel.capture_cdp_screenshot([page]) == b"hi"


def test_cdp_call(monkeypatch):
    import json
    import websockets.sync.client as wsc

    sent, replies = [], []

    class WS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, m):
            sent.append(json.loads(m))

        def recv(self, timeout):
            return json.dumps(replies.pop(0))

    urls = []
    monkeypatch.setattr(wsc, "connect", lambda url, **kw: urls.append(url) or WS())
    page = {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:5/t/devtools/page/1"}
    replies[:] = [{"method": "Page.loadEventFired"}, {"id": 1, "result": {"ok": True}}]
    assert tunnel.cdp_call([page], "Page.navigate", {"url": "x"}) == {"ok": True}
    assert sent == [{"id": 1, "method": "Page.navigate", "params": {"url": "x"}}] and urls == [page["webSocketDebuggerUrl"]]
    replies[:] = [{"id": 1, "error": {"message": "nope"}}]
    with pytest.raises(RuntimeError, match="nope"):
        tunnel.cdp_call([page], "X")
    with pytest.raises(RuntimeError, match="No app page"):
        tunnel.cdp_call([], "X")
    with pytest.raises(RuntimeError, match="Unexpected DevTools"):
        tunnel.cdp_call([{"type": "page", "webSocketDebuggerUrl": "ws://evil:1/x"}], "X")


def test_tunnel_manager():
    class Client:
        closed = False

        def get_transport(self):
            return Transport()

        def close(self):
            Client.closed = True
            raise OSError("already closed")

    tm = tunnel.TunnelManager()
    t = tm.open_port(3, 9222, "Matrix App OR 3", Client())
    assert t.base.startswith(f"http://127.0.0.1:{t.local_port}/") and t.url == t.base
    tm.set_url(t.id, "http://inspector")
    tm.set_url("nope", "x")
    assert [x.url for x in tm.list()] == ["http://inspector"]
    assert tm.close(t.id) and Client.closed and not tm.close(t.id)
    t2 = tm.open_port(4, 9223, "x", Client())
    tm._tunnels[t2.id]["server"].shutdown = lambda: (_ for _ in ()).throw(OSError("x"))
    tm.close_all()
    assert tm.list() == []
