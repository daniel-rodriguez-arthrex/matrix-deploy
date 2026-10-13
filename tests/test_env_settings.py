import sys

import pytest

from matrix_deploy import env_settings as es
from matrix_deploy import secret_store


def test_is_secret_key():
    assert es.is_secret_key("SSH_PASSWORD")
    assert es.is_secret_key("MATRIX_QA_TOKEN")
    assert es.is_secret_key("MATRIX_SWITCH_3_PASSWORD")
    assert es.is_secret_key("SOME_API_KEY")
    assert not es.is_secret_key("ROUTER_IP")


def test_paths(monkeypatch, tmp_path):
    assert es.profile_env_path("config/qa2lab.json").name == "qa2lab.env"
    monkeypatch.undo()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MatrixDeploy.exe"))
    assert es.default_env_path() == tmp_path.resolve() / ".env"
    monkeypatch.delattr(sys, "frozen")
    assert es.default_env_path().name == ".env"


def test_parse_env_file(tmp_path, monkeypatch):
    p = tmp_path / "x.env"
    p.write_text("\ufeff# comment\n\nexport ROUTER_IP=1.2.3.4\nSSH_USERNAME='bob'\nJUNK\nSSH_PASSWORD=\" pw \"\n=x\n"
                 "SUDO_PASSWORD=dpapi:not-base64!!\n", encoding="utf-8")
    raw = es.parse_env_file(p)
    assert raw["ROUTER_IP"] == "1.2.3.4" and raw["SSH_USERNAME"] == "bob" and raw["SSH_PASSWORD"] == " pw "
    assert "SUDO_PASSWORD" not in raw  # can't be decrypted here: as if not saved
    assert es.parse_env_file(tmp_path / "missing.env") == {}
    assert es.load_env_settings(p) == {"router_ip": "1.2.3.4", "username": "bob"}
    assert es.load_env_secrets(p) == {"ssh_password": " pw "}


def test_save_env_values_roundtrip(tmp_path):
    p = tmp_path / "sub" / "lab.env"
    es.save_env_values(p, {})  # nothing to do
    assert not p.exists()
    es.save_env_values(p, {"ssh_password": "s3cret", "router_ip": "1.1.1.1", "username": " padded ", "jenkins_username": ""})
    text = p.read_text()
    assert "SSH_PASSWORD=" in text and ("s3cret" not in text) == secret_store.AVAILABLE
    assert 'SSH_USERNAME=" padded "' in text and "JENKINS_USERNAME" not in text
    assert es.load_env_secrets(p)["ssh_password"] == "s3cret"
    # Updated in place under an alias, other lines untouched.
    p.write_text("# keep\nMATRIX_SSH_PASSWORD=old\nexport ROUTER_IP=2.2.2.2\nnot a line\n", encoding="utf-8")
    es.save_env_values(p, {"ssh_password": "new", "router_ip": "3.3.3.3", "artifactory_email": "'quoted'"})
    lines = p.read_text().splitlines()
    assert lines[0] == "# keep" and lines[1].startswith("MATRIX_SSH_PASSWORD=") and lines[2] == "ROUTER_IP=3.3.3.3"
    assert lines[-1] == "ARTIFACTORY_EMAIL=\"'quoted'\""
    assert es.load_env_secrets(p)["ssh_password"] == "new"
    assert es.load_env_settings(p)["artifactory_email"] == "'quoted'"
    # Empty values never erase, unless allowed.
    es.save_env_values(p, {"router_ip": ""})
    assert es.load_env_settings(p)["router_ip"] == "3.3.3.3"
    es.save_env_values(p, {"router_ip": "", "username": None}, allow_clear=True)
    assert "router_ip" not in es.load_env_settings(p)
    with pytest.raises(ValueError, match="Line breaks"):
        es.save_env_values(p, {"ssh_password": "a\nROUTER_IP=evil"})


def test_format_env_value_quote_choice():
    assert es._format_env_value('"x" ') == "'\"x\" '"
    assert es._format_env_value("plain") == "plain"


@pytest.mark.skipif(not secret_store.AVAILABLE, reason="DPAPI is Windows-only")
def test_protect_env_file(tmp_path):
    p = tmp_path / "lab.env"
    p.write_text("# c\nSSH_PASSWORD=plain\nexport MATRIX_SWITCH_2_PASSWORD='sw'\nROUTER_IP=1.1.1.1\nnoeq\n", encoding="utf-8")
    assert es.protect_env_files([p, tmp_path / "missing.env"]) == 2
    text = p.read_text()
    assert "plain" not in text and "ROUTER_IP=1.1.1.1" in text and text.startswith("# c")
    assert es.parse_env_file(p)["MATRIX_SWITCH_2_PASSWORD"] == "sw"
    assert es.protect_env_file(p) == 0  # already encrypted


def test_protect_env_file_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "AVAILABLE", False)
    p = tmp_path / "lab.env"
    p.write_text("SSH_PASSWORD=plain\n", encoding="utf-8")
    assert es.protect_env_file(p) == 0
    es.save_env_values(p, {"sudo_password": "x"})
    assert "SUDO_PASSWORD=x" in p.read_text()


def test_merged_env_and_switch_passwords(isolated):
    lab = isolated.config / "qa1lab.json"
    lab.write_text("{}", encoding="utf-8")
    assert es.active_env_paths(lab) == (isolated.env, isolated.env)  # no sibling .env yet
    assert es.active_env_paths(None) == (isolated.env, isolated.env)
    es.save_env_values(isolated.env, {"router_ip": "1.1.1.1", "ssh_password": "root-pw", "jenkins_token": "jt"})
    sib = es.profile_env_path(lab)
    es.save_env_values(sib, {"ssh_password": "lab-pw"})
    assert es.active_env_paths(lab) == (isolated.env, sib)
    env, secrets = es.load_merged_env(lab)
    assert env["router_ip"] == "1.1.1.1" and secrets["ssh_password"] == "lab-pw" and secrets["jenkins_token"] == "jt"
    assert not es.switch_passwords_saved(lab, 2)
    sib.write_text(sib.read_text() + "MATRIX_SWITCH_0_PASSWORD=a\nMATRIX_SWITCH_1_PASSWORD=b\n", encoding="utf-8")
    # MATRIX_SWITCH_0_PASSWORD is also the lab-wide alias.
    assert es.switch_passwords_saved(lab, 2)
    sib.write_text("MATRIX_SWITCH_1_PASSWORD=b\n", encoding="utf-8")
    assert not es.switch_passwords_saved(lab, 2)
    assert not es.switch_passwords_saved(lab, 0) and not es.switch_passwords_saved(None, 1)
    sib.write_text("MATRIX_SWITCH_PASSWORD=all\n", encoding="utf-8")
    assert es.switch_passwords_saved(lab, 5)
