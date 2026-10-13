import json
import sys

import pytest

from matrix_deploy import config
from matrix_deploy.config import AppConfig, ArtifactoryBranch, ArtifactoryConfig, Room

from conftest import profile_data, write_profile


def test_room_ports_and_urls():
    r = Room(number=3, room_id="10.1.3.13", name="OR 3")
    assert r.ssh_port(200) == 203
    assert r.swu_external_port() == 303
    assert r.api_external_port() == 10003
    assert r.nms_external_port() == 11003
    assert r.api_url() == "https://localhost:10003"
    assert r.external_api_url("1.2.3.4") == "https://1.2.3.4:10003"
    assert r.web_app_url("1.2.3.4") == "https://1.2.3.4:10003/app/"
    assert r.nms_url() == "https://10.1.3.13:8443"
    assert r.demonstrator_gui_url("1.2.3.4") == "https://1.2.3.4:11003/nms-demonstrator-gui/index.html#/login"
    assert r.trusted_endpoint() == "10.1.3.13"


def test_room_nms_override():
    r = Room(number=3, room_id="10.1.3.20", name="OR 3", nms_ui_url="https://x:11004/ui")
    assert r.demonstrator_gui_url("1.2.3.4") == "https://x:11004/ui"
    assert r.trusted_endpoint() == "10.1.3.13"


def test_artifactory_branches():
    cfg = ArtifactoryConfig(url="u", repo="r", build_path="b", branch_filter="")
    [only] = cfg.available_branches()
    assert (only.label, only.build_path, only.branch_filter) == ("latest", "b", "")
    named = ArtifactoryConfig(url="u", repo="r", build_path="b",
                              branches=(ArtifactoryBranch("a", "pa", "fa"), ArtifactoryBranch("b", "pb", "fb")))
    assert [b.label for b in named.available_branches()] == ["a", "b"]


def test_golden_paths():
    assert config.golden_nms_config_path("MAX").name == "nms-config.max-bandwidth.json"
    with pytest.raises(ValueError, match="Unknown bandwidth"):
        config.golden_nms_config_path("HUGE")


def test_render_nms_user_config():
    room = Room(number=1, room_id="10.1.1.13", name="OR 1")
    text = config.render_nms_user_config(room, 1234)
    assert "ip: 10.1.1.13" in text and "upload: 1234" in text and "noVideoOverlayId" not in text
    assert "__" not in text
    assert "noVideoOverlayId: matrixEmptyOverlay" in config.render_nms_user_config(room, 1, remove_overlay=True)


def test_app_dir_frozen(monkeypatch, tmp_path):
    monkeypatch.undo()  # the real app_dir, not the isolated one
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MatrixDeploy.exe"))
    assert config.app_dir() == tmp_path.resolve()
    monkeypatch.delattr(sys, "frozen")
    assert (config.app_dir() / "matrix_deploy").is_dir()


def test_list_profiles_and_default(isolated):
    cfg = isolated.config
    assert config.config_dir() == cfg
    assert config.default_profile_path() == cfg / "deploy_config.json"  # nothing yet
    write_profile(cfg, "zlab", name="Z Lab")
    data = profile_data()
    data.pop("site")
    data["site_name"] = "Legacy name"
    (cfg / "alab.json").write_text(json.dumps(data), encoding="utf-8")
    plain = profile_data()
    plain.pop("site")
    (cfg / "mlab.json").write_text(json.dumps(plain), encoding="utf-8")
    (cfg / "deploy_config.example.json").write_text(json.dumps(profile_data()), encoding="utf-8")
    (cfg / "broken.json").write_text("{nope", encoding="utf-8")
    (cfg / "other.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    profiles = config.list_profiles()
    assert [(p.name, p.path.endswith(f)) for p, f in zip(profiles, ("alab.json", "mlab.json", "zlab.json"))] == [
        ("Legacy name", True), ("mlab", True), ("Z Lab", True)]
    assert config.default_profile_path().name == "alab.json"
    (cfg / "deploy_config.json").write_text(json.dumps(profile_data()), encoding="utf-8")
    assert config.default_profile_path().name == "deploy_config.json"
    assert config.list_profiles(cfg / "missing") == []


def test_load(isolated):
    path = write_profile(isolated.config, rooms=[
        {"number": 1, "room_id": "10.1.1.13", "name": "OR 1"},
        {"number": 5, "room_id": "10.1.5.13", "name": "OR 5", "secondary": True}], facility={"site_code": "QA1"})
    raw = json.loads(path.read_text())
    raw["artifactory"]["branches"] = [{"label": "w", "build_path": "p", "branch_filter": "w"}]
    path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = AppConfig.load(path)
    assert cfg.site_name == "QA Lab 1" and cfg.path == str(path)
    assert cfg.room(5).secondary and cfg.room(9) is None
    assert cfg.connection.router_ip == "172.16.0.5" and cfg.facility == {"site_code": "QA1"}
    assert cfg.artifactory.branches[0].label == "w"
    with pytest.raises(FileNotFoundError):
        AppConfig.load(isolated.config / "nope.json")
    assert AppConfig.load().path == str(path)  # default = first profile


@pytest.mark.parametrize("arti", [{}, None, "missing"])
def test_load_without_artifactory(isolated, arti):
    data = profile_data()
    if arti == "missing":
        data.pop("artifactory")
    else:
        data["artifactory"] = arti
    path = isolated.config / "lab.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    a = AppConfig.load(path).artifactory
    assert (a.url, a.repo, a.build_path) == ("", "", "")


def test_standalone():
    cfg = AppConfig.standalone()
    assert cfg.rooms == [] and cfg.path is None and "device checks" in cfg.site_name
