"""Configuration loading and data models for Matrix Deploy."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Room:
    """A single operating room definition."""

    number: int
    room_id: str
    name: str

    def ssh_port(self, port_base: int) -> int:
        return port_base + self.number

    def api_url(self) -> str:
        """Inter-operating-room API URL based on room number."""
        return f"https://localhost:{10000 + self.number}"

    def nms_url(self) -> str:
        return f"https://{self.room_id}:8443"


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
    same_physical_host: bool = True


@dataclass(frozen=True)
class ArtifactoryConfig:
    url: str
    repo: str
    build_path: str
    build_name: str = "Embedded Builder"
    branch_filter: str = "wrynose"


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


def render_nms_user_config(room: "Room", bandwidth_kbps: int) -> str:
    """Render the barco-nms ``application-user.yml`` for a room with the given
    interop link bandwidth (kbps) applied to both upload and download."""
    text = NMS_USER_CONFIG_TEMPLATE.read_text(encoding="utf-8")
    return text.replace("__IP__", room.room_id).replace(
        "__BANDWIDTH__", str(bandwidth_kbps)
    )


def _default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidate = exe_dir / "config" / "deploy_config.json"
        if candidate.exists():
            return candidate
        bundle_dir = Path(getattr(sys, "_MEIPASS", exe_dir))
        bundled = bundle_dir / "config" / "deploy_config.json"
        if bundled.exists():
            return bundled
    return Path(__file__).resolve().parent.parent / "config" / "deploy_config.json"


@dataclass
class AppConfig:
    connection: ConnectionConfig
    artifactory: ArtifactoryConfig
    rooms: List[Room] = field(default_factory=list)
    _rooms_by_number: Dict[int, Room] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._rooms_by_number = {r.number: r for r in self.rooms}

    def room(self, number: int) -> Optional[Room]:
        return self._rooms_by_number.get(number)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        """Load configuration from a JSON file.

        Defaults to ``config/deploy_config.json`` relative to the project root.
        """
        if path is None:
            path = _default_config_path()
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        data = json.loads(path.read_text(encoding="utf-8"))

        conn = ConnectionConfig(**data["connection"])
        arti = ArtifactoryConfig(**data["artifactory"])
        rooms = [Room(**r) for r in data["rooms"]]
        return cls(connection=conn, artifactory=arti, rooms=rooms)
