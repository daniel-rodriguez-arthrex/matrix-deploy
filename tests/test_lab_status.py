import pytest
import requests

from matrix_deploy import lab_status as ls
from matrix_deploy.config import Room
from matrix_deploy.deployer import DeploymentCredentials
from matrix_deploy.ssh_client import SSHError

from fakes import FakeSSHClient, rules

CREDS = DeploymentCredentials(ssh_password="pw", sudo_password="sudo")


class Resp:
    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code, self._data, self.headers, self.text = status, data, headers or {}, text

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class Http:
    """Fake NMS: login + GraphQL answered by query name."""

    def __init__(self, token="tok", answers=None, login_status=200, valid=("tok",)):
        self.token, self.answers, self.login_status, self.valid = token, answers or {}, login_status, valid
        self.posts = []

    def post(self, url, json=None, headers=None, **kw):
        self.posts.append((url, json, headers, kw))
        if url.endswith("/api/authentication"):
            return Resp(self.login_status, headers={"Authorization": f"Bearer {self.token}"})
        if headers["Authorization"].split()[1] not in self.valid:
            return Resp(401)
        for name, ans in self.answers.items():
            if name in json["query"]:
                return ans if isinstance(ans, Resp) else Resp(data={"data": ans})
        return Resp(data={"data": {}})


def test_nms_base_url(app_config):
    assert ls.nms_base_url(app_config, app_config.room(1)) == "https://172.16.0.5:11001"
    room = Room(number=3, room_id="x", name="OR 3", nms_api_base_url="https://10.0.0.1:11004/")
    assert ls.nms_base_url(app_config, room) == "https://10.0.0.1:11004"


def test_nms_login_variants(app_config):
    class H:
        def __init__(self, resp):
            self.resp = resp

        def post(self, url, **kw):
            assert kw["allow_redirects"] is False and kw["json"]["username"] == "admin"
            return self.resp

    login = lambda resp: ls._nms_login(app_config, "https://n", "pw", H(resp))  # noqa: E731
    assert login(Resp(headers={"Authorization": "Bearer abc"})) == "abc"
    assert login(Resp(data={"token": "def"})) == "def"
    assert login(Resp(data=ValueError("not json"), text="ghi")) == "ghi"
    with pytest.raises(ls.NmsLoginError, match="HTTP 403"):
        login(Resp(403))
    with pytest.raises(ls.NmsLoginError, match="did not return a token"):
        login(Resp(data={}))


def test_nms_query(app_config):
    class H:
        def __init__(self, resp):
            self.resp = resp

        def post(self, url, **kw):
            assert url == "https://n/api/graphql"
            return self.resp

    q = lambda resp: ls._nms_query(app_config, "https://n", "t", "{x}", H(resp))  # noqa: E731
    assert q(Resp(data={"data": {"a": 1}})) == {"a": 1}
    assert q(Resp(data={})) == {}
    with pytest.raises(ls.NmsError, match="HTTP 500"):
        q(Resp(500))
    with pytest.raises(ls.NmsError, match="boom; \\?"):
        q(Resp(data={"errors": [{"message": "boom"}, {}]}))


STATUS = {"devices": [
    {"id": "enc1", "type": "MNA-240", "deviceClass": "encoder", "deviceState": "online", "licensed": True,
     "firmwareVersion": "1.0", "videoSources": [{"id": "s1", "friendlyName": "Cam", "connectorType": "SDI", "connected": True}],
     "videoSinks": [{"id": "k1", "powerState": "ON", "connected": False}]},
    {"id": "dec1", "type": "MNA-420", "deviceClass": "decoder", "deviceState": "offline", "licensed": False,
     "upgradeRequired": True},
], "license": {"features": [{"featureName": "f1", "inGracePeriod": True, "countTotal": 4}]}}

DIAG = {"devicesDiagnosticsV2": [{
    "deviceId": "enc1",
    "system": {"cpuTemperature": 90, "fpgaTemperature": "70", "alerts": [{"code": "CPU_TEMP_TOO_HIGH"}, {"code": "PSU_FAIL"}],
               "fans": [{"name": "Fan1", "speed": 3800, "pwm": 60, "alerts": [{"code": "FAN_SPEED_TOO_LOW"}]},
                        {"name": "Fan2", "speed": 3000, "pwm": 0, "alerts": [{"code": "FAN_STUCK"}]}]},
    "networkInterfaces": [{"name": "sfp0", "rxPower": -5.5, "txPower": -2.0}, {"name": "sfp1", "rxPower": -40, "txPower": -8},
                          {"name": "eth0"}],
}]}

NET = {"devices": [{"id": "enc1", "serialNumber": "SN1",
                    "networkInterfaces": [{"name": "eth0", "ip": "10.1.1.20", "macAddress": "00:04:A5:00:00:01"}, {"name": "x"}]}]}


def test_nms_fetch_fresh_and_cached(app_config, monkeypatch):
    http = Http(answers={"LabStatus": STATUS, "LabDiagnostics": DIAG, "LabNetwork": NET})
    monkeypatch.setattr(ls.tls_pins, "pinned_session", lambda base, t: http)
    data, diags, net = ls._nms_fetch(app_config, "https://n", "pw")
    assert data == STATUS and diags == DIAG["devicesDiagnosticsV2"] and net["enc1"]["serialNumber"] == "SN1"
    assert ls._NMS_TOKENS["https://n"] == "tok"
    logins = sum(p[0].endswith("authentication") for p in http.posts)
    ls._nms_fetch(app_config, "https://n", "pw")  # cached token reused
    assert sum(p[0].endswith("authentication") for p in http.posts) == logins
    # Expired cached token: logs in once more.
    ls._NMS_TOKENS["https://n"] = "old"
    ls._nms_fetch(app_config, "https://n", "pw")
    assert ls._NMS_TOKENS["https://n"] == "tok"


def test_nms_fetch_partial_failures(app_config, monkeypatch):
    http = Http(answers={"LabStatus": STATUS, "LabDiagnostics": Resp(500), "LabNetwork": Resp(500)})
    monkeypatch.setattr(ls.tls_pins, "pinned_session", lambda base, t: http)
    data, diags, net = ls._nms_fetch(app_config, "https://n", "pw")
    assert diags == "NMS query returned HTTP 500" and net == {}
    bad = Http(answers={"LabStatus": Resp(500)})
    monkeypatch.setattr(ls.tls_pins, "pinned_session", lambda base, t: bad)
    with pytest.raises(ls.NmsError):
        ls._nms_fetch(app_config, "https://m", "pw")


def test_summaries_and_grades():
    assert ls._license_summary([])["state"] == "none"
    assert ls._license_summary([{"expired": True}])["state"] == "expired"
    s = ls._license_summary([{"featureName": "a", "permanent": True}])
    assert s["state"] == "active" and s["permanent"] and s["features"][0]["name"] == "a"
    assert ls._alert_text("CPU_TEMP_TOO_HIGH") == "CPU temp too high"
    assert ls._grade_temp("MNA-240", 80)["state"] == "warn" and ls._grade_temp("MNA-420", 96)["state"] == "bad"
    assert ls._grade_temp("other", 99) is None and ls._grade_temp("MNA-240", None) is None
    assert ls._grade_fan(3800, 60)["state"] == "bad" and ls._grade_fan(5800, 60)["state"] == "ok"
    assert ls._grade_fan(4400, 60)["state"] == "warn" and ls._grade_fan(100, 0) is None
    assert ls._grade_sfp(None) is None and ls._grade_sfp(-30) == {"state": "dark"}
    assert ls._grade_sfp(-2)["state"] == "ok" and ls._grade_sfp(-5)["state"] == "warn" and ls._grade_sfp(-7)["state"] == "bad"
    assert ls._number("1.5") == 1.5 and ls._number(True) is None and ls._number("x") is None and ls._number(None) is None
    assert ls._diagnostics_summary(None) is None and ls._diagnostics_summary({"system": {}}) is None


def test_diagnostics_summary():
    d = ls._diagnostics_summary(DIAG["devicesDiagnosticsV2"][0], "MNA-240")
    texts = [f["text"] for f in d["findings"]]
    assert d["state"] == "bad"
    assert "CPU temperature above 85°C (critical)" in texts
    assert "Fan1 speed below 65% of PWM x 100 (critical)" in texts
    assert "sfp0 Rx power -4 to -6.99 dBm (warning)" in texts and "sfp1 Tx power -7 dBm or lower (critical)" in texts
    assert "PSU fail (warning)" in texts and "Fan2: Fan stuck (warning)" in texts
    assert not any(t.startswith("CPU temp too high") for t in texts)  # covered by the graded reading
    assert [p["name"] for p in d["sfp"]] == ["sfp0", "sfp1"] and d["sfp"][1]["rx_grade"] == {"state": "dark"}


def room_client(info=None, eeprom="barco_nms_password = nmspw\n"):
    info = info or ("HOSTNAME=or1\nUPTIME=up 2 days\nAPI=active\nNMS=active\nSWVER=2.0.0\nBUILD=b15\nUPSECS=100.5\n")
    return FakeSSHClient(rules(("act-mfg-eeprom", (eeprom, 0)), ("HOSTNAME", (info, 0))))


def test_collect_room_status_healthy(app_config, monkeypatch):
    client = room_client()
    monkeypatch.setattr(ls, "connect", lambda t: client)
    seen = []
    monkeypatch.setattr(ls, "_nms_fetch", lambda c, b, pw: seen.append(pw) or (
        {"devices": [{"id": "enc1", "type": "MNA-240", "deviceClass": "encoder", "deviceState": "ONLINE", "licensed": True}],
         "license": {"features": [{"featureName": "x"}]}}, [], {"enc1": {"serialNumber": "SN1"}}))
    st = ls.collect_room_status(app_config, CREDS, app_config.room(1))
    assert st["state"] == "healthy" and st["issues"] == [] and seen == ["nmspw"] and client.closed
    assert st["ssh"]["hostname"] == "or1" and isinstance(st["ssh"]["booted_at"], int)
    assert st["software"] == {"version": "2.0.0", "build": "b15"} and st["devices"][0]["serial"] == "SN1"
    assert st["nms"]["ok"] and ls._NMS_PASSWORDS["https://172.16.0.5:11001"] == "nmspw"


def test_collect_room_status_problems(app_config, monkeypatch):
    client = room_client(info="HOSTNAME=or1\nAPI=failed\nNMS=\nUPSECS=x\n")
    monkeypatch.setattr(ls, "connect", lambda t: client)
    monkeypatch.setattr(ls, "_nms_fetch", lambda c, b, pw: (STATUS, "diag broke", {}))
    st = ls.collect_room_status(app_config, CREDS, app_config.room(1))
    assert st["state"] == "critical" or st["state"] == "warning"
    issues = "\n".join(st["issues"])
    assert "Matrix API service (matrix-api) is failed" in issues and "booted_at" not in st["ssh"]
    assert "NMS license is in its grace period" in issues
    assert "Decoder MNA-420 (dec1) is offline" in issues and "is not licensed" in issues and "firmware upgrade" in issues
    assert st["nms"]["diagnostics_error"] == "diag broke" and st["services"]["nms"] == "unknown"

    monkeypatch.setattr(ls, "_nms_fetch", lambda c, b, pw: (STATUS, DIAG["devicesDiagnosticsV2"], {}))
    st = ls.collect_room_status(app_config, CREDS, app_config.room(1))
    assert st["state"] == "critical" and any("critical" in i for i in st["issues"])

    for lic, msg in (([], "No NMS license installed"), ([{"expired": True}], "NMS license expired")):
        monkeypatch.setattr(ls, "_nms_fetch", lambda c, b, pw, lic=lic: ({"license": {"features": lic}}, [], {}))
        assert msg in ls.collect_room_status(app_config, CREDS, app_config.room(1))["issues"]


def test_collect_room_status_without_nms(app_config, monkeypatch):
    def fail(t):
        raise SSHError("No answer")

    monkeypatch.setattr(ls, "connect", fail)
    st = ls.collect_room_status(app_config, CREDS, app_config.room(1))
    assert st["state"] == "unreachable" and st["issues"] == ["Can't SSH in: No answer"]

    monkeypatch.setattr(ls, "connect", lambda t: room_client())
    st = ls.collect_room_status(app_config, DeploymentCredentials(ssh_password="pw"), app_config.room(1))
    assert st["state"] == "warning" and "Needs the sudo password" in st["nms"]["error"]

    secondary = Room(number=2, room_id="10.1.2.13", name="OR 2", secondary=True)
    st = ls.collect_room_status(app_config, CREDS, secondary)
    assert st["state"] == "healthy" and st["nms"]["error"].startswith("Secondary room")

    monkeypatch.setattr(ls, "connect", lambda t: room_client(eeprom="nothing useful\n"))
    st = ls.collect_room_status(app_config, CREDS, app_config.room(1))
    assert "Couldn't read the NMS password" in st["nms"]["error"]

    class Boom(FakeSSHClient):
        def exec_command(self, command, get_pty=False):
            raise OSError("channel closed")

    monkeypatch.setattr(ls, "connect", lambda t: Boom())
    st = ls.collect_room_status(app_config, CREDS, app_config.room(1))
    assert st["state"] == "error" and st["issues"][0] == "SSH checks failed: channel closed"


def test_collect_room_status_not_forwarded_is_a_note(app_config, monkeypatch):
    monkeypatch.setattr(ls, "connect", lambda t: room_client())

    def refuse(*a):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(ls, "_nms_fetch", refuse)
    monkeypatch.setattr(ls, "port_is_open", lambda h, p: False)
    st = ls.collect_room_status(app_config, CREDS, app_config.room(1))
    assert st["state"] == "healthy" and ls.NMS_NOT_FORWARDED in st["notes"][0]


def test_collect_room_status_cached_password(app_config, monkeypatch):
    base = "https://172.16.0.5:11001"
    ls._NMS_PASSWORDS[base] = "cached"
    client = room_client()
    monkeypatch.setattr(ls, "connect", lambda t: client)
    calls = []

    def fetch(c, b, pw):
        calls.append(pw)
        if pw == "cached":
            raise ls.NmsLoginError("rejected")
        return {"license": {"features": [{"featureName": "x"}]}}, [], {}

    monkeypatch.setattr(ls, "_nms_fetch", fetch)
    st = ls.collect_room_status(app_config, CREDS, app_config.room(1))
    # The cached password was rejected (room re-imaged): read again over SSH.
    assert calls == ["cached", "nmspw"] and st["nms"]["ok"] and ls._NMS_PASSWORDS[base] == "nmspw"
    assert any("act-mfg-eeprom" in c for c in client.commands)


def test_nms_error(monkeypatch):
    base = "https://1.2.3.4:11001"
    ls._NMS_PASSWORDS[base], ls._NMS_TOKENS[base] = "p", "t"
    assert ls._nms_error(ls.NmsLoginError("bad login"), base) == "bad login"
    assert base not in ls._NMS_PASSWORDS and base not in ls._NMS_TOKENS
    assert "certificate changed" in ls._nms_error(requests.exceptions.SSLError("fingerprint mismatch"), base)
    monkeypatch.setattr(ls, "port_is_open", lambda h, p: True)
    assert ls._nms_error(requests.ConnectionError("x"), base) == f"NMS not answering at {base}"
    monkeypatch.setattr(ls, "port_is_open", lambda h, p: False)
    assert "NMS port 11001 isn't forwarded" in ls._nms_error(requests.ConnectionError("x"), base)
    assert "NMS port 443" in ls._nms_error(requests.ConnectionError("x"), "https://host")
    assert ls._nms_error(requests.Timeout("slow"), base) == f"NMS timed out at {base}"
    assert ls._nms_error(ValueError("other"), base) == "other"


SFP = {"devicesDiagnosticsV2": [{"deviceId": "enc1", "networkInterfaces": [
    {"name": "sfp0", "ipAddress": "10.1.1.20", "macAddress": "00:04:A5:00:00:01", "rxPower": "-3.1", "txPower": -2,
     "rxErrors": 5, "sfpVendor": "FINISAR", "sfpTemperature": 40, "alerts": [{"code": "SFP_RX_LOW"}]},
    {"name": "noip"}]}, {"networkInterfaces": None}]}


def test_collect_room_sfp(app_config, monkeypatch):
    base = "https://172.16.0.5:11001"
    room = app_config.room(1)
    sec = Room(number=2, room_id="x", name="OR 2", secondary=True)
    assert ls.collect_room_sfp(app_config, CREDS, sec)["error"].startswith("Secondary room")
    assert "Needs the sudo password" in ls.collect_room_sfp(app_config, DeploymentCredentials(), room)["error"]

    monkeypatch.setattr(ls.Deployer, "fetch_nms_password", lambda self, r: self.log("Can't SSH in: down", "error") or None)
    assert ls.collect_room_sfp(app_config, CREDS, room)["error"] == "Can't SSH in: down"
    monkeypatch.setattr(ls.Deployer, "fetch_nms_password", lambda self, r: None)
    assert "act-mfg-eeprom" in ls.collect_room_sfp(app_config, CREDS, room)["error"]

    http = Http(answers={"MatrixDeployFiber": SFP})
    monkeypatch.setattr(ls.tls_pins, "pinned_session", lambda b, t: http)
    monkeypatch.setattr(ls.Deployer, "fetch_nms_password", lambda self, r: "nmspw")
    out = ls.collect_room_sfp(app_config, CREDS, room)
    assert out["number"] == 1 and ls._NMS_PASSWORDS[base] == "nmspw"
    [dev, empty] = out["devices"]
    assert empty == {"id": "", "interfaces": []}
    assert dev["interfaces"] == [{"name": "sfp0", "ip": "10.1.1.20", "mac": "00:04:a5:00:00:01", "rx": -3.1, "tx": -2.0,
                                  "rx_errors": 5.0, "vendor": "FINISAR", "temp": 40.0, "alerts": ["SFP rx low"]}]
    # Cached password + expired token: logs in again.
    ls._NMS_TOKENS[base] = "stale"
    assert ls.collect_room_sfp(app_config, CREDS, room)["devices"]
    http.login_status = 403
    ls._NMS_TOKENS.pop(base)
    out = ls.collect_room_sfp(app_config, CREDS, room)
    assert out["error"] == "NMS login returned HTTP 403" and base not in ls._NMS_PASSWORDS
