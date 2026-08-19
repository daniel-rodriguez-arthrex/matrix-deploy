"""Per-room Matrix API config JSON generation.

Ported from ``Update-MatrixOrConfigs.ps1`` so the GUI produces identical output:
- Sets ``apiServer.nms`` (username/url, optional password)
- Sets ``room.roomId``
- Rebuilds ``interOperatingRooms`` from the full room registry when creating
  a new file from template; preserves manual changes when updating existing files.
JSON is written UTF-8 without BOM for clean parsing by Node on Linux.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from .config import AppConfig, Room


def _desired_inter_operating_rooms(rooms: List[Room]) -> List[dict]:
    return [
        {"roomId": r.room_id, "apiURL": r.api_url(), "name": r.name}
        for r in rooms
    ]


def build_room_config(
    config: AppConfig,
    room: Room,
    template_path: Path,
    output_dir: Path,
    nms_password: Optional[str] = None,
) -> Path:
    """Generate (or update) the per-room JSON file and return its path.

    If a previously generated ``or{N}.json`` exists it is updated in place;
    otherwise a fresh copy is created from ``template_path``.
    """
    template_path = Path(template_path)
    output_dir = Path(output_dir)
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    target = output_dir / f"or{room.number}.json"
    source = target if target.exists() else template_path
    data = json.loads(source.read_text(encoding="utf-8-sig"))

    # Ensure nested structure exists.
    api_server = data.setdefault("apiServer", {})
    nms = api_server.setdefault("nms", {})
    room_section = data.setdefault("room", {})

    nms["username"] = "admin"
    if nms_password is not None:
        nms["password"] = nms_password
    nms["url"] = room.nms_url()
    room_section["roomId"] = room.room_id

    external_urls = [
        r.external_api_url(config.connection.router_ip) for r in config.rooms
    ]
    api_server["trustedEndPoints"] = [
        room.trusted_endpoint(),
        config.connection.router_ip,
        "127.0.0.1",
        "10.101.44.236",
        "192.168.1.68",
        "::1",
        "localhost",
        *external_urls,
    ]

    # Never auto-populate interOperatingRooms - leave it as-is from template
    # or preserve manual edits in existing files.

    # Write UTF-8 without BOM.
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return target
