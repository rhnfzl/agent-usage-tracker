"""Per-machine watermark store. Bounds work on each scan; correctness of dedup comes
from event_id + ON CONFLICT at the sink, so the watermark can be slightly over-inclusive
without ever double-counting.

Layout (JSON): {"collectors": {"<name>": {<private state>}}}; `aut label` keeps its watermark under
the name "label". A process writes back only the names it owns (see save_state).
A collector that changes its file entries keeps a `state_v` in its state and upgrades older state
once, in its own _upgrade_state, when it starts.
The watermark is only committed AFTER a successful push, so a failed push means the next
run re-reads and re-sends (idempotent at the sink).
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator


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
        data = path.read_bytes()                # copy, not move: save_state still wants the path
        with os.fdopen(os.open(kept, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
            fh.write(data)
        detail = f"a copy is at {kept}"
    except OSError as exc:
        detail = f"could not copy it aside ({exc})"
    print(f"aut: warning: the watermark at {path} is unusable ({reason}); {detail}. Starting "
          "clean, so this run re-reads and re-pushes ALL history. The sink will dedup it, but "
          "expect a slow scan.", file=sys.stderr)


def load_state() -> dict[str, Any]:
    """The watermarks. A read error (a root-owned file, EIO) raises AutError: the file may be fine,
    and starting clean would end with save_state replacing it."""
    path = state_path()
    try:
        obj = json.loads(path.read_text())
    except FileNotFoundError:
        return {"collectors": {}}       # first run on this machine: genuinely nothing yet
    except OSError as exc:
        from .config import AutError    # here because config imports this module
        raise AutError(f"aut: could not read {path} ({exc}); fix its owner or mode") from exc
    except ValueError as exc:           # JSONDecodeError and UnicodeDecodeError are ValueErrors
        _warn_unusable(path, str(exc))
        return {"collectors": {}}
    if not isinstance(obj, dict) or not isinstance(obj.get("collectors"), dict):
        _warn_unusable(path, f"unexpected shape: {type(obj).__name__}")
        return {"collectors": {}}
    return obj


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an advisory lock on a file beside `path`, for one short read-merge-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+b") as fh:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # OSError after about 10 s of retries
            try:
                yield
            finally:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)      # closing the file releases it
            yield


def save_state(state: dict[str, Any], names: Iterable[str]) -> None:
    """Write the named collectors' watermarks (a name gone from `state` is dropped) over what is on
    disk now. A scan and a long `aut label run` each hold a copy from when they started; writing
    that copy back whole would roll the other's watermarks back. Two runs of one collector still
    race on its own entry, which costs a re-read at most: the sink dedups on event_id. A read error
    raises, because the file may hold other collectors' good watermarks."""
    path = state_path()
    with _locked(path):
        try:
            disk = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            disk = {}
        if not isinstance(disk, dict):
            disk = {}
        if not isinstance(disk.get("collectors"), dict):
            disk["collectors"] = {}
        for name in names:
            if name in state["collectors"]:
                disk["collectors"][name] = state["collectors"][name]
            else:
                disk["collectors"].pop(name, None)
        atomic_write_text(path, json.dumps(disk, indent=1))


def warn_if_group_readable(path: Path) -> None:
    """For a file that holds a secret. Windows has no POSIX modes, so the check is off there."""
    if os.name != "nt" and path.stat().st_mode & 0o077:
        print(f"aut: warning: {path} holds a secret but is group/other-accessible; "
              f"run: chmod 600 {path}", file=sys.stderr)


def _plain_write_mode(path: Path) -> int:
    """The mode a plain open() leaves: an existing file's own, else the umask's."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)     # the only way to read it
        os.umask(umask)
        return 0o666 & ~umask


def atomic_write_text(path: Path, text: str, *, shared: bool = False) -> None:
    """Write through a unique temp file and a rename, so a crash or a second process never leaves
    a half-written file. Creates the parent directory. The file is private to this user unless
    `shared`, for a file other users or services may read: then it gets the mode a plain write
    would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if shared:
            os.chmod(tmp, _plain_write_mode(path))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json_dict(path: Path) -> dict[str, Any]:
    """A rebuildable cache's contents; {} when it is missing or unusable (it is simply redone)."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def parse_record(raw: bytes | str) -> dict[str, Any] | None:
    """One complete JSONL line as a dict, else None. read_new_lines holds back partial lines, so
    there a None is a real anomaly: the caller counts it as malformed and skips it. A reader that
    only shows local text just skips it, as does one of a file a crash can cut mid-write."""
    try:
        o = json.loads(raw)
    except ValueError:              # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None
    return o if isinstance(o, dict) else None


def collector_wm(state: dict[str, Any], name: str) -> dict[str, Any]:
    """Return (creating if needed) the private watermark dict for one collector. Resets a
    non-dict value (from external corruption) to an empty dict so a scan can't crash on it."""
    collectors = state.setdefault("collectors", {})
    wm = collectors.get(name)
    if not isinstance(wm, dict):
        wm = {}
        collectors[name] = wm
    return wm
