"""Find observable retries in captured attempts without inferring intent."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Any

from .evidence import event_links, file_retry_keys
from .store import Store

FINGERPRINT = re.compile(r"[a-f0-9]{64}")
_KNOWN_OUTCOMES = {"success", "error", "ok", "completed"}
_AttemptRetry = tuple[int, int, int, int | None, str]


def _target(call: dict[str, Any], keys: dict[str, list[str]]) -> tuple[str, str] | None:
    if call.get("truncated") or not isinstance(text := call.get("text"), str):
        return None
    target, args = keys.get(call["id"], [call.get("retry_target_sha256"), call.get("retry_arguments_sha256")])
    if isinstance(target, str) and isinstance(args, str) and FINGERPRINT.fullmatch(
            target) and FINGERPRINT.fullmatch(args):
        return target, args
    return file_retry_keys(text)


def _attempt_retries(events: list[dict[str, Any]], start: int, end: int,
                     keys: dict[str, list[str]], *, changed_arguments: bool) -> Iterator[_AttemptRetry]:
    attempt = events[start:end]
    positions = {event["id"]: index for index, event in enumerate(attempt, start)}
    links = event_links(attempt)
    counts = Counter((e.get("run_id", ""), e.get("call_id"), e["kind"]) for e in attempt)
    exact: dict[tuple[str, str, str], int] = {}
    targets: dict[tuple[str, str, str], tuple[int, str]] = {}

    for index, call in enumerate(attempt, start):
        if call["kind"] != "call":
            continue
        run, tool = call.get("run_id", ""), call.get("tool")
        fingerprint = call.get("source_sha256")
        text = call.get("text", "")
        if not tool or not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
            continue
        exact_key = (run, tool, fingerprint)
        earlier = exact.pop(exact_key, -1)
        match = "identical"
        target = _target(call, keys) if changed_arguments else None
        target_key = (run, tool, target[0]) if target is not None else None
        previous = targets.pop(target_key, None) if target_key is not None else None
        identity = (run, call.get("call_id"))
        if (not identity[1] or counts[(*identity, "call")] != 1
                or counts[(*identity, "result")] > 1 or call.get("truncated")
                or call.get("tool_event_ambiguous")
                or not isinstance(text, str) or not text.strip()):
            continue
        exact[exact_key] = index
        if target is not None and target_key is not None:
            targets[target_key] = (index, target[1])
            if previous is not None and previous[0] > earlier:
                if previous[1] == target[1]:
                    continue
                earlier, match = previous[0], "changed_arguments"
        if earlier < 0:
            continue
        failure = positions.get(links.get(events[earlier]["id"], ""))
        if (failure is None or failure >= index or events[failure].get("status") != "error"
                or events[failure].get("tool_event_ambiguous")):
            continue
        result = positions.get(links.get(call["id"], ""))
        if result is not None and events[result].get("tool_event_ambiguous"):
            result = None
        yield earlier, failure, index, result, match


def retries(events: list[dict[str, Any]], keys: dict[str, list[str]] | None = None, *,
            changed_arguments: bool = True) -> Iterator[_AttemptRetry]:
    """Pair retries within one captured attempt and stop at evidence gaps."""
    keys = keys or {}
    start = 0
    for index, event in enumerate(events):
        if event["kind"] in {"prompt", "boundary", "tool_gap", "compaction"}:
            yield from _attempt_retries(events, start, index, keys, changed_arguments=changed_arguments)
            start = index + 1
    yield from _attempt_retries(events, start, len(events), keys, changed_arguments=changed_arguments)


def count_identical(events: list[dict[str, Any]]) -> int | None:
    for event in events:
        if event["kind"] != "call":
            continue
        fingerprint, text = event.get("source_sha256"), event.get("text")
        if (not event.get("tool") or event.get("truncated") or not isinstance(fingerprint, str)
                or not FINGERPRINT.fullmatch(fingerprint) or not isinstance(text, str) or not text.strip()):
            return None
    return sum(1 for _ in retries(events, changed_arguments=False))


def references(events: list[dict[str, Any]], match: _AttemptRetry) -> dict[str, Any]:
    first, error, retry, result, method = match
    return {"first_call": events[first]["id"], "error_result": events[error]["id"],
            "repeated_call": events[retry]["id"], "later_result": events[result]["id"]
            if result is not None else None, "match": method}


def _evidence_rank(evidence: dict[str, Any]) -> tuple[bool, bool, bool, str]:
    return (evidence["outcome"] in _KNOWN_OUTCOMES, evidence["later_result"] is not None,
            evidence["source_complete"], evidence["session_id"])


def discover(store: Store, snapshots: list[str], ambiguous: set[str], minimum_sessions: int,
             limit: int, tool_filter: str, label: Callable[[str], str] | None) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    sources: dict[tuple[str, str], set[tuple[str, str]]] = {}
    incidents: dict[tuple[tuple[str, str], str], tuple[str, dict[str, Any]]] = {}
    conflicted: set[tuple[tuple[str, str], str]] = set()
    skipped = 0
    for snapshot in snapshots:
        session = store.get("sessions", snapshot)
        source_id = session.get("session_id")
        if (snapshot in ambiguous or session["coverage"].get("divergent_copy")
                or not isinstance(source_id, str) or not source_id):
            skipped += 1
            continue
        agent = session["agent"]
        source = (agent, source_id)
        events = session["events"]
        for match in retries(events, session.get("retry_keys")):
            first, failure, _, result, _ = match
            tool = events[first]["tool"]
            incident = (source, events[failure]["id"])
            if incident in conflicted:
                continue
            outcome = "missing" if result is None else events[result].get("status", "unknown")
            if outcome != "missing" and outcome not in _KNOWN_OUTCOMES:
                outcome = "unknown"
            evidence = {"session_id": snapshot, "outcome": outcome,
                        "source_complete": bool(session["coverage"].get("complete")),
                        **references(events, match)}
            if incident in incidents:
                previous_tool, previous = incidents[incident]
                before, after = previous["outcome"], outcome
                if before in _KNOWN_OUTCOMES and after in _KNOWN_OUTCOMES and (
                        previous_tool != tool or (before == "error") != (after == "error")
                        or previous["repeated_call"] != evidence["repeated_call"]):
                    conflicted.add(incident)
                    del incidents[incident]
                    continue
                if _evidence_rank(previous) >= _evidence_rank(evidence):
                    continue
            incidents[incident] = (tool, evidence)

    for (source, _), (tool, evidence) in incidents.items():
        if tool_filter and label and label(tool) != tool_filter:
            continue
        agent, _ = source
        key = (agent, tool)
        group = groups.setdefault(key, {"agent": agent, "occurrences": 0, "matches": Counter(),
                                        "outcomes": Counter(), "examples": []})
        group["occurrences"] += 1
        group["matches"][evidence["match"]] += 1
        group["outcomes"][evidence["outcome"]] += 1
        distinct = sources.setdefault(key, set())
        if source not in distinct and len(group["examples"]) < 10:
            group["examples"].append(evidence)
        distinct.add(source)

    rows = []
    for key, group in groups.items():
        count = len(sources[key])
        if count >= minimum_sessions:
            rows.append({**group, "distinct_source_sessions": count,
                         **({"tool": label(key[1])} if label else {})})
    rows.sort(key=lambda row: (-row["distinct_source_sessions"], -row["occurrences"],
                               row["agent"], row.get("tool", "")))
    return {"groups": rows[:limit], "matching_groups": len(rows), "skipped_snapshots": skipped,
            "conflicting_incidents": len(conflicted)}
