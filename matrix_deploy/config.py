"""Configuration loading and data models for Matrix Deploy."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Room:
    """A single operating room definition."""

    number: int
    room_id: str
    name: str
    # Optional per-room overrides (some rooms use a non-standard NMS port,
    # e.g. OR 3 -> 11004). When set, these are used verbatim.
    nms_ui_url: Optional[str] = None
    nms_api_base_url: Optional[str] = None
    secondary: bool = False

    def ssh_port(self, port_base: int) -> int:
        return port_base + self.number

    def api_url(self) -> str:
        """Inter-operating-room API URL based on room number."""
        return f"https://localhost:{10000 + self.number}"

    def external_api_url(self, router_ip: str) -> str:
        """Externally-reachable API URL through the router's forwarded port.

        Same port scheme as ``api_url`` (10000 + room number) but with the
        router's IP instead of ``localhost``. This is the ``Origin`` a
        browser sends when reaching the room through the router, and must
        appear verbatim (protocol + host + port) in
        ``apiServer.trustedEndPoints`` or module script requests 403.
        """
        return f"https://{router_ip}:{10000 + self.number}"

    def web_app_url(self, router_ip: str) -> str:
        """The room's Matrix web app, served by matrix-api under ``/app/``
        through the router's forwarded port."""
        return f"{self.external_api_url(router_ip)}/app/"

    def nms_url(self) -> str:
        return f"https://{self.room_id}:8443"

    def demonstrator_gui_url(self, router_ip: str, port_base: int = 11000) -> str:
        """NMS demonstrator GUI login URL, reachable through the router on a
        per-room port (base 11000 -> 11001-11012 for rooms 1-12). A room may
        override this with ``nms_ui_url`` for a non-standard port/path."""
        if self.nms_ui_url:
            return self.nms_ui_url
        return (
            f"https://{router_ip}:{port_base + self.number}"
            f"/nms-demonstrator-gui/index.html#/login"
        )

    def trusted_endpoint(self) -> str:
        """Room subnet IP with host id .13, used for apiServer.trustedEndPoints."""
        return f"{self.room_id.rsplit('.', 1)[0]}.13"


@dataclass(frozen=True)
class ConnectionConfig:
    router_ip: str
    ssh_username: str
    ssh_port_base: int = 200
    remote_config_path: str = "/usr/lib/node_modules/matrix.api/matrix.api.config.json"
    remote_nms_config_path: str = "/usr/lib/node_modules/matrix.api/nms-config.json"
    remote_nms_user_config_path: str = "/etc/barco/nms/application-user.yml"
    service_name: str = "matrix-api"
    nms_service_name: str = "barco-nms"
    swu_service_port: int = 8080
    # Chrome DevTools Protocol port the Matrix App (Electron kiosk) exposes on
    # the room's loopback when launched with --remote-debugging-port.
    matrix_app_debug_port: int = 9222
    # Launcher script the Matrix App kiosk is started from; remote debugging is
    # enabled by injecting the debug flags here and relaunching via sway.
    matrix_app_launcher_path: str = "/usr/share/matrix-app/matrix-app-launcher.sh"
    same_physical_host: bool = True
    # Web app (Matrix Electron web app + matrix.api backend) deploy targets,
    # used by Deployer.deploy_web_app/reset_web_app/diagnose_web_app. Ported
    # from the standalone matrix-electron-web-deployer tool.
    remote_webapp_app_folder: str = "/opt/matrix-api-app"
    remote_webapp_node_module: str = "/usr/lib/node_modules/matrix.api"
    webapp_service_unit_path: str = "/usr/lib/systemd/system/matrix-api.service"


@dataclass(frozen=True)
class ArtifactoryBranch:
    """A named build source: which Artifactory folder/filter to pull SWUs from
    (e.g. ``wrynose`` vs ``MatrixG2-2.0``)."""

    label: str
    build_path: str
    branch_filter: str


@dataclass(frozen=True)
class ArtifactoryConfig:
    url: str
    repo: str
    build_path: str
    build_name: str = "Embedded Builder"
    branch_filter: str = "wrynose"
    # Optional named build sources selectable at download time. When empty, a
    # single default entry is synthesized from ``build_path``/``branch_filter``.
    branches: Tuple[ArtifactoryBranch, ...] = ()

    def available_branches(self) -> List[ArtifactoryBranch]:
        """Selectable build sources. Falls back to a single entry derived from
        the top-level ``build_path``/``branch_filter`` when none are configured."""
        if self.branches:
            return list(self.branches)
        return [
            ArtifactoryBranch(
                label=self.branch_filter or "latest",
                build_path=self.build_path,
                branch_filter=self.branch_filter,
            )
        ]


GOLDEN_FILES_DIR = Path(__file__).resolve().parent / "golden_files"

GOLDEN_NMS_CONFIGS = {
    "MAX": GOLDEN_FILES_DIR / "nms-config.max-bandwidth.json",
    "LIMITED": GOLDEN_FILES_DIR / "nms-config.limited-bandwidth.json",
}


def golden_nms_config_path(bandwidth: str) -> Path:
    """Return the bundled golden ``nms-config.json`` for the given bandwidth mode."""
    try:
        return GOLDEN_NMS_CONFIGS[bandwidth]
    except KeyError as exc:
        raise ValueError(f"Unknown bandwidth mode: {bandwidth}") from exc


NMS_USER_CONFIG_TEMPLATE = GOLDEN_FILES_DIR / "application-user.yml.template"


def render_nms_user_config(
    room: "Room", bandwidth_kbps: int, remove_overlay: bool = False
) -> str:
    """Render the barco-nms ``application-user.yml`` for a room with the given
    interop link bandwidth (kbps) applied to both upload and download.

    ``remove_overlay`` controls whether ``nexxis.overlay.noVideoOverlayId`` is
    included; it is independent of the bandwidth setting so bandwidth pushes
    don't silently strip the overlay and the "remove overlay" action doesn't
    silently reset bandwidth.
    """
    text = NMS_USER_CONFIG_TEMPLATE.read_text(encoding="utf-8")
    overlay_line = "    noVideoOverlayId: matrixEmptyOverlay\n" if remove_overlay else ""
    return (
        text.replace("__IP__", room.room_id)
        .replace("__BANDWIDTH__", str(bandwidth_kbps))
        .replace("__OVERLAY_LINE__", overlay_line)
    )


def app_dir() -> Path:
    """Folder holding the user-editable ``config/`` and ``.env``: next to
    ``MatrixDeploy.exe`` when frozen (so a distributed folder is
    self-contained and editable), else the project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _default_config_path() -> Path:
    return app_dir() / "config" / "deploy_config.json"


@dataclass(frozen=True)
class ProfileInfo:
    """A selectable site/lab configuration profile."""

    path: str
    name: str


def config_dir() -> Path:
    """Directory that holds deploy config profiles (siblings of the default)."""
    return _default_config_path().parent


def default_profile_path() -> Path:
    """Best default profile: ``deploy_config.json`` if present, else the first
    discovered profile (alphabetical), else the legacy default path."""
    legacy = _default_config_path()
    if legacy.exists():
        return legacy
    profiles = list_profiles()
    if profiles:
        return Path(profiles[0].path)
    return legacy


def _profile_name(data: dict, path: Path) -> str:
    site = data.get("site")
    if isinstance(site, dict) and site.get("name"):
        return str(site["name"])
    if data.get("site_name"):
        return str(data["site_name"])
    return path.stem


def list_profiles(directory: Optional[Path] = None) -> List[ProfileInfo]:
    """Discover site profiles in the config directory.

    A profile is any ``*.json`` (except the committed example) that has both a
    ``connection`` and a ``rooms`` section. Its display name comes from
    ``site.name``/``site_name`` if present, else the file stem.
    """
    directory = Path(directory) if directory else config_dir()
    profiles: List[ProfileInfo] = []
    if not directory.exists():
        return profiles
    for p in sorted(directory.glob("*.json")):
        if p.name == "deploy_config.example.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if "connection" not in data or "rooms" not in data:
            continue
        profiles.append(ProfileInfo(path=str(p), name=_profile_name(data, p)))
    return profiles


@dataclass
class AppConfig:
    connection: ConnectionConfig
    artifactory: ArtifactoryConfig
    rooms: List[Room] = field(default_factory=list)
    site_name: Optional[str] = None
    path: Optional[str] = None
    _rooms_by_number: Dict[int, Room] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._rooms_by_number = {r.number: r for r in self.rooms}

    def room(self, number: int) -> Optional[Room]:
        return self._rooms_by_number.get(number)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        """Load configuration from a JSON file.

        Defaults to ``config/deploy_config.json`` relative to the project root.
        An optional top-level ``site`` (``{"name": ...}``) names the profile.
        """
        if path is None:
            path = default_profile_path()
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        data = json.loads(path.read_text(encoding="utf-8-sig"))

        conn = ConnectionConfig(**data["connection"])
        arti_data = dict(data["artifactory"])
        branches = tuple(
            ArtifactoryBranch(**b) for b in arti_data.pop("branches", []) or []
        )
        arti = ArtifactoryConfig(branches=branches, **arti_data)
        rooms = [Room(**r) for r in data["rooms"]]
        return cls(
            connection=conn,
            artifactory=arti,
            rooms=rooms,
            site_name=_profile_name(data, path),
            path=str(path),
        )
