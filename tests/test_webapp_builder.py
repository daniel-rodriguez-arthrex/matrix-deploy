import json
import subprocess
import sys
from pathlib import Path

import pytest

from matrix_deploy import webapp_builder as wb


@pytest.fixture(autouse=True)
def reset_npm_cache(monkeypatch):
    monkeypatch.setattr(wb, "_NPM_PATH_CACHE", None)


def test_npm_on_path(monkeypatch):
    monkeypatch.setattr(wb.shutil, "which", lambda n: r"C:\node\npm.cmd" if n == "npm.cmd" else None)
    assert wb._npm() == r"C:\node\npm.cmd"
    monkeypatch.setattr(wb.shutil, "which", lambda n: None)
    assert wb._npm() == r"C:\node\npm.cmd"  # cached


def test_npm_fallback_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(wb.shutil, "which", lambda n: None)
    for var in ("ProgramFiles", "ProgramFiles(x86)", "APPDATA", "LOCALAPPDATA", "NVM_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NVM_SYMLINK", str(tmp_path / "nvm"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    dirs = wb._npm_fallback_dirs()
    assert tmp_path / "nvm" in dirs and tmp_path / "local" / "Programs" / "nodejs" in dirs
    monkeypatch.setattr(wb, "_npm_fallback_dirs", lambda: [tmp_path / "none"])
    assert wb._npm() == "npm"
    (tmp_path / "nvm").mkdir()
    (tmp_path / "nvm" / "npm.exe").write_text("")
    monkeypatch.setattr(wb, "_npm_fallback_dirs", lambda: [tmp_path / "nvm"])
    assert wb._npm() == str(tmp_path / "nvm" / "npm.exe")


def test_helpers():
    assert wb._fmt_elapsed(5) == "5s" and wb._fmt_elapsed(125) == "2m 05s"
    assert wb._summarize(["git", "pull"], [" 3 files changed, 2 insertions"]) == "3 files changed, 2 insertions"
    assert wb._summarize(["git", "pull"], ["Already up to date."]) == "Already up to date."
    assert wb._summarize(["git", "pull"], []) == ""
    assert wb._summarize(["npm", "install"], ["added 10 packages", "found 0 vulnerabilities"]) == \
        "added 10 packages; found 0 vulnerabilities"
    assert wb._summarize(["npm", "run", "build"], ["x"]) == ""
    out = ["npm warn EBADENGINE Unsupported engine {", "npm warn EBADENGINE   package: 'a@1',",
           "npm warn EBADENGINE   required: { node: '>=20' },", "npm warn EBADENGINE   current: { node: 'v18.0.0' }",
           "npm warn EBADENGINE   package: 'a@1',", "other"]
    assert wb._engine_warning(out) == ("Node v18.0.0 is outside the supported range for 1 package(s) - e.g. a@1 needs "
                                       "node >=20. The build may fail or behave differently; consider upgrading Node.")
    assert "(unknown version)" in wb._engine_warning(["EBADENGINE package: 'b@2'"])
    assert wb._engine_warning(["fine"]) is None


def test_run_success_and_failure(tmp_path, log, monkeypatch):
    monkeypatch.setattr(wb, "_HEARTBEAT_SECONDS", 0)
    ok = wb._run([sys.executable, "-c", "print('added 1 package'); print('EBADENGINE package: \\'z@1\\'')"],
                 tmp_path, log, None, "[1/1] npm install")
    assert ok and "[1/1] npm install..." in log.text("info") and "done in" in log.text("detail")
    assert "still running" in log.text("detail") and "outside the supported range" in log.text("warning")
    assert not wb._run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"], tmp_path, log, None, "step")
    assert "step failed (exit code 3)" in log.text("error") and "  boom" in log.text("detail")
    assert not wb._run(["definitely-not-a-program-xyz"], tmp_path, log, None, "missing")
    assert "Failed to run" in log.text("error")


def test_run_cancel(tmp_path, log):
    assert not wb._run([sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"],
                       tmp_path, log, lambda: True, "slow")
    assert "Cancelled." in log.text("warning")


def test_kill_tree_posix(monkeypatch):
    killed = []
    monkeypatch.setattr(wb.os, "name", "posix")
    wb._kill_tree(type("P", (), {"terminate": lambda self: killed.append(1), "pid": 1})())
    assert killed == [1]


def test_has_uncommitted_changes(monkeypatch, tmp_path):
    def run(out):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out)

    monkeypatch.setattr(wb.subprocess, "run", run("?? dist/\n"))
    assert not wb._has_uncommitted_changes(tmp_path)
    monkeypatch.setattr(wb.subprocess, "run", run(" M src/a.ts\n"))
    assert wb._has_uncommitted_changes(tmp_path)

    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(wb.subprocess, "run", boom)
    assert not wb._has_uncommitted_changes(tmp_path)


def test_build_repo(monkeypatch, tmp_path, log):
    assert not wb.build_repo(tmp_path / "missing", log)
    repo = tmp_path / "onedrive - x" / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setattr(wb, "_npm", lambda: "npm.cmd")
    steps = []
    monkeypatch.setattr(wb.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(wb, "_has_uncommitted_changes", lambda d: False)
    monkeypatch.setattr(wb, "_run", lambda cmd, cwd, log, c, label: steps.append(label) or True)
    assert wb.build_repo(repo, log)
    assert steps == ["[1/3] git pull", "[2/3] npm install", "[3/3] npm run build"]
    assert "inside OneDrive" in log.text("warning") and "Build complete" in log.text("success")
    steps.clear()
    monkeypatch.setattr(wb, "_has_uncommitted_changes", lambda d: True)
    monkeypatch.setattr(wb, "_run", lambda cmd, cwd, log, c, label: steps.append(label) and False)
    assert not wb.build_repo(repo, log)
    assert steps == ["[1/2] npm install"] and "Skipping git pull" in log.text("warning")

    def no_npm(*a, **k):
        raise FileNotFoundError("npm")

    monkeypatch.setattr(wb.subprocess, "run", no_npm)
    assert not wb.build_repo(repo, log) and "npm is not installed" in log.text("error")


def test_resolve_and_folder_problem(tmp_path):
    web = tmp_path / "dist"
    app = web / "arthrex-synergy-matrix"
    app.mkdir(parents=True)
    assert wb.resolve_web_assets(web) == web  # nothing has index.html yet
    (app / "index.html").write_text("")
    assert wb.resolve_web_assets(app) == app and wb.resolve_web_assets(web) == app
    other = tmp_path / "other"
    (other / "zz").mkdir(parents=True)
    (other / "zz" / "index.html").write_text("")
    assert wb.resolve_web_assets(other) == other / "zz"

    assert wb.folder_problem("web_repo", tmp_path / "missing") is None
    assert "no package.json" in wb.folder_problem("backend_repo", tmp_path)
    (tmp_path / "package.json").write_text("{}")
    assert wb.folder_problem("backend_repo", tmp_path) is None
    assert f"use {app} instead" in wb.folder_problem("webapp_web", web)
    assert "build the web app" in wb.folder_problem("webapp_web", tmp_path / "other" / "zz" / ".." / "..")
    assert wb.folder_problem("webapp_web", app) is None
    backend = tmp_path / "api"
    (backend / "dist").mkdir(parents=True)
    assert "build the backend" in wb.folder_problem("webapp_dist", backend)
    (backend / "dist" / "server.js").write_text("")
    assert "use" in wb.folder_problem("webapp_dist", backend)
    assert wb.folder_problem("webapp_dist", backend / "dist") is None
    assert wb.folder_problem("swu_file", tmp_path) is None


def test_versions(tmp_path, log):
    backend = tmp_path / "matrix.api"
    (backend / "dist").mkdir(parents=True)
    web = tmp_path / "web" / "dist"
    web.mkdir(parents=True)
    ok, msg = wb.check_version_compatibility(backend / "dist", web, log)
    assert ok and msg == "Backend version unknown"
    (backend / "package.json").write_text(json.dumps({"version": "14.2.0"}))
    assert wb.get_backend_version(backend / "dist") == "14.2.0"
    assert wb.check_version_compatibility(backend / "dist", web, log) == (True, "Web assets version unknown")
    (web / "package.json").write_text("{bad")
    assert wb.get_web_version(web) is None
    (web.parent / "package.json").write_text(json.dumps({"version": "13.0.0"}))
    ok, msg = wb.check_version_compatibility(backend / "dist", web, log)
    assert not ok and "major version 14 does not match web assets major version 13" in msg
    (web.parent / "package.json").write_text(json.dumps({"version": "14.9.1"}))
    assert wb.check_version_compatibility(backend / "dist", web, log) == (True, "Versions match")
