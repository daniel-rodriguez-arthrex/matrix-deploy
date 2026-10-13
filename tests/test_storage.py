"""secret_store, qa_lock, router_changes and tls_pins: small files on disk."""

import json
import socket

import pytest
import requests

from matrix_deploy import qa_lock, router_changes, secret_store, tls_pins
from matrix_deploy.env_settings import parse_env_file


# -- secret_store -----------------------------------------------------------

@pytest.mark.skipif(not secret_store.AVAILABLE, reason="DPAPI is Windows-only")
def test_dpapi_roundtrip():
    enc = secret_store.protect("p@ss wörd")
    assert enc.startswith("dpapi:") and secret_store.is_protected(enc)
    assert secret_store.protect(enc) == enc and secret_store.protect("") == ""
    assert secret_store.unprotect(enc) == "p@ss wörd"
    assert secret_store.unprotect("dpapi:AAAA") is None  # not ours
    assert secret_store.unprotect("plain") == "plain"


def test_secret_store_off_windows(monkeypatch):
    monkeypatch.setattr(secret_store, "AVAILABLE", False)
    assert secret_store.protect("x") == "x"
    assert secret_store.unprotect("dpapi:abc") is None
    assert secret_store.unprotect("x") == "x"


# -- qa_lock ----------------------------------------------------------------

def test_qa_lock_lifecycle(isolated, monkeypatch):
    sleeps = []
    monkeypatch.setattr(qa_lock.time, "sleep", sleeps.append)
    assert not qa_lock.is_configured() and qa_lock.load_lock() is None
    assert not qa_lock.check_password("anything") and not qa_lock.remembered()
    token = qa_lock.set_password("correct horse battery")
    lock = json.loads(qa_lock.lock_path().read_text())
    assert "correct horse" not in qa_lock.lock_path().read_text() and lock["iterations"] == 1000
    assert qa_lock.is_configured()
    assert qa_lock.check_password("correct horse battery")
    assert not qa_lock.check_password("wrong") and sleeps == [1]
    assert not qa_lock.check_password("")
    assert qa_lock.remember_token("correct horse battery") == token
    assert not qa_lock.remembered()
    qa_lock.save_token(token)
    assert parse_env_file(isolated.env)[qa_lock.TOKEN_KEY] == token and qa_lock.remembered()
    qa_lock.save_token("")
    assert not qa_lock.remembered()
    qa_lock.remove_lock()
    assert not qa_lock.is_configured()
    qa_lock.remove_lock()  # already gone


def test_qa_lock_unreadable_file_stays_locked(isolated):
    qa_lock.lock_path().write_text("garbage", encoding="utf-8")
    assert qa_lock.load_lock() is None and qa_lock.is_configured()
    qa_lock.lock_path().write_text(json.dumps(["x"]), encoding="utf-8")
    assert qa_lock.load_lock() is None
    assert qa_lock._read_lock(None) is None


def test_qa_lock_builtin(isolated, monkeypatch, tmp_path):
    base = tmp_path / "meipass"
    (base / "matrix_deploy").mkdir(parents=True)
    monkeypatch.setattr(qa_lock.sys, "_MEIPASS", str(base), raising=False)
    assert qa_lock.builtin_lock_path() == base / "matrix_deploy" / "qa_lock.cfg"
    assert not qa_lock.has_builtin_lock()
    qa_lock.set_password("built in password")  # writes config/qa_lock.cfg
    (base / "matrix_deploy" / "qa_lock.cfg").write_text(qa_lock.lock_path().read_text(), encoding="utf-8")
    qa_lock.lock_path().unlink()
    assert qa_lock.has_builtin_lock() and qa_lock.is_configured()
    assert qa_lock.check_password("built in password")
    with pytest.raises(ValueError, match="built in"):
        qa_lock.set_password("another password")
    with pytest.raises(ValueError, match="built in"):
        qa_lock.remove_lock()


# -- router_changes -----------------------------------------------------------

R1 = "ip nat inside source static tcp 10.1.1.13 22 interface Gi0/0/0 201"
R2 = "ip nat inside source static tcp 10.1.1.13 8443 interface Gi0/0/0 11001"
R3 = "ip nat inside source static tcp 10.1.2.13 22 interface Gi0/0/0 202"


def test_nat_rules_parsing():
    out = f"Router#show run\n {R1}\nip nat inside source list X interface Gi0 overload\n  {R2}  \n"
    rules = router_changes.nat_rules(out)
    assert sorted(rules.values()) == sorted([R1, R2])


def test_record_and_undo():
    key = "lab|1.1.1.1"
    assert router_changes.load(key) == {"added": [], "removed": []}
    entry = router_changes.record(key, before=R3, after=f"{R1}\n{R2}", commands=[R1, R2, f"no {R3}", "garbage"])
    assert entry == {"added": sorted([R1, R2]), "removed": [R3]}
    assert router_changes.load(key) == entry
    assert router_changes.undo_commands(entry) == [f"no {l}" for l in sorted([R1, R2])] + [R3]
    # Removing one it added and re-adding one it removed cancels both out.
    entry = router_changes.record(key, before=f"{R1}\n{R2}", after=f"{R2}\n{R3}", commands=[f"no {R1}", R3])
    assert entry == {"added": [R2], "removed": []}
    entry = router_changes.record(key, before=R2, after="", commands=[f"no {R2}"])
    assert entry == {"added": [], "removed": []}
    assert key not in json.loads(router_changes.CHANGES_FILE.read_text())
    router_changes.record(key, before="", after=R1, commands=[R1])
    router_changes.forget(key)
    router_changes.forget(key)
    assert router_changes.load(key)["added"] == []


def test_corrupt_changes_file():
    router_changes.CHANGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    router_changes.CHANGES_FILE.write_text("[1, 2]", encoding="utf-8")
    assert router_changes.load("k") == {"added": [], "removed": []}
    router_changes.CHANGES_FILE.write_text("{bad", encoding="utf-8")
    assert router_changes._read_all() == {}


# -- tls_pins -----------------------------------------------------------------

def test_pins(monkeypatch):
    calls = []
    monkeypatch.setattr(tls_pins, "_fetch_fingerprint", lambda h, p, t: calls.append((h, p)) or "ab" * 32)
    assert tls_pins.pin_for("https://1.2.3.4:11001/") == "ab" * 32
    assert tls_pins.pin_for("https://1.2.3.4:11001") == "ab" * 32
    assert tls_pins.pin_for("https://host") == "ab" * 32
    assert calls == [("1.2.3.4", 11001), ("host", 443)]
    session = tls_pins.pinned_session("https://1.2.3.4:11001")
    adapter = session.get_adapter("https://1.2.3.4:11001/api")
    assert isinstance(adapter, tls_pins._PinnedAdapter) and adapter.poolmanager.connection_pool_kw["assert_fingerprint"] == "ab" * 32
    assert tls_pins.forget("1.2.3.4", 11001) and not tls_pins.forget("1.2.3.4", 11001)
    tls_pins.PINS_PATH.write_text("[]", encoding="utf-8")
    assert tls_pins._load() == {}
    tls_pins.PINS_PATH.write_text("{bad", encoding="utf-8")
    assert tls_pins._load() == {}


def test_fetch_fingerprint_errors(monkeypatch):
    def timeout(*a, **k):
        raise socket.timeout("slow")

    def refused(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(tls_pins.socket, "create_connection", timeout)
    with pytest.raises(requests.Timeout):
        tls_pins._fetch_fingerprint("h", 1, 1)
    monkeypatch.setattr(tls_pins.socket, "create_connection", refused)
    with pytest.raises(requests.ConnectionError):
        tls_pins._fetch_fingerprint("h", 1, 1)


def test_fetch_fingerprint_ok(monkeypatch):
    class Tls:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getpeercert(self, binary_form):
            return b"cert"

    class Ctx:
        check_hostname = True
        verify_mode = None

        def wrap_socket(self, sock, server_hostname):
            return Tls()

    monkeypatch.setattr(tls_pins.socket, "create_connection", lambda *a, **k: Tls())
    monkeypatch.setattr(tls_pins.ssl, "create_default_context", Ctx)
    import hashlib
    assert tls_pins._fetch_fingerprint("h", 443, 1) == hashlib.sha256(b"cert").hexdigest()


def test_describe_error():
    msg = tls_pins.describe_error(Exception("Fingerprints did not match"), "https://1.2.3.4:11001")
    assert "1.2.3.4:11001" in msg and "Remove Fingerprint" in msg
    assert tls_pins.describe_error(Exception("boom"), "https://x") == "boom"
