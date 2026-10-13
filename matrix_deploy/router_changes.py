"""Which router NAT rules this app added or removed, so they can be undone.

Apply to Router reads the router's static NAT rules before and after the
change; for the rules it touched, the difference is recorded here per lab +
router (``%APPDATA%\\MatrixFacilityManager\\router-changes.json``, no
secrets). ``undo_commands`` turns that back into ``no ...`` / re-add lines,
so the router can be put back the way it was before the app changed it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .facility import parse_nat_line

CHANGES_FILE = Path(os.environ.get("APPDATA") or Path.home()) / "MatrixFacilityManager" / "router-changes.json"
_lock = threading.Lock()


def _rule_key(line: str) -> Optional[Tuple]:
    r = parse_nat_line(line)
    return (r["proto"], r["inside_ip"], r["inside_port"], r["outside"], r["outside_port"]) if r else None


def nat_rules(show_output: str) -> Dict[Tuple, str]:
    """``{rule key: line}`` for the static NAT lines in a ``show`` transcript."""
    out = {}
    for raw in show_output.splitlines():
        line = " ".join(raw.split())
        if (k := _rule_key(line)) is not None:
            out[k] = line
    return out


def _read_all() -> Dict[str, Dict[str, List[str]]]:
    try:
        data = json.loads(CHANGES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_all(data: Dict[str, Dict[str, List[str]]]) -> None:
    CHANGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHANGES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, CHANGES_FILE)


def load(key: str) -> Dict[str, List[str]]:
    entry = _read_all().get(key) or {}
    return {"added": list(entry.get("added") or []), "removed": list(entry.get("removed") or [])}


def record(key: str, before: str, after: str, commands: List[str]) -> Dict[str, List[str]]:
    """Update the record for the rules in ``commands`` from the router's
    rules ``before`` / ``after`` the apply (``show`` transcripts). Adding back
    a rule the app removed (or removing one it added) cancels it out."""
    old, new = nat_rules(before), nat_rules(after)
    touched = {k for c in commands if (k := _rule_key(c[3:] if c.startswith("no ") else c)) is not None}
    with _lock:
        data = _read_all()
        entry = data.get(key) or {}
        added = {k: l for l in entry.get("added") or [] if (k := _rule_key(l)) is not None}
        removed = {k: l for l in entry.get("removed") or [] if (k := _rule_key(l)) is not None}
        for k in touched:
            if k in new and k not in old:
                if removed.pop(k, None) is None:
                    added[k] = new[k]
            elif k in old and k not in new:
                if added.pop(k, None) is None:
                    removed[k] = old[k]
        result = {"added": sorted(added.values()), "removed": sorted(removed.values())}
        if result["added"] or result["removed"]:
            data[key] = result
        else:
            data.pop(key, None)
        _write_all(data)
    return result


def forget(key: str) -> None:
    with _lock:
        data = _read_all()
        if data.pop(key, None) is not None:
            _write_all(data)


def undo_commands(entry: Dict[str, List[str]]) -> List[str]:
    """Remove what the app added, put back what it removed."""
    return [f"no {l}" for l in entry["added"]] + list(entry["removed"])
