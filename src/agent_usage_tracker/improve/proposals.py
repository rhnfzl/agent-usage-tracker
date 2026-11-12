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


def prepare(store: Store, case_id: str, scope: Path, target: Path, before: str,
            after: str) -> str:
    case = store.get("cases", case_id)
    if not case.get("events"):
        raise AutError("improve: a proposal needs source evidence")
    scope, target = scope.resolve(), target.resolve()
    if not target.is_relative_to(scope) or not target.is_file():
        raise AutError("improve: proposal target is outside its allowed scope")
    try:
        original = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AutError("improve: proposal target is not readable text") from exc
    if not before or original.count(before) != 1 or before == after:
        raise AutError("improve: replacement must match one unique nonempty span")
    candidate = original.replace(before, after, 1)
    relative = target.relative_to(scope).as_posix()
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True),
                                       candidate.splitlines(keepends=True),
                                       fromfile=f"a/{relative}", tofile=f"b/{relative}"))
    value: dict[str, Any] = {"version": 1, "case_id": case_id, "scope": str(scope),
        "target": relative, "before_sha256": file_digest(target),
        "after_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "before": original, "after": candidate, "diff": patch,
        "authority": "operator_scoped_proposal", "state": "draft",
        "rollback": "Restore the exact saved before content in an isolated candidate."}
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
    if not source.is_file() or file_digest(source) != proposal["before_sha256"]:
        raise AutError("improve: proposal base changed; prepare a new proposal")
    if target.exists() and file_digest(target) not in {
            proposal["before_sha256"], proposal["after_sha256"]}:
        raise AutError("improve: candidate contains unrelated changes")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode if target.exists() else source.stat().st_mode)
    atomic_write_text(target, proposal["after"], shared=True)
    os.chmod(target, mode)
    store.append("proposal_staged", proposal_id=proposal_id, candidate=str(root))
    return target
