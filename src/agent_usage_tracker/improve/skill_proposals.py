"""Propose a project skill from selected, corroborating session evidence."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import AutError
from .case_view import _attempt
from .outputs import commits_in
from .proposals import matches_file, prepare, save_change
from .store import Store, repository
from .study_independence import attempt_fingerprint, source_identities

SKILL_TARGET = re.compile(r"\.agents/skills/([a-z0-9]+(?:-[a-z0-9]+)*)/SKILL\.md")


def propose_skill(store: Store, audit_id: str, scope: Path, target: str,
                  draft: str, proofs: list[str]) -> str:
    match = SKILL_TARGET.fullmatch(target)
    if not match or len(match[1]) > 64:
        raise AutError("evidence: use .agents/skills/<skill-name>/SKILL.md as the target")
    header = re.match(r"\A---[ \t]*\r?\n(.+?)\r?\n---[ \t]*\r?\n(.*)\Z", draft, re.S)
    if (len(draft.encode("utf-8")) > 32_768 or header is None or not header[2].strip()
            or re.findall(r"^name:[ \t]*([^\r\n]+)\r?$", header[1], re.M) != [match[1]]
            or not re.search(r"^description:[ \t]*\S[^\n]*$", header[1], re.M)):
        raise AutError("evidence: the skill needs matching name, description and instructions")
    if not 2 <= len(proofs) <= 12:
        raise AutError("evidence: cite successful results from 2..12 independent sessions")
    scope = scope.resolve()
    project = repository(scope)
    if project is None or not (scope / ".git").exists():
        raise AutError("evidence: the skill scope must be a Git repository")
    target_path = scope / target
    if target_path.resolve() != target_path:
        raise AutError("evidence: the skill target cannot use symlinks")

    audit = store.get("audits", audit_id)
    allowed = set(audit["session_ids"]) - set(audit.get("ambiguous_session_ids", []))
    sources: set[tuple[str, str]] = set()
    attempts: set[str] = set()
    supporting: list[dict[str, Any]] = []
    for proof in proofs:
        case_id, separator, event_id = proof.partition(":")
        if not separator or not event_id:
            raise AutError("evidence: cite a result as CASE_ID:EVENT_ID")
        case = store.get("cases", case_id)
        session_id = case["session_snapshot"]
        if session_id not in allowed:
            raise AutError("evidence: skill evidence is outside the audit or ambiguous")
        session = store.get("sessions", session_id)
        cwd = session.get("cwd", "")
        if not cwd or not Path(cwd).is_absolute() or repository(Path(cwd)) != project:
            raise AutError("evidence: skill evidence must come from the target project")
        events = _attempt(session, case)
        observed = [event for event in events if event["id"] == event_id]
        call = observed[0].get("call_id") if observed else None
        if (not case["coverage"].get("complete") or case.get("omitted_task_events")
                or not observed or observed[0]["kind"] != "result"
                or observed[0].get("status") != "success" or not call
                or not any(event["kind"] == "call" and event.get("call_id") == call
                           for event in events[:events.index(observed[0])])):
            raise AutError("evidence: cite a matched successful result in a complete attempt")
        identities = source_identities(session)
        signature = attempt_fingerprint(events)
        if not session.get("session_id") or sources.intersection(identities) or signature in attempts:
            raise AutError("evidence: repeated or copied attempts cannot corroborate a skill")
        sources.update(identities)
        attempts.add(signature)
        supporting.append({"case_id": case_id, "session_id": session_id, "event_id": event_id,
                           "commit_ids": commits_in(case, session)})

    extra = {"supporting_cases": supporting, "evidence_audit_id": audit_id,
             "title": f"Review the {match[1]} project skill", "kind": "skill"}
    if matches_file(target_path, None):
        return save_change(store, supporting[0]["case_id"], scope, target_path, "", draft,
                           None, "", draft, extra=extra)
    try:
        before = target_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise AutError("evidence: the existing skill is not readable text") from exc
    return prepare(store, supporting[0]["case_id"], scope, target_path, before, draft, extra=extra)
