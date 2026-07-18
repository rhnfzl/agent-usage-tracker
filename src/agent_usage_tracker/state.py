"""Per-machine watermark store. Bounds work on each scan; correctness of dedup comes
from event_id + ON CONFLICT at the sink, so the watermark can be slightly over-inclusive
without ever double-counting.

Layout (JSON): {"collectors": {"<name>": {<collector-private state>}}}
The watermark is only committed AFTER a successful push, so a failed push means the next
run re-reads and re-sends (idempotent at the sink).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _state_path() -> Path:
    if os.environ.get("AUT_STATE"):
        return Path(os.environ["AUT_STATE"]).expanduser()
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base) / "agent-usage-tracker" / "state.json"


def load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {"collectors": {}}
    try:
        obj = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"collectors": {}}
    if not isinstance(obj, dict) or not isinstance(obj.get("collectors"), dict):
        return {"collectors": {}}   # corrupt / unexpected shape -> start clean
    return obj


def save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # atomic write: tmp + rename, so a crash never leaves a half-written watermark.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def collector_wm(state: dict, name: str) -> dict:
    """Return (creating if needed) the private watermark dict for one collector. Resets a
    non-dict value (from external corruption) to an empty dict so a scan can't crash on it."""
    collectors = state.setdefault("collectors", {})
    wm = collectors.get(name)
    if not isinstance(wm, dict):
        wm = {}
        collectors[name] = wm
    return wm
