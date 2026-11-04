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
import sys
import tempfile
from pathlib import Path


def state_path() -> Path:
    if os.environ.get("AUT_STATE"):
        return Path(os.environ["AUT_STATE"]).expanduser()
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base) / "agent-usage-tracker" / "state.json"


def _warn_unusable(path: Path, reason: str) -> None:
    """Starting clean is the right recovery, but doing it silently is not: losing the watermark
    means the next scan re-reads every log from byte 0 and re-pushes all history. The sink dedups
    that on event_id, so nothing breaks, but on a busy machine it is a multi-minute scan and a
    lot of redundant rows -- and if the state keeps going bad, silence hides it happening every
    single run. Copy the file aside first, because save_state() overwrites it at the end of this
    same run and would otherwise destroy the only evidence of what went wrong."""
    kept = path.with_suffix(".corrupt")
    try:
        kept.write_bytes(path.read_bytes())     # copy, not move: save_state still wants the path
        detail = f"a copy is at {kept}"
    except OSError as exc:
        detail = f"could not copy it aside ({exc})"
    print(f"aut: warning: the watermark at {path} is unusable ({reason}); {detail}. Starting "
          "clean, so this run re-reads and re-pushes ALL history. The sink will dedup it, but "
          "expect a slow scan.", file=sys.stderr)


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {"collectors": {}}       # first run on this machine: genuinely nothing yet
    try:
        obj = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _warn_unusable(path, str(exc))
        return {"collectors": {}}
    if not isinstance(obj, dict) or not isinstance(obj.get("collectors"), dict):
        _warn_unusable(path, f"unexpected shape: {type(obj).__name__}")
        return {"collectors": {}}
    return obj


def save_state(state: dict) -> None:
    atomic_write_text(state_path(), json.dumps(state, indent=1))


def warn_if_group_readable(path: Path) -> None:
    """For a file that holds a secret. Windows has no POSIX modes, so the check is off there."""
    if os.name != "nt" and path.stat().st_mode & 0o077:
        print(f"aut: warning: {path} holds a secret but is group/other-accessible; "
              f"run: chmod 600 {path}", file=sys.stderr)


def atomic_write_text(path: Path, text: str) -> None:
    """Write through a unique temp file and a rename, so a crash or a second process never leaves
    a half-written file. Creates the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json_dict(path: Path) -> dict:
    """A rebuildable cache's contents; {} when it is missing or unusable (it is simply redone)."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def collector_wm(state: dict, name: str) -> dict:
    """Return (creating if needed) the private watermark dict for one collector. Resets a
    non-dict value (from external corruption) to an empty dict so a scan can't crash on it."""
    collectors = state.setdefault("collectors", {})
    wm = collectors.get(name)
    if not isinstance(wm, dict):
        wm = {}
        collectors[name] = wm
    return wm
