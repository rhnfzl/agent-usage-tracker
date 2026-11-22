"""Measure paired observations of ordinary work without executing a trial."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import Any

from ..config import AutError
from ..event import parse_ts
from .case_view import _attempt
from .evidence import event_links
from .file_reads import observed_file_read, path_fingerprint
from .judge import _attempt_coverage, _complete_attempt
from .setup_git import COMMIT_ID, target_in_workspace, verify_setup
from .store import Store, repository
from .study_independence import ReservedEvidence, attempt_fingerprint, reserved_evidence, source_identities
from .study_reviews import baseline_reviews, read_review
from .turn_revisions import details as turn_revision_details

METRICS = ("tool_errors", "tool_calls", "codex_model_calls")
COVERAGE_BASES = ("full_case", "tool_events")


def validate_measurement(policy: dict[str, Any]) -> None:
    if (policy.get("coverage_basis", "full_case") not in COVERAGE_BASES
            or type(policy.get("require_turn_context", False)) is not bool
            or policy.get("metric") not in METRICS
            or policy["metric"] == "codex_model_calls" and (
                not policy.get("require_turn_context") or "coverage_basis" in policy)):
        raise AutError("improve: invalid ordinary-work observation policy")


def validate_policy(policy: Any) -> None:
    if (not isinstance(policy, dict)
            or set(policy) - {"require_turn_context", "coverage_basis", "require_setup_revision",
                              "require_approved_file_read", "require_worktree_setup", "baseline_reviews"}
            != {"metric", "minimum_pairs", "minimum_improvement", "baseline_cases"}
            or type(policy.get("require_setup_revision", False)) is not bool
            or policy.get("require_setup_revision") and not policy.get("require_turn_context")
            or type(policy.get("require_approved_file_read", False)) is not bool
            or policy.get("require_approved_file_read") and not policy.get("require_turn_context")
            or type(policy.get("require_worktree_setup", False)) is not bool
            or policy.get("require_worktree_setup") and not policy.get("require_turn_context")
            or type(policy["minimum_pairs"]) is not int or not 2 <= policy["minimum_pairs"] <= 200
            or type(policy["minimum_improvement"]) not in (float, int)
            or not 0 <= policy["minimum_improvement"] < math.inf):
        raise AutError("improve: invalid ordinary-work observation policy")
    validate_measurement(policy)

    baseline = policy["baseline_cases"]
    if (not isinstance(baseline, dict) or not policy["minimum_pairs"] <= len(baseline) <= 200
            or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                   or not isinstance(family, str) or not family.strip() or len(family) > 200
                   for key, family in baseline.items())):
        raise AutError("improve: freeze distinct baseline case IDs and their task families")
    if "baseline_reviews" in policy:
        reviews = policy["baseline_reviews"]
        if (not isinstance(reviews, dict) or set(reviews) != set(baseline)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in reviews.values())):
            raise AutError("improve: freeze one acceptance review ID for each baseline case")


def _sample(store: Store, case_id: str) -> dict[str, Any]:
    case = store.get("cases", case_id)
    session = store.get("sessions", case["session_snapshot"])
    return _sample_attempt(session, case, _attempt(session, case))


def _sample_attempt(session: dict[str, Any], case: dict[str, Any],
                    events: list[dict[str, Any]]) -> dict[str, Any]:
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
    turn = None
    if context_status == "observed":
        turn = events[-1].get("native_turn_key")
        if not turn or any(event.get("native_turn_key") != turn for event in events):
            context_status = "ambiguous"
    receipt = events[-1].get("codex_model_call_receipt")
    if not isinstance(receipt, dict):
        receipt = {}
    model_calls = receipt.get("count")
    model_started = parse_ts(receipt.get("started_at"))
    if (not has_timed_source or session["agent"] != "codex"
            or session.get("origin") != "interactive" or context_status != "observed"
            or receipt.get("thread_origin") != "interactive"
            or events[0]["kind"] != "prompt" or events[-1].get("text") != "turn_complete"
            or type(model_calls) is not int or model_calls < 1
            or not model_started or not model_started.tzinfo or model_started > min(times)
            or any(coverage.get(gap) for gap in ("malformed_records", "missing_timestamps",
                       "divergent_copy", "source_incomplete", "unknown_copy_boundary"))):
        model_calls, model_started = None, None
    setup_revision = None
    setup_files: dict[str, str | None] = {}
    if context and context_status == "observed":
        observed = turn_revision_details(context.get("turn_start_revision"))
        setup_revision = observed["commit"] if observed else context.get("session_start_revision")
        if observed is not None:
            setup_files = observed.get("setup_files", {})
    return {
        "context_status": context_status,
        "identity": (session["agent"], session.get("session_id")),
        "source_identities": source_identities(session),
        "content": attempt_fingerprint(events),
        "cwd": context.get("cwd") if context else session.get("cwd"),
        "model": context.get("model") if context else session.get("model"),
        "setup_revision": setup_revision,
        "setup_files": setup_files,
        "read_origin": _read_origin(session, case["session_snapshot"], events[-1], context, turn)
        if context and context_status == "observed" and turn else None,
        "started": min(times) if times else None, "ended": max(times) if times else None,
        "complete": completed, "tool_events_complete": tool_events_complete, "tool_calls": calls,
        "codex_model_calls": model_calls,
        "codex_model_started": model_started.astimezone(timezone.utc) if model_started else None,
        "tool_representation": next(iter(representations)) if len(representations) == 1 else None,
        "tool_errors": sum(event.get("status") == "error" for event in results)
                       if known_errors else None,
    }


def _read_origin(session: dict[str, Any], snapshot: str, end: dict[str, Any],
                 context: dict[str, Any], turn: str) -> tuple[str, str, str] | None:
    if session["agent"] == "codex":
        return snapshot, end["id"], turn
    count = context.get("matching_items")
    if (session["agent"] == "t3" and context.get("source_session") in session.get("supplemental_sources", ())
            and isinstance(context.get("source_event"), str) and type(count) is int and count >= 2):
        return context["source_session"], context["source_event"], turn
    return None


def _in_scope(cwd: str | None, scope: Path, project: str | None) -> bool:
    if not cwd or not Path(cwd).is_absolute():
        return False
    path = Path(cwd).resolve()
    return path == scope or (project is not None and repository(path) == project)


def _sufficient_coverage(sample: dict[str, Any], policy: dict[str, Any]) -> bool:
    if policy["metric"] == "codex_model_calls":
        return sample["codex_model_calls"] is not None
    if policy.get("coverage_basis") == "tool_events":
        return bool(sample["tool_events_complete"])
    return bool(sample["complete"])


def measurement_issue(sample: dict[str, Any], policy: dict[str, Any]) -> str | None:
    context = sample["context_status"]
    if context in {"missing", "ambiguous"} or policy.get("require_turn_context") and context != "observed":
        return "turn_context_unverified"
    if not _sufficient_coverage(sample, policy):
        return "measurement_coverage_incomplete"
    if sample[policy["metric"]] is None:
        return "metric_unknown"
    if (not isinstance(sample["model"], str) or not sample["model"]
            or not isinstance(sample["cwd"], str) or not Path(sample["cwd"]).is_absolute()):
        return "setup_labels_unverified"
    return None


def _is_revision(value: Any) -> bool:
    return isinstance(value, str) and bool(COMMIT_ID.fullmatch(value))


def _matches_worktree(sample: dict[str, Any], target: Path | None, expected: str | None) -> bool:
    if target is None:
        return False
    key = path_fingerprint(target)
    files = sample["setup_files"]
    return key in files and files[key] == expected


def baseline_gate(policy: dict[str, Any], proposal: dict[str, Any], reserved: ReservedEvidence,
                  before: datetime | None = None) -> Callable[[dict[str, Any]], str | None]:
    scope = Path(proposal["scope"]).resolve()
    project = repository(scope)

    @lru_cache(maxsize=200)
    def in_scope(cwd: str) -> bool:
        return _in_scope(cwd, scope, project)

    @lru_cache(maxsize=200)
    def matches_setup(revision: str) -> bool:
        return verify_setup(proposal, revision, "before")["status"] == "verified"

    @lru_cache(maxsize=200)
    def read_target(cwd: str) -> Path | None:
        return target_in_workspace(proposal, cwd)

    def check(sample: dict[str, Any]) -> str | None:
        if sample["source_identities"] & reserved.sources or sample["content"] in reserved.fingerprints:
            return "reserved_study_evidence"
        if issue := measurement_issue(sample, policy):
            return issue
        if before and sample["ended"] >= before:
            return "after_cutoff"
        if not in_scope(sample["cwd"]):
            return "outside_proposal_project"
        revision = sample["setup_revision"]
        if policy.get("require_setup_revision") and (not _is_revision(revision) or not matches_setup(revision)):
            return "setup_revision_unverified"
        if (policy.get("require_worktree_setup")
                and not _matches_worktree(sample, read_target(sample["cwd"]), proposal["before_sha256"])):
            return "worktree_setup_unverified"
        return None

    return check


def check_baselines(store: Store, policy: dict[str, Any], proposal: dict[str, Any],
                    before: datetime, reserved: ReservedEvidence) -> None:
    baseline_reviews(store, policy, before)
    check = baseline_gate(policy, proposal, reserved, before)
    sessions: set[tuple[str, str]] = set()
    contents: set[str] = set()
    for key in policy["baseline_cases"]:
        sample = _sample(store, key)
        if (check(sample) or key in reserved.case_ids
                or sample["source_identities"] & sessions or sample["content"] in contents):
            continue
        sessions.update(sample["source_identities"])
        contents.add(sample["content"])
    if len(contents) < policy["minimum_pairs"]:
        needed = " with recorded turn context" if policy.get("require_turn_context") else ""
        if policy.get("require_setup_revision"):
            needed += " and verified setup revisions"
        if policy.get("require_worktree_setup"):
            needed += " and verified worktree setup"
        raise AutError(f"improve: not enough eligible baseline cases{needed}")


def observe(store: Store, study_id: str, spec: dict[str, Any], *,
            preview: bool = False) -> dict[str, Any]:
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
    frozen_reviews = baseline_reviews(store, policy, frozen)
    pairs = spec["pairs"]
    if not isinstance(pairs, list) or len(pairs) > 200:
        raise AutError("improve: supply up to 200 matched pairs")
    proposal = store.get("proposals", study["proposal_id"])
    require_setup = policy.get("require_setup_revision", False)
    require_read = policy.get("require_approved_file_read", False)
    require_worktree = policy.get("require_worktree_setup", False)

    @lru_cache(maxsize=400)
    def read_setup(revision: str, condition: str) -> dict[str, str]:
        return verify_setup(proposal, revision, condition)

    @lru_cache(maxsize=200)
    def read_target(cwd: str) -> Path | None:
        return target_in_workspace(proposal, cwd)

    adoption_setup = None
    if require_setup:
        saved = adoption.get("commit_setup") or {}
        adopted_revision = saved.get("revision") or adoption.get("commit", "")
        adoption_setup = read_setup(adopted_revision, "after")
    scope = Path(proposal["scope"]).resolve()
    project = repository(scope)
    known = set(study["known_case_ids"])
    reserved = reserved_evidence(store, proposal, study["discovery_cases"], study["control_cases"])
    paired_baselines: set[str] = set()
    seen_cases: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    seen_content: set[str] = set()
    counts: Counter[str] = Counter()
    acceptance: Counter[str] = Counter()
    outcomes = []
    metric = policy["metric"]

    for pair in pairs:
        has_review = bool(frozen_reviews and isinstance(pair, dict) and "candidate_review_id" in pair)
        fields(pair, "baseline candidate family" + (" candidate_review_id" if has_review else ""))
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
        review_fields = {}
        review_issue = None
        if frozen_reviews:
            result = "missing"
            if has_review:
                review = read_review(store, pair["candidate_review_id"], right, family)
                if review["criterion"] != frozen_reviews[left]["criterion"]:
                    raise AutError("improve: candidate review must use the frozen acceptance criterion")
                if datetime.fromisoformat(review["reviewed_at"]) <= frozen:
                    raise AutError("improve: candidate review must follow the frozen policy")
                result = review["result"]
            acceptance[result] += 1
            review_fields = {"baseline_review_id": policy["baseline_reviews"][left],
                             "candidate_acceptance": result}
            review_issue = {"missing": "task_review_missing", "unknown": "task_acceptance_unknown",
                            "rejected": "task_not_accepted"}.get(result)
        sources = before["source_identities"] | after["source_identities"]
        content = {before["content"], after["content"]}
        contexts = {before["context_status"], after["context_status"]}
        after_started = after["codex_model_started"] if metric == "codex_model_calls" else after["started"]
        reason = None
        if (reserved.case_ids & {left, right} or sources & reserved.sources
                or before["source_identities"] & after["source_identities"]
                or len(content) != 2 or seen_sources & sources
                or (seen_content | reserved.fingerprints) & content):
            reason = "reused_or_copied_evidence"
        elif (left not in known or right in known or not _sufficient_coverage(before, policy)
              or not _sufficient_coverage(after, policy) or before["ended"] >= frozen
              or after_started <= frozen or after_started.date() <= adopted):
            reason = "missing_coverage_or_wrong_window"
        elif (contexts & {"missing", "ambiguous"}
              or policy.get("require_turn_context") and contexts != {"observed"}):
            reason = "turn_context_unverified"
        elif (not _in_scope(before["cwd"], scope, project)
              or not _in_scope(after["cwd"], scope, project)
              or before["identity"][0] != after["identity"][0]
              or not before["model"] or before["model"] != after["model"]):
            reason = "project_harness_or_model_labels_differ"
        elif (metric != "codex_model_calls" and before["tool_representation"] and after["tool_representation"]
              and before["tool_representation"] != after["tool_representation"]):
            reason = "tool_representations_differ"
        elif require_setup and adoption_setup and adoption_setup["status"] != "verified":
            reason = "adoption_commit_unverified"
        elif require_setup and not all(_is_revision(sample["setup_revision"])
                and read_setup(sample["setup_revision"], condition)["status"] == "verified"
                for sample, condition in ((before, "before"), (after, "after"))):
            reason = "setup_revision_unverified"
        elif require_worktree and not all(_matches_worktree(sample, read_target(sample["cwd"]),
                proposal[f"{condition}_sha256"]) for sample, condition in ((before, "before"), (after, "after"))):
            reason = "worktree_setup_unverified"
        elif before[metric] is None or after[metric] is None:
            reason = "metric_unknown"
        elif review_issue:
            reason = review_issue
        approved_read = None
        if require_read and reason is None:
            source = after["read_origin"]
            target = read_target(after["cwd"])
            if source and target:
                snapshot, end, turn = source
                match = observed_file_read(store.get("sessions", snapshot), end, turn, target,
                                           proposal["after_sha256"])
                if match:
                    approved_read = {"session_snapshot": snapshot, **match}
            if approved_read is None:
                reason = "approved_file_read_unverified"
        seen_sources.update(sources)
        seen_content.update(content)
        if reason:
            counts[reason] += 1
            outcomes.append({**pair, **review_fields, "eligible": False, "reason": reason})
            continue
        outcomes.append({**pair, **review_fields, "eligible": True, "baseline_value": before[metric],
                         "candidate_value": after[metric],
                         "reduction": before[metric] - after[metric],
                         "uses_tool_events_only": policy.get("coverage_basis") == "tool_events"
                         and not (before["complete"] and after["complete"]),
                         "setup_revision_verified": require_setup,
                         **({"worktree_setup_verified": True} if require_worktree else {}),
                         **({"approved_file_read": approved_read} if approved_read else {}),
                         "basis": "recorded_turn_context" if contexts == {"observed"}
                         else "includes_session_labels"})

    reductions = [row["reduction"] for row in outcomes if row["eligible"]]
    reduction = mean(reductions) if reductions else None
    comparison = "inconclusive"
    unpaired = sorted(set(baseline) - paired_baselines)
    all_accepted = bool(frozen_reviews and not unpaired and acceptance["accepted"] == len(baseline))
    if (not unpaired and (not frozen_reviews or all_accepted) and len(reductions) >= policy["minimum_pairs"]
            and reduction is not None):
        if reduction > policy["minimum_improvement"]:
            comparison = "observed_decrease"
        elif reduction < -policy["minimum_improvement"]:
            comparison = "observed_increase"
    record = {"study_id": study_id, "adoption_id": adoption_id, "metric": metric,
              "comparison": comparison, "eligible_pairs": len(reductions),
              "coverage_basis": ("codex_model_calls" if metric == "codex_model_calls"
                                 else policy.get("coverage_basis", "full_case")),
              "tool_events_only_pairs": sum(row.get("uses_tool_events_only", False)
                                           for row in outcomes),
              "excluded_pairs": len(pairs) - len(reductions), "exclusions": dict(counts),
              "recorded_turn_context_pairs": sum(row.get("basis") == "recorded_turn_context"
                                                   for row in outcomes),
              "setup_revision_verified_pairs": sum(row.get("setup_revision_verified", False)
                                                      for row in outcomes),
              **({"worktree_setup_verified_pairs": len(reductions)} if require_worktree else {}),
              "approved_file_read_pairs": sum("approved_file_read" in row for row in outcomes),
              **({"adoption_commit_setup": adoption_setup} if require_setup else {}),
              **({"task_acceptance": {"basis": "operator_review", "all_cases_accepted": all_accepted,
                                     "candidate_results": dict(acceptance)}} if frozen_reviews else {}),
              "unpaired_baseline_ids": unpaired, "mean_reduction": reduction, "pairs": outcomes,
              "causal_effect_established": False, "task_quality_verified": False,
              "attempt_setup_exposure_verified": False,
              "scope": "Operator-paired ordinary work with matching recorded context labels."}
    if preview:
        return {**record, "preview": True}
    key = store.put("study_observations", record)
    return {"observation_id": key, **record}
