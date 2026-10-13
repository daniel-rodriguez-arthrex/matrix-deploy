"""Artifactory and Jenkins HTTP clients (requests is faked)."""

import pytest
import requests

from matrix_deploy import artifactory, jenkins
from matrix_deploy.artifactory import ArtifactoryClient, ArtifactoryCredentials, ArtifactoryError
from matrix_deploy.config import ArtifactoryConfig
from matrix_deploy.jenkins import JenkinsClient, JenkinsCredentials, JenkinsError, JenkinsLoginRequiredError


class Resp:
    def __init__(self, status=200, data=None, headers=None, chunks=(), text="", url="https://x/api"):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self._chunks = chunks
        self.text = text
        self.url = url

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def iter_content(self, chunk_size):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


FILES = {"files": [
    {"uri": "/a/wrynose-1.swu", "lastModified": "2026-01-01"},
    {"uri": "/b/wrynose-2.swu", "lastModified": "2026-03-01"},
    {"uri": "/c/other-3.swu", "lastModified": "2026-05-01"},
    {"uri": "/c/readme.txt", "lastModified": "2026-06-01"},
    {"uri": "/d", "folder": True},
]}


def _client(**cfg):
    return ArtifactoryClient(ArtifactoryConfig(url="https://af", repo="repo", build_path="builds", **cfg),
                             ArtifactoryCredentials("me@x", "tok"))


def test_find_latest_swu(monkeypatch, log):
    calls = []
    monkeypatch.setattr(artifactory.requests, "get", lambda url, **kw: calls.append((url, kw)) or Resp(data=FILES))
    c = _client()
    assert c.find_latest_swu(log) == "b/wrynose-2.swu"
    assert calls[0][0] == "https://af/api/storage/repo/builds?list&deep=1&listFolders=0"
    assert calls[0][1]["auth"] == ("me@x", "tok")
    assert c.find_latest_swu(branch_filter="") == "c/other-3.swu"
    assert c.find_latest_swu(log, build_path="x", branch_filter="nomatch") == "c/other-3.swu"
    assert "No .swu matched branch 'nomatch'" in log.text("warning")
    assert c.find_latest_swu(branch_filter="nomatch") == "c/other-3.swu"


def test_find_latest_swu_errors(monkeypatch):
    monkeypatch.setattr(artifactory.requests, "get", lambda url, **kw: Resp(data={"files": []}))
    with pytest.raises(ArtifactoryError, match="No .swu files"):
        _client().find_latest_swu()
    monkeypatch.setattr(artifactory.requests, "get", lambda url, **kw: Resp(status=401))
    with pytest.raises(ArtifactoryError, match="Authentication failed"):
        _client().find_latest_swu()
    monkeypatch.setattr(artifactory.requests, "get", lambda url, **kw: Resp(status=500))
    with pytest.raises(requests.HTTPError):
        _client().find_latest_swu()


def test_download_latest(monkeypatch, tmp_path, log):
    def get(url, **kw):
        if "api/storage" in url:
            return Resp(data=FILES)
        assert url == "https://af/repo/builds/b/wrynose-2.swu" and kw["stream"]
        return Resp(headers={"content-length": "6"}, chunks=[b"abc", b"", b"def"])

    monkeypatch.setattr(artifactory.requests, "get", get)
    progress = []
    dest = _client().download_latest(tmp_path / "cache", log, progress=lambda s, t: progress.append((s, t)))
    assert dest.read_bytes() == b"abcdef" and not list((tmp_path / "cache").glob("*.part"))
    assert progress == [(3, 6), (6, 6)]
    assert "50%" in log.text("detail") and "Download complete" in log.text("success")
    assert _client().download_latest(tmp_path / "cache", log) == dest
    assert "Already cached" in log.text("success")


def test_download_cancelled(monkeypatch, tmp_path, log):
    monkeypatch.setattr(artifactory.requests, "get", lambda url, **kw: Resp(data=FILES) if "api" in url else Resp(chunks=[b"x"]))
    with pytest.raises(ArtifactoryError, match="cancelled"):
        _client().download_latest(tmp_path / "dl", log, is_cancelled=lambda: True)
    assert not list((tmp_path / "dl").glob("*"))


# -- Jenkins ----------------------------------------------------------------

class Session:
    def __init__(self, gets=(), posts=()):
        self.gets, self.posts = list(gets), list(posts)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        r = self.gets.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.posts.pop(0)


def _jenkins(gets=(), posts=()):
    c = JenkinsClient(JenkinsCredentials("me", "tok"), url="https://j/")
    c.session = Session(gets, posts)
    return c


CRUMB = Resp(data={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"})


def test_trigger_build_waits_for_number(monkeypatch, log):
    monkeypatch.setattr(jenkins.time, "sleep", lambda s: None)
    queued = Resp(data={"why": "Waiting for executor"})
    started = Resp(data={"executable": {"number": 42, "url": "https://j/job/x/42/"}})
    c = _jenkins(gets=[CRUMB, requests.ConnectionError("blip"), queued, Resp(status=500), started],
                 posts=[Resp(status=201, headers={"Location": "https://j/queue/item/5/"})])
    res = c.trigger_build(log)
    assert (res.build_number, res.build_url, res.queue_url) == (42, "https://j/job/x/42/", "https://j/queue/item/5/")
    method, url, kw = c.session.calls[1]
    assert url == "https://j/job/Embedded%20Builder/buildWithParameters"
    assert kw["data"]["PUBLISH_AF2"] == "true" and kw["data"]["Jenkins-Crumb"] == "abc"
    assert "Queued: Waiting for executor" in log.text("detail") and "Build #42 started" in log.text("success")


def test_trigger_build_no_wait_no_csrf():
    c = _jenkins(gets=[Resp(status=404)], posts=[Resp(status=201, headers={"Location": "https://j/queue/1/"})])
    res = c.trigger_build(wait_for_number=False)
    assert res.build_number is None and "Jenkins-Crumb" not in c.session.calls[1][2]["data"]


def test_wait_for_build_number_cancel_and_timeout(monkeypatch, log):
    monkeypatch.setattr(jenkins.time, "sleep", lambda s: None)
    c = _jenkins(gets=[Resp(data={"cancelled": True})])
    assert c._wait_for_build_number("https://j/q/1", log) == (None, None)
    assert "cancelled" in log.text("warning")
    ticks = iter([0, 0, 100])
    monkeypatch.setattr(jenkins.time, "monotonic", lambda: next(ticks))
    c = _jenkins(gets=[Resp(data={})])
    assert c._wait_for_build_number("https://j/q/1", log, timeout=10) == (None, None)
    assert "Timed out" in log.text("warning")
    c = _jenkins(posts=[Resp(status=201)], gets=[CRUMB])
    res = c.trigger_build(log)
    assert res.queue_url is None and "Build triggered successfully" in log.text("success")


@pytest.mark.parametrize("resp, exc, msg", [
    (Resp(status=302, headers={"Location": "https://j/securityRealm/commenceLogin"}), JenkinsLoginRequiredError, "login page"),
    (Resp(status=302, headers={"Location": "https://j/elsewhere"}), JenkinsError, "Unexpected redirect"),
    (Resp(status=401, text="nope", headers={"WWW-Authenticate": "Basic"}), JenkinsError, "Authentication failed fetching"),
    (Resp(status=500, text="x" * 400), JenkinsError, "Failed to fetch CSRF crumb"),
    (Resp(data=ValueError("html")), JenkinsError, "not JSON"),
])
def test_crumb_errors(resp, exc, msg, log):
    with pytest.raises(exc, match=msg):
        _jenkins(gets=[resp]).trigger_build(log)


@pytest.mark.parametrize("resp, exc, msg", [
    (Resp(status=303, headers={"Location": "https://j/login?from=x"}), JenkinsLoginRequiredError, "login page"),
    (Resp(status=401), JenkinsError, "Authentication failed triggering"),
    (Resp(status=403), JenkinsError, "Forbidden"),
    (Resp(status=404), JenkinsError, "not found"),
    (Resp(status=500), JenkinsError, "HTTP 500"),
])
def test_trigger_errors(resp, exc, msg, log):
    with pytest.raises(exc, match=msg):
        _jenkins(gets=[CRUMB], posts=[resp]).trigger_build(log)


def test_describe_response():
    text = jenkins._describe_response(Resp(status=401, text="y" * 400, headers={"WWW-Authenticate": "Basic"}))
    assert text.startswith("HTTP 401 from https://x/api | WWW-Authenticate: Basic | Body:") and "(truncated)" in text
    assert jenkins._describe_response(Resp(status=500)) == "HTTP 500 from https://x/api"
