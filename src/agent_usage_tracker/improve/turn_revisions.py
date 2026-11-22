"""Record Git context before an opted-in Codex turn sends work to the model."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

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
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECORD_LIMIT = 4096
_FILE_LIMIT = 1024 * 1024
SETUP_FILE_LIMIT = 16


class TurnRevision(TypedDict):
    commit: str
    recorded_at: str
    setup_files: NotRequired[dict[str, str | None]]


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


def _target(value: str) -> PurePosixPath:
    target = PurePosixPath(value)
    if (not target.parts or target.is_absolute() or ".." in target.parts or "\\" in value or ":" in value
            or any(part.casefold() == ".git" for part in target.parts)
            or any(ord(char) < 32 for char in value)):
        raise ValueError("expected a setup file path relative to the project")
    return target


def _file_digest(root: Path, parts: tuple[str, ...]) -> str | None:
    path = root
    try:
        for part in parts[:-1]:
            path /= part
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError("setup path contains a symlink or non-directory")
        path /= parts[-1]
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > _FILE_LIMIT:
        raise ValueError("setup path is not a bounded regular file")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size > _FILE_LIMIT
                or not os.path.samestat(before, opened)):
            raise ValueError("setup file changed while opening")
        contents = source.read(_FILE_LIMIT + 1)
        after = os.fstat(source.fileno())
    if (len(contents) != before.st_size or not os.path.samestat(before, path.lstat())
            or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)
            or path.resolve(strict=True) != path):
        raise ValueError("setup file changed while reading")
    return hashlib.sha256(contents).hexdigest()


def _setup_files(worktree: Path, prefix: bytes,
                 targets: tuple[PurePosixPath, ...]) -> tuple[set[str], dict[str, str | None]]:
    files: dict[str, str | None] = {}
    selected: set[str] = set()
    if prefix:
        parent = _target(os.fsdecode(prefix)).parts
    else:
        parent = ()
    for target in targets:
        parts = parent + target.parts
        key = path_fingerprint(worktree.joinpath(*parts))
        selected.add(key)
        try:
            files[key] = _file_digest(worktree, parts)
        except (OSError, ValueError, RuntimeError):
            continue
    return selected, files


def _saved_files(record: dict[str, Any]) -> dict[str, str | None]:
    files = record.get("setup_files")
    if not isinstance(files, dict) or len(files) > SETUP_FILE_LIMIT:
        return {}
    return {key: value for key, value in files.items()
            if isinstance(key, str) and _SHA256.fullmatch(key)
            and (value is None or isinstance(value, str) and _SHA256.fullmatch(value))}


def record_hook(root: Path, project: Path, raw: bytes, setup_files: tuple[str, ...] = ()) -> None:
    """Record Git metadata and optional digests of explicitly selected setup files."""
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
        if len(setup_files) > SETUP_FILE_LIMIT:
            raise ValueError("select up to 16 setup paths")
        targets = tuple(_target(value) for value in setup_files)
        git_dir_args = ("--path-format=absolute", "--git-common-dir")
        git_dir, top, revision = _read_git(workspace, "rev-parse", *git_dir_args, "--show-toplevel",
                                           "--verify", "HEAD^{commit}").removesuffix(b"\n").split(b"\n")
        project_args = (*git_dir_args, "--show-prefix") if targets else git_dir_args
        project_dir, _, prefix = _read_git(project, "rev-parse", *project_args).removesuffix(b"\n").partition(b"\n")
        if (not COMMIT_ID.fullmatch(commit := revision.decode("ascii"))
                or git_dir != project_dir):
            return
        worktree = Path(os.fsdecode(top)).resolve(strict=True)
        selected, files = _setup_files(worktree, prefix, targets)
        if files and _read_git(workspace, "rev-parse", "--verify", "HEAD^{commit}").strip() != revision:
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
            if previous is not None and previous.get("conflict"):
                return
            if previous is not None and (prior := details(previous)) is not None:
                earlier_files = prior.get("setup_files", {})
                if (any(previous.get(name) != value for name, value in state.items())
                        or earlier_files.keys() & (selected - files.keys())
                        or any(earlier_files[name] != files[name] for name in earlier_files.keys() & files.keys())
                        or len(earlier_files.keys() | files.keys()) > SETUP_FILE_LIMIT):
                    atomic_write_text(path, canonical({"turn_key": key, "conflict": True}) + "\n")
                    return
                files = earlier_files | files
                if files == earlier_files:
                    return
                record["recorded_at"] = max(record["recorded_at"], prior["recorded_at"])
            if files:
                record["setup_files"] = files
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


def details(record: object) -> TurnRevision | None:
    if not isinstance(record, dict):
        return None
    commit, observed = record.get("commit"), parse_ts(record.get("recorded_at"))
    if not isinstance(commit, str) or not COMMIT_ID.fullmatch(commit) or not observed or not observed.tzinfo:
        return None
    result: TurnRevision = {"commit": commit, "recorded_at": observed.isoformat()}
    if files := _saved_files(record):
        result["setup_files"] = files
    return result


def match(record: dict[str, Any], cwd: str, started: datetime,
          before: datetime | None) -> TurnRevision | None:
    """Join the receipt to the recorded workspace and native turn window."""
    visible = details(record)
    if visible is None or record.get("workspace_sha256") != path_fingerprint(Path(cwd)):
        return None
    observed = datetime.fromisoformat(visible["recorded_at"])
    return visible if before and before.tzinfo and started <= observed <= before else None
