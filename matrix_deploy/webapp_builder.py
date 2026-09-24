"""Build the Matrix Electron web app + matrix.api backend from source (Qt-free).

Ported from the standalone ``matrix-electron-web-deployer`` tool. Building
requires Node.js/npm and git on this machine, plus local checkouts of the
internal ``matrix-api-linux`` (backend) and ``matrix-app-linux`` (web app)
repositories - both access-restricted and not part of this repo. Deploying
already-built artifacts (see ``Deployer.deploy_web_app``) has no such
requirement.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

Logger = Callable[[str, str], None]

# Default location of build output relative to each repo, used when the
# caller doesn't override ``local_dist``/``local_web`` explicitly.
DEFAULT_BACKEND_DIST_SUBPATH = Path("dist")
DEFAULT_WEB_DIST_SUBPATH = Path("dist") / "arthrex-synergy-matrix"


def _npm_fallback_dirs() -> List[Path]:
    """Common Node.js install locations to check when npm isn't resolvable
    via PATH (e.g. this process inherited a stale environment)."""
    dirs = [Path("C:\\nvm4w\\nodejs")]
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if not base:
            continue
        dirs.append(Path(base) / "nodejs")
        dirs.append(Path(base) / "npm")
        dirs.append(Path(base) / "Programs" / "nodejs")
    for env_var in ("NVM_SYMLINK", "NVM_HOME"):
        base = os.environ.get(env_var)
        if base:
            dirs.append(Path(base))
    return dirs


_NPM_PATH_CACHE: Optional[str] = None


def _npm() -> str:
    """Resolve a launchable ``npm`` command on Windows.

    ``npm`` on PATH is normally a ``.cmd`` shim, not an ``.exe``. Python's
    subprocess (without ``shell=True``) can only launch it if given the exact
    path *with* the ``.cmd`` extension - a bare ``"npm"`` fails with
    ``FileNotFoundError``/``WinError 2`` (or ``WinError 193`` with the wrong
    extension order on some nvm installs). Always prefer ``npm.cmd`` over the
    extensionless shim. On non-Windows platforms the bare name resolves fine.
    """
    global _NPM_PATH_CACHE
    if _NPM_PATH_CACHE:
        return _NPM_PATH_CACHE

    for name in ("npm.cmd", "npm.exe", "npm"):
        found = shutil.which(name)
        if found:
            _NPM_PATH_CACHE = found
            return found

    for directory in _npm_fallback_dirs():
        for name in ("npm.cmd", "npm.exe"):
            candidate = directory / name
            if candidate.exists():
                _NPM_PATH_CACHE = str(candidate)
                return _NPM_PATH_CACHE

    return "npm"


_HEARTBEAT_SECONDS = 20
_FAILURE_TAIL_LINES = 40
_ENGINE_PKG_RE = re.compile(r"package: '([^']+)'")
_ENGINE_REQ_RE = re.compile(r"required: \{ node: '([^']+)'")
_ENGINE_CUR_RE = re.compile(r"current: \{ node: '([^']+)'")


def _fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _summarize(cmd: list, output: List[str]) -> str:
    """One-line result for a finished step, picked from its output."""
    lines = [l.strip() for l in output if l.strip()]
    if cmd[0] == "git":
        changed = next((l for l in lines if "changed" in l and "file" in l), None)
        return changed or (lines[-1] if lines else "")
    if "install" in cmd:
        result = next((l for l in lines if re.match(r"(added|removed|changed|up to date)\b", l)), "")
        vulns = next((l for l in lines if re.match(r"found \d+ vulnerabilit", l)), "")
        return "; ".join(x for x in (result, vulns) if x)
    return ""


def _engine_warning(output: List[str]) -> Optional[str]:
    """Collapse npm's per-package EBADENGINE blocks into one sentence."""
    packages, required, current = [], [], None
    for line in output:
        if "EBADENGINE" not in line:
            continue
        if m := _ENGINE_PKG_RE.search(line):
            packages.append(m.group(1))
        elif m := _ENGINE_REQ_RE.search(line):
            required.append(m.group(1))
        elif m := _ENGINE_CUR_RE.search(line):
            current = m.group(1)
    if not packages:
        return None
    unique = list(dict.fromkeys(packages))
    example = f"{packages[0]} needs node {required[0]}" if required else packages[0]
    return (
        f"Node {current or '(unknown version)'} is outside the supported range for "
        f"{len(unique)} package(s) - e.g. {example}. The build may fail or behave "
        "differently; consider upgrading Node."
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop a step and everything it spawned. On Windows ``npm`` is a
    ``.cmd`` shim, so terminating ``proc`` alone would leave the real
    ``node`` processes running."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True, timeout=30,
        )
    else:
        proc.terminate()


def _run(
    cmd: list, cwd: Path, log: Logger, is_cancelled: Optional[Callable[[], bool]], label: str
) -> bool:
    """Run one build step quietly: log a start line, a heartbeat while it
    runs, and a one-line summary when it finishes. The raw output is only
    shown (its last lines) if the step fails."""
    log(f"{label}...", "info")
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        log(f"Failed to run {cmd[0]}: {exc}", "error")
        return False

    # Read on a thread so the heartbeat and Cancel still work while a step
    # (e.g. tsc) goes quiet for a minute.
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def _reader() -> None:
        for raw in proc.stdout:
            lines.put(raw.rstrip())
        lines.put(None)

    threading.Thread(target=_reader, daemon=True).start()
    output: List[str] = []
    next_beat = start + _HEARTBEAT_SECONDS
    while True:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            line = ""
        if line is None:
            break
        if line:
            output.append(line)
        if is_cancelled is not None and is_cancelled():
            _kill_tree(proc)
            log("Cancelled.", "warning")
            return False
        now = time.monotonic()
        if now >= next_beat:
            log(f"  still running ({_fmt_elapsed(now - start)})...", "detail")
            next_beat = now + _HEARTBEAT_SECONDS

    code = proc.wait()
    elapsed = _fmt_elapsed(time.monotonic() - start)
    warning = _engine_warning(output)
    if warning:
        log(warning, "warning")
    if code != 0:
        log(f"{label} failed (exit code {code}) after {elapsed}. Last output:", "error")
        for tail_line in output[-_FAILURE_TAIL_LINES:]:
            log(f"  {tail_line}", "detail")
        return False
    summary = _summarize(cmd, output)
    log(f"  done in {elapsed}" + (f" - {summary}" if summary else ""), "detail")
    return True


def _has_uncommitted_changes(repo_dir: Path) -> bool:
    """True if ``repo_dir`` has uncommitted changes to *tracked* files
    (modified/staged/deleted) - the kind that makes ``git pull`` abort a
    merge. Untracked files (``??`` in ``git status --porcelain``, e.g. a
    ``dist/`` or ``node_modules/`` left outside .gitignore) don't block a
    pull, so they're ignored here.

    Used to skip ``git pull`` rather than risk it failing mid-merge or -
    worse - silently discarding local edits (e.g. a deploy-specific config
    tweak) via an automatic stash/checkout.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(
        line and not line.startswith("??")
        for line in result.stdout.splitlines()
    )


def build_repo(repo_dir: Path, log: Logger, is_cancelled: Optional[Callable[[], bool]] = None) -> bool:
    """Run ``git pull`` (best-effort) + ``npm install`` + ``npm run build`` in
    ``repo_dir``.

    ``git pull`` is skipped - with a warning, not a hard failure - when the
    repo has uncommitted local changes, since this tool never stashes or
    discards local edits on the caller's behalf (they may be an intentional,
    deploy-specific tweak). The build proceeds against the working tree as-is.
    """
    repo_dir = Path(repo_dir)
    if not repo_dir.exists():
        log(f"Repo not found: {repo_dir}", "error")
        return False

    npm = _npm()
    try:
        subprocess.run([npm, "--version"], capture_output=True, check=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        log(f"npm is not installed or not in PATH. Tried: {npm} ({exc})", "error")
        return False
    log(f"Repo: {repo_dir}", "detail")
    if "onedrive" in str(repo_dir.resolve()).lower():
        log(
            "This repo is inside OneDrive. OneDrive offloads node_modules to the "
            "cloud, so builds can take many minutes or appear stuck while files "
            "download. Move the repo outside OneDrive (e.g. C:\\repos) for fast builds.",
            "warning",
        )

    steps = [("npm install", [npm, "install"]), ("npm run build", [npm, "run", "build"])]
    if _has_uncommitted_changes(repo_dir):
        log(
            "Skipping git pull: the repo has uncommitted local changes. "
            "Building from the working tree as-is (commit or stash your "
            "changes first if you want the latest remote commits).",
            "warning",
        )
    else:
        steps.insert(0, ("git pull", ["git", "pull"]))

    start = time.monotonic()
    for number, (label, cmd) in enumerate(steps, 1):
        if not _run(cmd, repo_dir, log, is_cancelled, f"[{number}/{len(steps)}] {label}"):
            return False
    log(f"Build complete in {_fmt_elapsed(time.monotonic() - start)}.", "success")
    return True


def _read_package_version(package_json: Path) -> Optional[str]:
    try:
        if not package_json.exists():
            return None
        data = json.loads(package_json.read_text(encoding="utf-8"))
        return data.get("version")
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def get_backend_version(backend_dist: Path) -> Optional[str]:
    """Detect the version from a matrix.api backend dist folder.

    Expects the package.json to live at the parent of ``dist`` (the standard
    node package layout: matrix.api/package.json + matrix.api/dist).
    """
    backend_dist = Path(backend_dist)
    for candidate in (backend_dist.parent / "package.json", backend_dist / "package.json"):
        version = _read_package_version(candidate)
        if version:
            return version
    return None


def get_web_version(web_assets: Path) -> Optional[str]:
    """Detect the version from Angular web assets."""
    web_assets = Path(web_assets)
    candidates = [
        web_assets / "package.json",
        web_assets.parent / "package.json",
        web_assets / "arthrex-synergy-matrix" / "package.json",
    ]
    for candidate in candidates:
        version = _read_package_version(candidate)
        if version:
            return version
    return None


def check_version_compatibility(backend_dist: Path, web_assets: Path, log: Logger) -> Tuple[bool, str]:
    """Compare backend and web asset versions and warn on a major mismatch.

    Returns ``(is_safe, message)``. ``is_safe`` is True when the major
    versions match or when either version cannot be detected - a mismatch is
    logged but not treated as a hard error, since a deploy of a known-working
    combination may be intentional.
    """
    backend_version = get_backend_version(backend_dist)
    web_version = get_web_version(web_assets)

    if not backend_version:
        log("Could not detect backend version from dist.", "warning")
        return True, "Backend version unknown"
    if not web_version:
        log("Could not detect web assets version.", "warning")
        return True, "Web assets version unknown"

    log(f"Backend version: {backend_version}", "detail")
    log(f"Web assets version: {web_version}", "detail")

    backend_major = backend_version.split(".")[0]
    web_major = web_version.split(".")[0]
    if backend_major != web_major:
        message = (
            f"Version mismatch: backend major version {backend_major} does not "
            f"match web assets major version {web_major}. Deploying incompatible "
            "versions may cause runtime errors."
        )
        log(message, "warning")
        return False, message

    log("Backend and web asset major versions match.", "success")
    return True, "Versions match"
