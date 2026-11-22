"""Measure paired observations of ordinary work without executing a trial."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from ..config import AutError
from ..event import parse_ts
from .case_view import _attempt
from .evidence import event_links
from .judge import _attempt_coverage, _complete_attempt
from .store import Store, digest, repository


def validate_policy(policy: Any) -> None:
    if (not isinstance(policy, dict)
            or set(policy) - {"require_turn_context", "coverage_basis"}
            != {"metric", "minimum_pairs", "minimum_improvement", "baseline_cases"}
            or policy.get("coverage_basis", "full_case") not in ("full_case", "tool_events")
            or type(policy.get("require_turn_context", False)) is not bool
            or policy["metric"] not in ("tool_errors", "tool_calls")
            or type(policy["minimum_pairs"]) is not int or not 2 <= policy["minimum_pairs"] <= 200
            or type(policy["minimum_improvement"]) not in (float, int)
            or not 0 <= policy["minimum_improvement"] < math.inf):
        raise AutError("improve: invalid ordinary-work observation policy")

    baseline = policy["baseline_cases"]
    if (not isinstance(baseline, dict) or not policy["minimum_pairs"] <= len(baseline) <= 200
            or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                   or not isinstance(family, str) or not family.strip() or len(family) > 200
                   for key, family in baseline.items())):
        raise AutError("improve: freeze distinct baseline case IDs and their task families")


def _sample(store: Store, case_id: str) -> dict[str, Any]:
    case = store.get("cases", case_id)
    session = store.get("sessions", case["session_snapshot"])
    events = _attempt(session, case)
    stamps = [parse_ts(event.get("timestamp")) for event in events]
    times = [stamp.astimezone(timezone.utc) for stamp in stamps if stamp and stamp.tzinfo]
    calls = sum(event["kind"] == "call" for event in events)
    results = [event for event in events if event["kind"] == "result"]
    known_errors = all(event.get("status") in ("success", "error") for event in results)
    representations = {event["tool_representation"] for event in events
                       if event.get("tool_representation")}
    has_timed_source = bool(len(times) == len(events) and session.get("session_id")
                            and session.get("origin") not in {"automated", "subagent"})
    checked = session.get("coverage", {}).get("tool_events_checked") is True
    # A sampled case can omit events whose order and identities remain intact in its snapshot.
    coverage = _attempt_coverage(events, case["coverage"])
    tool_events_complete = (has_timed_source
                            and checked and len(representations) <= 1
                            and len({event.get("run_id") for event in events}) == 1
                            and calls == len(results) == len(event_links(events))
                            and _complete_attempt(events, coverage, require_text=False))
    completed = bool(has_timed_source and case["coverage"].get("complete")
                     and not case.get("omitted_task_events")
                     and (not checked or tool_events_complete))
    context = events[-1].get("turn_context") if events[-1]["kind"] == "boundary" else None
    context_status = context["status"] if context else "legacy_session_labels"
    if context_status == "observed":
        turn = events[-1].get("native_turn_key")
        if not turn or any(event.get("native_turn_key") != turn for event in events):
            context_status = "ambiguous"
    return {
        "context_status": context_status,
        "identity": (session["agent"], session.get("session_id")),
        "content": digest([{key: event.get(key) for key in ("kind", "status", "text", "tool")}
                           for event in events]),
        "cwd": context.get("cwd") if context else session.get("cwd"),
        "model": context.get("model") if context else session.get("model"),
        "started": min(times) if times else None, "ended": max(times) if times else None,
        "complete": completed, "tool_events_complete": tool_events_complete, "tool_calls": calls,
        "tool_representation": next(iter(representations)) if len(representations) == 1 else None,
        "tool_errors": sum(event.get("status") == "error" for event in results)
                       if known_errors else None,
    }


def _in_scope(cwd: str | None, scope: Path, project: str | None) -> bool:
    if not cwd or not Path(cwd).is_absolute():
        return False
    path = Path(cwd).resolve()
    return path == scope or (project is not None and repository(path) == project)


def _sufficient_coverage(sample: dict[str, Any], policy: dict[str, Any]) -> bool:
    if policy.get("coverage_basis") == "tool_events":
        return bool(sample["tool_events_complete"])
    return bool(sample["complete"])


def check_strict_baselines(store: Store, policy: dict[str, Any], scope: Path, before: datetime,
                           excluded_sessions: set[str]) -> None:
    scope = scope.resolve()
    project = repository(scope)
    sessions: set[str] = set()
    contents: set[str] = set()
    for key in policy["baseline_cases"]:
        sample = _sample(store, key)
        source = digest(list(sample["identity"]))
        if (sample["context_status"] != "observed" or not _sufficient_coverage(sample, policy)
                or sample["ended"] >= before or sample[policy["metric"]] is None
                or source in excluded_sessions or source in sessions
                or sample["content"] in contents or not _in_scope(sample["cwd"], scope, project)):
            continue
        sessions.add(source)
        contents.add(sample["content"])
    if len(sessions) < policy["minimum_pairs"]:
        raise AutError("improve: not enough eligible baseline cases with recorded turn context")


def observe(store: Store, study_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    study = store.get("studies", study_id)
    policy = study.get("observation_policy")
    if policy is None:
        raise AutError("improve: the study has no frozen ordinary-work observation policy")
    validate_policy(policy)
    from .studies import fields

    fields(spec, "adoption_id pairs")
    adoption_id = spec["adoption_id"]
    if not isinstance(adoption_id, str):
        raise AutError("improve: supply a valid adoption ID")
    adoption = store.get("adoptions", adoption_id)
    if adoption.get("proposal_id") != study["proposal_id"]:
        raise AutError("improve: adoption is for a different proposal")
    adopted = date.fromisoformat(adoption["adopted_at"])
    frozen = datetime.fromisoformat(study["created_at"])
    if adopted <= frozen.astimezone(timezone.utc).date():
        raise AutError("improve: the recorded adoption day must follow the policy day")
    baseline = policy["baseline_cases"]
    pairs = spec["pairs"]
    if not isinstance(pairs, list) or len(pairs) > 200:
        raise AutError("improve: supply up to 200 matched pairs")
    proposal = store.get("proposals", study["proposal_id"])
    scope = Path(proposal["scope"]).resolve()
    project = repository(scope)
    known = set(study["known_case_ids"])
    discovery = set(study["discovery_cases"]) | set(study["control_cases"])
    excluded_sources = set()
    for key in discovery:
        case = store.get("cases", key)
        session = store.get("sessions", case["session_snapshot"])
        excluded_sources.add((session["agent"], session.get("session_id")))
    paired_baselines: set[str] = set()
    seen_cases: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    seen_content: set[str] = set()
    counts: Counter[str] = Counter()
    outcomes = []
    metric = policy["metric"]

    for pair in pairs:
        fields(pair, "baseline candidate family")
        left, right, family = (pair[key] for key in ("baseline", "candidate", "family"))
        if not all(isinstance(value, str) and value.strip() for value in (left, right, family)):
            raise AutError("improve: each pair needs two case IDs and a task family")
        if baseline.get(left) != family:
            raise AutError("improve: compare a frozen baseline case with its registered task family")
        paired_baselines.add(left)
        if left == right or seen_cases & {left, right}:
            raise AutError("improve: a case can appear only once in a comparison")
        seen_cases.update((left, right))
        before, after = _sample(store, left), _sample(store, right)
        sources = {before["identity"], after["identity"]}
        content = {before["content"], after["content"]}
        contexts = {before["context_status"], after["context_status"]}
        reason = None
        if (left in discovery or right in discovery or sources & excluded_sources
                or len(sources) != 2 or len(content) != 2 or seen_sources & sources
                or seen_content & content):
            reason = "reused_or_copied_evidence"
        elif (left not in known or right in known or not _sufficient_coverage(before, policy)
              or not _sufficient_coverage(after, policy) or before["ended"] >= frozen or after["started"] <= frozen
              or after["started"].date() <= adopted):
            reason = "missing_coverage_or_wrong_window"
        elif (contexts & {"missing", "ambiguous"}
              or policy.get("require_turn_context") and contexts != {"observed"}):
            reason = "turn_context_unverified"
        elif (not _in_scope(before["cwd"], scope, project)
              or not _in_scope(after["cwd"], scope, project)
              or before["identity"][0] != after["identity"][0]
              or not before["model"] or before["model"] != after["model"]):
            reason = "project_harness_or_model_labels_differ"
        elif (before["tool_representation"] and after["tool_representation"]
              and before["tool_representation"] != after["tool_representation"]):
            reason = "tool_representations_differ"
        elif before[metric] is None or after[metric] is None:
            reason = "metric_unknown"
        seen_sources.update(sources)
        seen_content.update(content)
        if reason:
            counts[reason] += 1
            outcomes.append({**pair, "eligible": False, "reason": reason})
            continue
        outcomes.append({**pair, "eligible": True, "baseline_value": before[metric],
                         "candidate_value": after[metric],
                         "reduction": before[metric] - after[metric],
                         "uses_tool_events_only": not (before["complete"] and after["complete"]),
                         "basis": "recorded_turn_context" if contexts == {"observed"}
                         else "includes_session_labels"})

    reductions = [row["reduction"] for row in outcomes if row["eligible"]]
    reduction = mean(reductions) if reductions else None
    comparison = "inconclusive"
    unpaired = sorted(set(baseline) - paired_baselines)
    if not unpaired and len(reductions) >= policy["minimum_pairs"] and reduction is not None:
        if reduction > policy["minimum_improvement"]:
            comparison = "observed_decrease"
        elif reduction < -policy["minimum_improvement"]:
            comparison = "observed_increase"
    record = {"study_id": study_id, "adoption_id": adoption_id, "metric": metric,
              "comparison": comparison, "eligible_pairs": len(reductions),
              "coverage_basis": policy.get("coverage_basis", "full_case"),
              "tool_events_only_pairs": sum(row.get("uses_tool_events_only", False)
                                           for row in outcomes),
              "excluded_pairs": len(pairs) - len(reductions), "exclusions": dict(counts),
              "recorded_turn_context_pairs": sum(row.get("basis") == "recorded_turn_context"
                                                   for row in outcomes),
              "unpaired_baseline_ids": unpaired, "mean_reduction": reduction, "pairs": outcomes,
              "causal_effect_established": False, "task_quality_verified": False,
              "attempt_setup_exposure_verified": False,
              "scope": "Operator-paired ordinary work with matching recorded context labels."}
    key = store.put("study_observations", record)
    return {"observation_id": key, **record}
