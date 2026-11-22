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
from .store import Store, digest, repository


def validate_policy(policy: Any) -> None:
    if (not isinstance(policy, dict)
            or set(policy) != {"metric", "minimum_pairs", "minimum_improvement", "baseline_cases"}
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
    results = [event for event in events if event["kind"] == "result"]
    known_errors = all(event.get("status") in ("success", "error") for event in results)
    completed = bool(case["coverage"].get("complete") and not case.get("omitted_task_events")
                     and len(times) == len(events) and session.get("session_id")
                     and session.get("origin") not in {"automated", "subagent"})
    return {
        "identity": (session["agent"], session.get("session_id")),
        "content": digest([{key: event.get(key) for key in ("kind", "status", "text", "tool")}
                           for event in events]),
        "cwd": session.get("cwd"), "model": session.get("model"),
        "started": min(times) if times else None, "ended": max(times) if times else None,
        "complete": completed, "tool_calls": sum(event["kind"] == "call" for event in events),
        "tool_errors": sum(event.get("status") == "error" for event in results)
                       if known_errors else None,
    }


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
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= 200:
        raise AutError("improve: supply 1..200 matched pairs")
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

    def in_scope(cwd: str | None) -> bool:
        if not cwd or not Path(cwd).is_absolute():
            return False
        path = Path(cwd).resolve()
        return path == scope or (project is not None and repository(path) == project)

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
        reason = None
        if (left in discovery or right in discovery or sources & excluded_sources
                or len(sources) != 2 or len(content) != 2 or seen_sources & sources
                or seen_content & content):
            reason = "reused_or_copied_evidence"
        elif (left not in known or right in known or not before["complete"] or not after["complete"]
              or before["ended"] >= frozen or after["started"] <= frozen
              or after["started"].date() <= adopted):
            reason = "missing_coverage_or_wrong_window"
        elif (not in_scope(before["cwd"]) or not in_scope(after["cwd"])
              or before["identity"][0] != after["identity"][0]
              or not before["model"] or before["model"] != after["model"]):
            reason = "session_project_harness_or_model_labels_differ"
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
                         "reduction": before[metric] - after[metric]})

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
              "excluded_pairs": len(pairs) - len(reductions), "exclusions": dict(counts),
              "unpaired_baseline_ids": unpaired, "mean_reduction": reduction, "pairs": outcomes,
              "causal_effect_established": False, "task_quality_verified": False,
              "attempt_setup_exposure_verified": False,
              "scope": "Operator-paired ordinary work with matching recorded session labels."}
    key = store.put("study_observations", record)
    return {"observation_id": key, **record}
