"""Apply a frozen descriptive decision rule to paired stored observations."""

from __future__ import annotations

import math
from typing import Any, cast

from pathlib import Path

from .store import Store, digest


def experiment_identity(store: Store, spec_id: str) -> str:
    """Identify executed inputs; ignored notes and case names cannot create fresh work."""
    spec = store.get("trial_specs", spec_id)
    cases = [digest({name: case[name] for name in (
        "argv", "timeout_s", "acceptance_argv", "acceptance_files") if name in case})
        for case in spec["cases"]]
    return digest({"kind": spec["kind"],
        "directories": {name: str(Path(root).resolve())
                        for name, root in spec["directories"].items()},
        "files": spec.get("files", {}), "cases": sorted(set(cases))})


def observed_experiment(store: Store, spec_id: str, *, allow_resume: bool = False) -> bool:
    identity = experiment_identity(store, spec_id)
    return any(experiment_identity(store, source) == identity
               for key in store.keys("trial_results")
               if (source := store.get("trial_results", key)["spec_id"]) != spec_id
               or not allow_resume)


def measure(store: Store, spec_id: str, metric: str, policy: dict[str, Any],
            proposal: dict[str, Any]) -> dict[str, Any]:
    spec = store.get("trial_specs", spec_id)
    expected = {(repeat, case["id"], condition) for repeat in range(spec["repeats"])
                for case in spec["cases"] for condition in ("baseline", "candidate")}
    rows = {}
    duplicates = 0
    for key in store.keys("trial_results"):
        row = store.get("trial_results", key)
        if row["spec_id"] == spec_id:
            marker = (row["repeat"], row["case_id"], row["condition"])
            duplicates += marker in rows
            rows[marker] = row
    values: dict[str, list[float]] = {"baseline": [], "candidate": []}
    deltas = []
    regressions = []
    failures = []
    missing = 0
    missing_quality = 0
    invalid = 0
    for repeat in range(spec["repeats"]):
        for case in spec["cases"]:
            pair = {condition: rows.get((repeat, case["id"], condition))
                    for condition in ("baseline", "candidate")}
            observed: dict[str, float] = {}
            for condition, observation in pair.items():
                if observation and observation.get("validation_error"):
                    invalid += 1
                    missing += 1
                    missing_quality += 1
                    continue
                accepted = observation.get("accepted") if observation else None
                missing_quality += type(accepted) is not bool
                if condition == "candidate" and accepted is False:
                    failures.append({"repeat": repeat, "case_id": case["id"]})
                value = ((int(accepted) if type(accepted) is bool else None)
                         if metric == "accepted" else observation.get("elapsed_s")
                         if metric == "elapsed_s" and observation else
                         (observation.get("metrics") or {}).get(metric) if observation else None)
                if type(value) not in (int, float) or not math.isfinite(cast(float, value)) or cast(float, value) < 0:
                    missing += 1
                else:
                    observed[condition] = cast(float, value)
                    values[condition].append(cast(float, value))
            if pair["baseline"] and pair["candidate"] and not any(
                    row.get("validation_error") for row in pair.values() if row) \
                    and pair["baseline"].get("accepted") \
                    and pair["candidate"].get("accepted") is False:
                regressions.append({"repeat": repeat, "case_id": case["id"]})
            if set(observed) == {"baseline", "candidate"}:
                deltas.append(observed["candidate"] - observed["baseline"])
    protected = bool(all(spec.get("files", {}).get(condition, {}).get(proposal["target"])
                     == proposal[f"{name}_sha256"]
                     for condition, name in (("baseline", "before"), ("candidate", "after")))
                     and all(case.get("acceptance_files") for case in spec["cases"]))
    delta = sum(deltas) / len(deltas) if deltas and not missing else None
    improvement = (delta if policy["direction"] == "increase" else -delta) \
        if delta is not None else None
    complete = set(rows) == expected and not duplicates
    decision = "inconclusive"
    reasons = []
    if not complete or missing or missing_quality:
        reasons.append("Incomplete or ambiguous paired observations.")
    if invalid:
        reasons.append("Rejected execution receipts are excluded from measured support.")
    if not protected:
        reasons.append("Protected proposal fixtures and acceptance hashes are missing.")
    quality_failed = ((policy["require_no_acceptance_regressions"] and bool(regressions)) or
                      (policy["require_all_candidate_accepted"] and bool(failures)))
    if quality_failed and complete and not missing and not missing_quality:
        decision = "rejected"
        reasons.append("The prospective acceptance constraint failed.")
    elif complete and not missing and not missing_quality and protected and improvement is not None:
        if improvement > policy["minimum_improvement"]:
            decision = "supported"
        elif improvement < 0:
            decision = "rejected"
            reasons.append("The selected metric worsened.")
        else:
            reasons.append("The selected metric does not exceed the prospective improvement minimum.")
    return {"decision": decision, "primary_metric": metric, "primary_metric_delta": delta,
        "improvement": improvement, "pairs": len(deltas), "expected_pairs": len(expected) // 2,
        "missing_observations": missing, "missing_quality": missing_quality,
        "invalid_observations": invalid, "duplicate_observations": duplicates, "acceptance_regressions": regressions,
        "candidate_failures": failures, "protected_inputs": protected,
        "conditions": {name: sum(v) / len(v) if v else None for name, v in values.items()},
        "reasons": reasons, "scope": "command/code correctness" if spec["kind"] == "command_check"
        else "bounded agent-task benefit", "statistical_calibration": False,
        "general_productivity_verified": False}
