"""Track the evidence reserved for discovery, controls, and proposal support."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import AutError
from .case_view import _attempt
from .store import Store, digest

Source = tuple[str, str]


def source_identities(session: dict[str, Any]) -> frozenset[Source]:
    identities = {(session["agent"], session["session_id"])} if session.get("session_id") else set()
    if session["agent"] == "t3":
        for reference in session.get("native_sessions", ()):
            if (isinstance(reference, (tuple, list)) and len(reference) == 2
                    and reference[0] in ("codex", "claude_code")
                    and isinstance(reference[1], str) and reference[1]):
                identities.add((reference[0], reference[1]))
    return frozenset(identities)


def attempt_fingerprint(events: list[dict[str, Any]]) -> str:
    return digest([{key: event.get(key) for key in ("kind", "status", "text", "tool")}
                   for event in events])


@dataclass(frozen=True)
class ReservedEvidence:
    case_ids: frozenset[str]
    sources: frozenset[Source]
    fingerprints: frozenset[str]


def reserved_evidence(store: Store, proposal: dict[str, Any],
                      discovery: list[str], controls: list[str]) -> ReservedEvidence:
    cases = set(discovery) | set(controls)
    for proof in proposal.get("supporting_cases", ()):
        if not isinstance(proof, dict) or not isinstance(proof.get("case_id"), str):
            raise AutError("improve: proposal support has no case reference")
        cases.add(proof["case_id"])
    sources: set[Source] = set()
    fingerprints: set[str] = set()
    for key in cases:
        case = store.get("cases", key)
        session = store.get("sessions", case["session_snapshot"])
        sources.update(source_identities(session))
        try:
            events = _attempt(session, case)
        except AutError:
            # Older case formats may retain the review excerpt alone.
            events = case["events"]
        if events:
            fingerprints.add(attempt_fingerprint(events))
    return ReservedEvidence(frozenset(cases), frozenset(sources), frozenset(fingerprints))
