"""Preview measurable attempts in a private audit without creating cases."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from ..config import AutError
from ..event import parse_ts
from .judge import _record_attempt
from .store import Store, digest
from .study_observe import _sample_attempt, measurement_issue, validate_measurement

_SESSION_BUDGET = 32
_ATTEMPT_BUDGET = 1000


def ordinary_work_page(store: Store, scope: dict[str, Any], keys: list[str],
                       ambiguous: set[str], policy: dict[str, Any], agent: str, before: str,
                       include_excluded: bool, cursor: str, limit: int,
                       label: Callable[[str], str]) -> dict[str, Any]:
    validate_measurement(policy)
    cutoff = parse_ts(before) if before else None
    if before and (cutoff is None or cutoff.tzinfo is None):
        raise AutError("evidence: ordinary-work cutoff needs a timezone")
    metric = policy["metric"]
    binding = digest([scope["audit_id"], keys, scope["content"], policy, agent, before,
                      include_excluded])[:24]
    first, first_event = 0, 0
    if cursor:
        match = re.fullmatch(rf"work:([0-9]{{1,6}}):([0-9]{{1,8}}):{binding}", cursor)
        if match is None:
            raise AutError("evidence: ordinary-work cursor does not match this query and scope")
        first, first_event = int(match[1]), int(match[2])

    items: list[dict[str, Any]] = []
    sources: set[tuple[str, str]] = set()
    excluded: Counter[str] = Counter()
    scanned = measurable = 0

    def response(next_cursor: str | None = None) -> dict[str, Any]:
        return {"metric": metric, "items": items, "next_cursor": next_cursor,
                "coverage_basis": (metric if metric == "codex_model_calls"
                                   else policy.get("coverage_basis", "full_case")),
                "scanned_attempts": scanned, "measurable_attempts": measurable,
                "distinct_measurable_sessions": len(sources), "exclusions": dict(excluded),
                "scope": "Counts cover attempts scanned for this page. The preview precedes study pairing.",
                **scope}

    for position in range(first, len(keys)):
        if position >= first + _SESSION_BUDGET:
            return response(f"work:{position}:0:{binding}")
        snapshot = keys[position]
        session = store.get("sessions", snapshot)
        if agent and session["agent"] != agent:
            continue
        coverage = dict(session["coverage"])
        if snapshot in ambiguous:
            coverage.update(complete=False, divergent_copy=True)
        events = session["events"]
        start = 0
        for index, event in enumerate(events):
            if event["kind"] == "prompt":
                start = index
            if event["kind"] != "boundary":
                continue
            opened, start = start, index + 1
            if (position == first and index < first_event
                    or event.get("text") != "turn_complete"):
                continue
            if len(items) >= limit or scanned >= _ATTEMPT_BUDGET:
                return response(f"work:{position}:{index}:{binding}")
            scanned += 1
            attempt = events[opened:index + 1]
            case = _record_attempt(session, snapshot, coverage, attempt, len(attempt) - 1,
                                   "selected_attempt", False)
            sample = _sample_attempt(session, case, attempt)
            issue: str | None
            if coverage.get("divergent_copy"):
                issue = "divergent_copy"
            elif attempt[0]["kind"] != "prompt":
                issue = "opening_prompt_missing"
            else:
                issue = measurement_issue(sample, policy)
            if not issue and cutoff and sample["ended"] >= cutoff:
                issue = "after_cutoff"
            if issue:
                excluded[issue] += 1
                if not include_excluded:
                    continue
            else:
                measurable += 1
                sources.add(sample["identity"])
            item = {"session_id": snapshot, "event_id": event["id"], "agent": session["agent"],
                    "prompt_event_id": attempt[0]["id"] if attempt[0]["kind"] == "prompt" else None,
                    "ended": sample["ended"].isoformat() if sample["ended"] else None,
                    "context_status": sample["context_status"], "measurable": issue is None,
                    "reason": issue, "metric_value": sample[metric] if not issue else None}
            if scope["content"] != "metadata":
                item.update({field: label(sample[field]) for field in ("cwd", "model")
                             if isinstance(sample[field], str)})
            items.append(item)
    return response()
