"""Find known harnesses from bundle names, commands and data-path metadata, never file contents."""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import islice
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

MAX_PER_ROOT = 512
MAX_TOTAL = 1024
MAX_WALK_PER_ENTRY = 256    # per catalog entry, so a large tree cannot starve later entries
MAX_WALK_DEPTH = 4
MAX_PATHS = 8
MAX_COMMANDS = 4
RECENT_DAYS = 7
_COMMAND = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def application_dirs(home: Path) -> tuple[Path, Path]:
    return Path("/Applications"), home / "Applications"


def command_dirs(home: Path) -> list[Path]:
    # launchd's PATH lacks ~/.local/bin, where several harness CLIs live.
    extra = [Path(part) for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    return [home / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), *extra]


def _strings(value: object, limit: int) -> list[str]:
    return [item for item in value[:limit] if isinstance(item, str)] if isinstance(value, list) else []


def _detect(entry: dict[str, Any], home: Path) -> tuple[list[Path], list[str]]:
    detect = entry.get("detect")
    detect = detect if isinstance(detect, dict) else {}
    paths = [home / raw[2:] for raw in _strings(detect.get("paths"), MAX_PATHS)
             if raw.startswith("~/") and ".." not in Path(raw).parts]
    commands = [name for name in _strings(detect.get("commands"), MAX_COMMANDS)
                if _COMMAND.fullmatch(name)]
    return paths, commands


def _has_command(dirs: list[Path], name: str) -> bool:
    for directory in dirs:
        candidate = directory / name
        try:
            if stat.S_ISREG(os.stat(candidate).st_mode) and os.access(candidate, os.X_OK):
                return True
        except OSError:
            continue
    return False


def _newest(paths: list[Path]) -> tuple[bool, float | None, bool]:
    """(any path exists, newest file mtime, walk finished within bounds). Reads metadata only.
    Only files date activity: a directory's mtime moves when a skill-sync tool adds `skills/`.
    Skips symlinks and `skills` trees, which those tools touch without any agent use."""
    exists, newest, complete, budget = False, None, True, MAX_WALK_PER_ENTRY
    stack: list[tuple[str, int]] = []
    for path in paths:
        try:
            info = os.lstat(path)
        except OSError:
            continue
        exists = True
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            stack.append((str(path), 1))
        else:
            newest = max(newest or 0., info.st_mtime)
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if budget <= 0:
                        return exists, newest, False
                    budget -= 1
                    if entry.is_symlink() or entry.name == "skills":
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < MAX_WALK_DEPTH:
                            stack.append((entry.path, depth + 1))
                        else:
                            complete = False
                        continue
                    try:
                        newest = max(newest or 0., entry.stat(follow_symlinks=False).st_mtime)
                    except OSError:     # gone mid-walk: keep walking the rest of the directory
                        complete = False
        except OSError:
            complete = False
    return exists, newest, complete


def _name(value: str) -> str:
    value = value.casefold()
    if value.endswith(".app"):
        value = value[:-4]
    value = re.sub(r"[^a-z0-9]", "", value)
    return value[:-4] if value.endswith("beta") else value


def _known_names(catalog: list[dict[str, Any]]) -> dict[str, set[str]]:
    known: dict[str, set[str]] = {}
    for entry in catalog[:128]:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", identifier):
            continue
        aliases = entry.get("aliases", [])
        aliases = aliases[:32] if isinstance(aliases, list) else []
        for value in [identifier, entry.get("name"), *aliases]:
            if isinstance(value, str) and len(value) <= 256 and (key := _name(value)):
                known.setdefault(key, set()).add(identifier)
    return known


def discover(home: Path, catalog: list[dict[str, Any]], *, readers: frozenset[str] = frozenset(),
             now: datetime | None = None) -> dict[str, Any]:
    """Match bundle names, probe declared commands and walk declared data paths, metadata only."""
    report: dict[str, Any] = {
        "status": "unknown", "observations": [], "completeness": "unknown", "roots": [],
        "entries_inspected": 0,
        "limits": {"entries_per_root": MAX_PER_ROOT, "entries_total": MAX_TOTAL},
        "claim_scope": "bundle_name_candidates_do_not_prove_installation_or_use",
    }
    if sys.platform != "darwin":
        report["reason"] = "platform_inventory_unavailable"
        return report
    known = _known_names(catalog)
    found: set[str] = set()
    limited = False
    for index, root in enumerate(application_dirs(home)[:2]):
        probe: dict[str, Any] = {"scope": "system" if index == 0 else "user",
                                 "status": "available", "entries_inspected": 0}
        remaining = min(MAX_PER_ROOT, MAX_TOTAL - report["entries_inspected"])
        try:
            if root.is_symlink():
                probe["status"] = "symlink_omitted"
                limited = True
            elif remaining <= 0:
                probe["status"] = "limited"
                limited = True
            else:
                with os.scandir(root) as entries:
                    for entry in islice(entries, remaining):
                        probe["entries_inspected"] += 1
                        report["entries_inspected"] += 1
                        if not entry.name.casefold().endswith(".app"):
                            continue
                        if entry.is_symlink():
                            probe["status"] = "symlinks_omitted"
                            limited = True
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        matches = known.get(_name(entry.name), set())
                        if len(matches) == 1:
                            found.update(matches)
                if probe["entries_inspected"] == remaining:
                    # Reaching the bound leaves completeness unknown without reading another name.
                    probe["status"] = "limited"
                    limited = True
        except FileNotFoundError:
            probe["status"] = "missing"
        except (OSError, ValueError):
            probe["status"] = "unreadable"
            limited = True
        report["roots"].append(probe)
    report["status"] = "limited" if limited else "available"
    evidence: dict[str, set[str]] = {identifier: {"bundle_directory_name"} for identifier in found}
    activity: dict[str, tuple[str, str | None]] = {}
    qualified: set[str] = set()
    dirs = command_dirs(home)
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - RECENT_DAYS * 86400
    for harness in catalog[:128]:
        identifier = harness.get("id") if isinstance(harness, dict) else None
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", identifier):
            continue
        paths, commands = _detect(harness, home)
        if any(_has_command(dirs, name) for name in commands):
            evidence.setdefault(identifier, set()).add("command")
        if set(_strings(harness.get("read_by"), 8)) & readers:
            # Already read by a configured collector: its activity changes nothing, so skip the walk.
            qualified.add(identifier)
            if any(os.path.lexists(path) for path in paths):
                evidence.setdefault(identifier, set()).add("path_metadata")
            continue
        exists, newest, complete = _newest(paths)
        if exists:
            evidence.setdefault(identifier, set()).add("path_metadata")
        if newest is not None and (newest >= cutoff or complete):
            day = datetime.fromtimestamp(newest, timezone.utc).date().isoformat()
            activity[identifier] = ("recent" if newest >= cutoff else "stale", day)
    report["observations"] = [
        {"harness_id": identifier, "evidence": sorted(kinds), "installation": "candidate",
         "activity": activity.get(identifier, ("unknown", None))[0],
         "last_active": activity.get(identifier, ("unknown", None))[1],
         "reader_qualified": identifier in qualified} for identifier, kinds in sorted(evidence.items())]
    return report
