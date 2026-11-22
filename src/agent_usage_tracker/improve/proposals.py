"""Prepare one operator-scoped change without writing to its original target."""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from ..config import AutError
from ..state import atomic_write_text
from .store import Store


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


AUTHORITIES = ("operator_scoped_proposal", "model_drafted_proposal")


def prepare(store: Store, case_id: str, scope: Path, target: Path, before: str,
            after: str, *, authority: str = AUTHORITIES[0],
            extra: dict[str, Any] | None = None) -> str:
    if authority not in AUTHORITIES:
        raise AutError("improve: unknown proposal authority")
    case = store.get("cases", case_id)
    if not case.get("events"):
        raise AutError("improve: a proposal needs source evidence")
    scope, target = scope.resolve(), target.resolve()
    if not target.is_relative_to(scope) or not target.is_file():
        raise AutError("improve: proposal target is outside its allowed scope")
    try:
        original = target.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise AutError("improve: proposal target is not readable text") from exc
    if not before or original.count(before) != 1 or before == after:
        raise AutError("improve: replacement must match one unique nonempty span")
    if authority == "model_drafted_proposal" and len(after) - len(before) > 1200:
        raise AutError("improve: drafted replacement adds more than 1200 characters")
    candidate = original.replace(before, after, 1)
    return save_change(store, case_id, scope, target, original, candidate, file_digest(target),
                       before, after, authority=authority, extra=extra)


def matches_file(path: Path, expected: str | None) -> bool:
    if expected is not None:
        return path.is_file() and file_digest(path) == expected
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def save_change(store: Store, case_id: str, scope: Path, target: Path, original: str,
                candidate: str, before_digest: str | None, before: str, after: str,
                *, authority: str = AUTHORITIES[0], extra: dict[str, Any] | None = None) -> str:
    relative = target.relative_to(scope).as_posix()
    lines = difflib.unified_diff(
        original.splitlines(keepends=True), candidate.splitlines(keepends=True),
        fromfile=f"a/{relative}" if before_digest is not None else "/dev/null",
        tofile=f"b/{relative}",
    )
    patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                    for line in lines)
    value: dict[str, Any] = {"version": 1, "case_id": case_id, "scope": str(scope),
        "target": relative, "before_sha256": before_digest,
        "after_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "before": original, "after": candidate, "diff": patch,
        "span": {"before": before, "after": after}, "authority": authority, "state": "draft",
        "rollback": ("Restore the exact saved before content in an isolated candidate."
                     if before_digest is not None else "Remove the added file from the candidate.")}
    if extra:
        if extra.keys() & (value.keys() - {"rollback"}):
            raise AutError("improve: proposal extra fields cannot replace its core fields")
        value.update(extra)
    key = store.put("proposals", value)
    store.append("proposal_prepared", proposal_id=key, case_id=case_id)
    return key


def stage(store: Store, proposal_id: str, candidate_root: Path) -> Path:
    proposal = store.get("proposals", proposal_id)
    scope = Path(proposal["scope"]).resolve()
    relative = Path(proposal["target"])
    source = (scope / relative).resolve()
    root = candidate_root.resolve()
    target = (root / relative).resolve()
    if (root == scope or root in scope.parents or target == source
            or target != root / relative or not source.is_relative_to(scope)
            or not target.is_relative_to(root)):
        raise AutError("improve: stage only inside an independent candidate directory")
    expected = proposal["before_sha256"]
    if expected is None and (root.is_relative_to(scope) or source != scope / relative):
        raise AutError("improve: a new file needs an independent candidate and a direct path")
    if not matches_file(source, expected):
        raise AutError("improve: proposal base changed; prepare a new proposal")
    if not matches_file(target, None) and not (
            matches_file(target, expected) or matches_file(target, proposal["after_sha256"])):
        raise AutError("improve: candidate contains unrelated changes")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode: int | None
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    else:
        mode = stat.S_IMODE(source.stat().st_mode) if expected is not None else None
    atomic_write_text(target, proposal["after"], shared=mode is not None, newline="")
    if mode is not None:
        os.chmod(target, mode)
    store.append("proposal_staged", proposal_id=proposal_id, candidate=str(root))
    return target
