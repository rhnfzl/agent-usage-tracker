"""Private content-addressed evidence and an append-only progress journal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..collectors import claude_code, codex
from ..config import AutError
from ..git_safe import git_argv, git_env
from ..state import _locked, atomic_write_text
from .evidence import read_session


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        marker = self.root / ".aut-improve"
        if self.root.exists() and any(self.root.iterdir()) and not marker.is_file():
            raise AutError("improve: refusing a nonempty directory without its private marker")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if marker.exists() and marker.read_text() != "1\n":
            raise AutError("improve: unsupported private store version")
        if not marker.exists():
            atomic_write_text(marker, "1\n")
        atomic_write_text(self.root / ".gitignore", "*\n")

    def path(self, kind: str, key: str) -> Path:
        if not re.fullmatch(r"[a-z_]{1,40}", kind) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise AutError("improve: invalid record identity")
        path = self.root / kind / f"{key}.json"
        if not path.resolve().is_relative_to(self.root):
            raise AutError("improve: record path escapes its private store")
        return path

    def put(self, kind: str, value: dict[str, Any]) -> str:
        try:
            if not isinstance(value, dict):
                raise ValueError("record is not an object")
            key = digest(value)
        except (TypeError, ValueError) as exc:
            raise AutError("improve: record must be a finite JSON object") from exc
        path = self.path(kind, key)
        with _locked(self.root / "write"):
            if path.exists():
                self.get(kind, key)
            else:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                atomic_write_text(path, canonical(value) + "\n")
        return key

    def get(self, kind: str, key: str) -> dict[str, Any]:
        path = self.path(kind, key)
        try:
            value = json.loads(path.read_text())
            valid = isinstance(value, dict) and digest(value) == key
        except (OSError, TypeError, ValueError) as exc:
            raise AutError("improve: private record is missing or invalid") from exc
        if not valid:
            raise AutError("improve: private record changed; refusing to continue")
        return value

    def keys(self, kind: str) -> list[str]:
        self.path(kind, "0" * 64)
        return sorted(p.stem for p in (self.root / kind).glob("*.json"))

    def append(self, kind: str, **attrs: Any) -> None:
        row = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **attrs}
        with _locked(self.root / "journal"):
            fd = os.open(self.root / "events.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(canonical(row) + "\n")
                stream.flush()
                os.fsync(stream.fileno())


def repository(path: Path) -> str | None:
    try:
        result = subprocess.run(git_argv(str(path), "rev-parse", "--path-format=absolute",
                                         "--git-common-dir"),
                                capture_output=True, text=True, timeout=5, env=git_env())
        return str(Path(result.stdout.strip()).resolve()) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def audit(store: Store, home: Path, *, days: int = 7, limit: int = 40,
          project: Path | None = None, idle_s: int = 120,
          since_epoch: float | None = None, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    if not 1 <= days <= 90 or not 1 <= limit <= 200 or idle_s < 0:
        raise AutError("improve: days must be 1..90 and session limit 1..200")
    now = time.time()
    since = since_epoch if since_epoch is not None else now - days * 86400
    files: list[tuple[float, str, Path]] = []
    counts: Counter[str] = Counter()
    for agent, discover in (("claude_code", claude_code.main_session_files),
                            ("codex", codex.session_files)):
        for name in discover(home):
            path = Path(name)
            try:
                stamp = path.stat().st_mtime
                if stamp >= since:
                    if stamp > now - idle_s:
                        counts["active_sources"] += 1
                    else:
                        files.append((stamp, agent, path))
            except OSError:
                counts["unreadable_sources"] += 1
    sessions: list[str] = []
    imports = codex.imported_until(home)
    seen: set[tuple[str, str]] = set()
    identities: dict[tuple[str, str], str] = {}
    copies: dict[tuple[str, str], list[str]] = {}
    project_repo = repository(project) if project else None
    if project and project_repo is None:
        raise AutError("improve: project must be a readable local Git repository")
    for _stamp, agent, path in sorted(files, reverse=True):
        if len(sessions) >= limit:
            counts["unread_sources"] += 1
            continue
        try:
            evidence = read_session(path, agent, max_bytes=max_bytes, since_epoch=since,
                                    imports=imports, exclude_automated=True)
        except (OSError, ValueError, TypeError, OverflowError):
            counts["unreadable_sources"] += 1
            continue
        if evidence is None or evidence["origin"] in {"subagent", "automated"}:
            counts["automated_sources"] += 1
            continue
        if not evidence["events"]:
            counts["no_recent_evidence"] += 1
            continue
        if project and (not evidence["cwd"] or repository(Path(evidence["cwd"])) != project_repo):
            counts["other_projects"] += 1
            continue
        source = (agent, evidence["source"]["sha256"])
        if source in seen:
            counts["duplicates"] += 1
            continue
        seen.add(source)
        identity = (agent, evidence["session_id"])
        if identity in identities:
            evidence["coverage"]["divergent_copy"] = True
            evidence["coverage"]["complete"] = False
            counts["divergent_copies"] += 1
        identities[identity] = source[1]
        key = store.put("sessions", evidence)
        sessions.append(key)
        copies.setdefault(identity, []).append(key)
        coverage = evidence["coverage"]
        counts["complete_sessions"] += bool(coverage["complete"])
        for name in ("calls", "matched_results", "unmatched_calls", "orphan_results",
                     "truncated_events", "malformed_records", "byte_cap_reached"):
            counts[name] += coverage.get(name, 0)
        counts["error_results"] += sum(e["kind"] == "result" and e["status"] == "error"
                                       for e in evidence["events"])
    ambiguous = sorted(key for group in copies.values() if len(group) > 1 for key in group)
    counts["complete_sessions"] = sum(store.get("sessions", key)["coverage"]["complete"]
                                     and key not in ambiguous for key in sessions)
    manifest = {"version": 3, "session_ids": sorted(sessions),
                "ambiguous_session_ids": ambiguous, "coverage": dict(counts),
                "project": str(project.resolve()) if project else None,
                "source_policy": {"days": days, "limit": limit, "max_bytes": max_bytes,
                                  "idle_s": idle_s, "agents": ["claude_code", "codex"]}}
    key = store.put("audits", manifest)
    store.append("audit", audit_id=key, sessions=len(sessions), coverage=dict(counts))
    return {"audit_id": key, "sessions": len(sessions), **counts}
