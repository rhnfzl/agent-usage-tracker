"""Record Git context before an opted-in Codex turn sends work to the model."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import AutError
from ..event import parse_ts
from ..state import _locked, atomic_write_text
from .file_reads import path_fingerprint
from .setup_git import COMMIT_ID, _read_git

if TYPE_CHECKING:
    from .store import Store

HOOK_INPUT_LIMIT = 2 * 1024 * 1024
RECORD_KIND = "codex_turn_revisions"
_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_RECORD_LIMIT = 2048


def _key(session: str, turn: str) -> str:
    from .store import digest

    return digest([session, turn])


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        raw = source.read(_RECORD_LIMIT + 1)
    record = json.loads(raw) if len(raw) <= _RECORD_LIMIT else None
    if not isinstance(record, dict):
        raise ValueError("invalid Codex turn receipt")
    return record


def record_hook(root: Path, project: Path, raw: bytes) -> None:
    """Write only the workspace fingerprint, revision, and timing for this turn."""
    from .store import Store, canonical

    try:
        if len(raw) > HOOK_INPUT_LIMIT:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
            return
        session, turn, cwd = (payload.get(field) for field in ("session_id", "turn_id", "cwd"))
        if (not isinstance(session, str) or not _ID.fullmatch(session)
                or not isinstance(turn, str) or not _ID.fullmatch(turn)
                or not isinstance(cwd, str) or "\0" in cwd or not Path(cwd).is_absolute()):
            return
        workspace = Path(cwd)
        git_dir_args = ("--path-format=absolute", "--git-common-dir")
        git_dir, revision = _read_git(workspace, "rev-parse", *git_dir_args, "--verify",
                                      "HEAD^{commit}").rstrip(b"\n").rsplit(b"\n", 1)
        if (not COMMIT_ID.fullmatch(commit := revision.decode("ascii"))
                or git_dir != _read_git(project, "rev-parse", *git_dir_args).removesuffix(b"\n")):
            return
        store = Store(root)
        key = _key(session, turn)
        path = store.path(RECORD_KIND, key)
        state = {"workspace_sha256": path_fingerprint(workspace), "commit": commit}
        record: dict[str, Any] = {
            "turn_key": key, **state,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        with _locked(store.root / RECORD_KIND):
            try:
                previous = _load(path)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                if previous.get("conflict") or all(previous.get(key) == value for key, value in state.items()):
                    return
                record = {"turn_key": key, "conflict": True}
            atomic_write_text(path, canonical(record) + "\n")
    except (AutError, OSError, subprocess.SubprocessError, ValueError):
        # Missing context lowers measurement coverage and leaves the user's turn running.
        return


def lookup(store: Store, session: str, turn: str) -> dict[str, Any]:
    key = _key(session, turn)
    try:
        record = _load(store.path(RECORD_KIND, key))
        return record if record.get("turn_key") == key else {}
    except (AutError, OSError, ValueError):
        return {}


def details(record: object) -> dict[str, str] | None:
    if not isinstance(record, dict):
        return None
    commit, observed = record.get("commit"), parse_ts(record.get("recorded_at"))
    if not isinstance(commit, str) or not COMMIT_ID.fullmatch(commit) or not observed or not observed.tzinfo:
        return None
    return {"commit": commit, "recorded_at": observed.isoformat()}


def match(record: dict[str, Any], cwd: str, started: datetime,
          before: datetime | None) -> dict[str, str] | None:
    """Join the receipt to the recorded workspace and native turn window."""
    visible = details(record)
    if visible is None or record.get("workspace_sha256") != path_fingerprint(Path(cwd)):
        return None
    observed = datetime.fromisoformat(visible["recorded_at"])
    return visible if before and before.tzinfo and started <= observed <= before else None
