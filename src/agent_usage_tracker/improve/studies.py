"""Freeze prospective policies and report bounded evidence without inferring causality."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from ..config import AutError
from .store import Store, digest
from .study_decision import measure, observed_experiment
from .trial import _spec
from .validation import evidence_key


def fields(value: Any, names: str) -> None:
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise AutError("improve: invalid study fields")


def strings(value: Any, *, empty: bool = False) -> None:
    if not isinstance(value, list) or (not value and not empty) or any(
            not isinstance(item, str) or not item.strip() for item in value) \
            or len(value) != len(set(value)):
        raise AutError("improve: expected distinct nonempty strings")


def identity(store: Store, case_id: str) -> dict[str, str]:
    case = store.get("cases", case_id)
    session = store.get("sessions", case["session_snapshot"])
    # Copies can change event ids, line numbers and snapshot coverage without changing the task.
    content = [{k: v for k, v in event.items() if k not in {"id", "line", "byte_offset", "timestamp"}}
               for event in case["events"]]
    return {"session": digest([session["agent"], session["session_id"]]),
            "content": digest(content), "evidence": evidence_key(case)}


def shortlist(store: Store, audit_id: str) -> dict[str, Any]:
    audit = store.get("audits", audit_id)
    incidents: dict[str, dict[str, Any]] = {}
    sessions = set()
    for snapshot in audit["session_ids"]:
        session = store.get("sessions", snapshot)
        source = digest([session["agent"], session["session_id"]])
        sessions.add(source)
        for event in session["events"]:
            if event["kind"] != "result" or event.get("status") != "error":
                continue
            key = digest([source, event["id"]])
            if key in incidents:
                incidents[key]["snapshots"].append(snapshot)
                continue
            incidents[key] = {"incident_id": key, "source_session": source,
                "event_id": event["id"], "snapshots": [snapshot],
                "signal": digest([event.get("tool"), event.get("text")])}
    counts = Counter(row["signal"] for row in incidents.values())
    return {"audit_id": audit_id, "distinct_source_sessions": len(sessions),
            "incidents": list(incidents.values()), "signal_counts": dict(counts),
            "scope": "Error groups are investigation signals, not causal findings."}


def _manifest(store: Store, value: dict[str, Any]) -> dict[str, Any]:
    fields(value, "version discovery_cases control_cases task_families proposal_id trial_spec_id "
           "primary_metric quality_constraints budget_s provenance reference_result_ids cost_inputs "
           "decision_policy")
    if value["version"] != 1:
        raise AutError("improve: unsupported study version")
    for name in ("discovery_cases", "control_cases", "quality_constraints"):
        strings(value[name])
    strings(value["reference_result_ids"], empty=True)
    ids = value["discovery_cases"] + value["control_cases"]
    if len(ids) != len(set(ids)):
        raise AutError("improve: duplicate study case")
    families = value["task_families"]
    if not isinstance(families, dict) or not set(ids) <= set(families) or any(
            not isinstance(v, str) or not v.strip() for v in families.values()):
        raise AutError("improve: every case needs a task family")
    identities = [identity(store, key) for key in ids]
    for key in families:
        identity(store, key)
    if len({row["content"] for row in identities}) != len(ids):
        raise AutError("improve: copied case evidence")
    discovery = [identity(store, key) for key in value["discovery_cases"]]
    controls = [identity(store, key) for key in value["control_cases"]]
    if {r["session"] for r in discovery} & {r["session"] for r in controls}:
        raise AutError("improve: control source sessions overlap discovery")
    proposal = store.get("proposals", value["proposal_id"])
    if proposal["case_id"] not in value["discovery_cases"]:
        raise AutError("improve: proposal must link discovery evidence")
    trial = _spec(store, value["trial_spec_id"])
    if trial["kind"] == "command_check":
        from pathlib import Path
        from .trial import _acceptance_inputs
        for case in trial["cases"]:
            if case.get("acceptance_files"):
                protected = {"acceptance_argv": case["argv"],
                             "acceptance_files": case["acceptance_files"]}
                roots = [Path(root).resolve() for root in trial["directories"].values()]
                for root in roots:
                    _acceptance_inputs(protected, roots, root)
    policy = value["decision_policy"]
    fields(policy, "direction minimum_improvement require_no_acceptance_regressions "
           "require_all_candidate_accepted")
    if policy["direction"] not in {"increase", "decrease"} or type(
            policy["minimum_improvement"]) not in (int, float) or not (
                0 <= policy["minimum_improvement"] < float("inf")) or any(
                    type(policy[name]) is not bool for name in (
                        "require_no_acceptance_regressions", "require_all_candidate_accepted")):
        raise AutError("improve: invalid prospective decision policy")
    if value["primary_metric"] not in {"accepted", "elapsed_s", "tool_calls", "interventions"}:
        raise AutError("improve: unsupported primary metric")
    if type(value["budget_s"]) is not int or not 1 <= value["budget_s"] <= 3600:
        raise AutError("improve: study budget must be 1..3600 seconds")
    fields(value["provenance"], "setup model tools instructions")
    if any(v is not None and (not isinstance(v, str) or not v.strip())
           for v in value["provenance"].values()):
        raise AutError("improve: provenance must be an explicit source or null for unknown")
    fields(value["cost_inputs"], "operator_minutes money")
    for v in value["cost_inputs"].values():
        if v is not None and (type(v) not in (int, float) or not 0 <= v < float("inf")):
            raise AutError("improve: cost inputs must be finite nonnegative numbers or null")
    for key in value["reference_result_ids"]:
        store.get("reference_results", key)
    return value


def create(store: Store, value: dict[str, Any]) -> dict[str, Any]:
    try:
        _manifest(store, value)
        if observed_experiment(store, value["trial_spec_id"]):
            raise AutError("improve: prospective policy cannot include pre-existing trial results")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AutError("improve: invalid study specification") from exc
    key = store.put("studies", {**value, "known_case_ids": store.keys("cases"),
                                 "created_at": datetime.now(timezone.utc).isoformat()})
    store.append("study_created", study_id=key)
    return {"study_id": key, "prospective": True}


def references(store: Store, study: dict[str, Any]) -> dict[str, Any]:
    frozen = [identity(store, key) for key in study["discovery_cases"] + study["control_cases"]]
    reports = [store.get("reference_results", key) for key in study["reference_result_ids"]]
    relevant = {row["evidence_sha256"] for report in reports for row in report["cases"]}
    lookup: dict[str, list[dict[str, str]]] = {}
    unattributable: set[str] = set()
    unattributable_cases = 0
    unindexable_cases = 0
    for key in store.keys("cases") if relevant else []:
        case = store.get("cases", key)
        if not {"events", "coverage", "omitted_task_events"} <= case.keys():
            unindexable_cases += 1
            continue
        evidence = evidence_key(case)
        if evidence not in relevant:
            continue
        snapshot = case.get("session_snapshot")
        session = store.get("sessions", snapshot) if snapshot is not None else {}
        if not all(isinstance(session.get(name), str) and session[name].strip()
                   for name in ("agent", "session_id")) or not isinstance(case["events"], list) \
                or any(not isinstance(event, dict) for event in case["events"]):
            unattributable.add(evidence)
            unattributable_cases += 1
            continue
        row = identity(store, key)
        lookup.setdefault(evidence, []).append({**row, "family":
            study["task_families"].get(key, "")})
    development = [r for report in reports if report["split"] == "development"
                   for r in report["cases"]]
    unknown_development = sum(not lookup.get(row["evidence_sha256"]) or
                              row["evidence_sha256"] in unattributable for row in development)
    blocked = list(frozen)
    for row in development:
        blocked.extend(lookup.get(row["evidence_sha256"], []))
    blocked_families = {study["task_families"][key] for key in
                        study["discovery_cases"] + study["control_cases"]}
    blocked_families.update(group["family"] for row in development
                            for group in lookup.get(row["evidence_sha256"], [])
                            if group["family"])
    counts: dict[str, Counter[str]] = {}
    seen = set()
    conflicts = 0
    holdout = 0
    overlap = 0
    unknown = 0
    for report in reports:
        for row in report["cases"]:
            marker = (row["evidence_sha256"], row["rubric"])
            if marker in seen:
                conflicts += 1
                continue
            seen.add(marker)
            count = counts.setdefault(row["rubric"], Counter({name: 0 for name in (
                "total", "missing", "abstentions", "misses", "false_alarms",
                "reference_pass", "reference_fail", "reference_unknown")}))
            expected, observed = row["expected"], row.get("observed")
            count["total"] += 1
            count[f"reference_{expected}"] += 1
            count["missing"] += observed is None
            count["abstentions"] += observed == "unknown"
            count["misses"] += expected == "fail" and observed in {"pass", "unknown"}
            count["false_alarms"] += expected == "pass" and observed == "fail"
            if report["split"] == "holdout":
                holdout += 1
                groups = lookup.get(row["evidence_sha256"], [])
                if not groups or row["evidence_sha256"] in unattributable or any(
                        not g["family"] for g in groups):
                    unknown += 1
                if any(g["session"] == b["session"] or g["content"] == b["content"]
                       for g in groups for b in blocked) or any(
                           g["family"] in blocked_families for g in groups if g["family"]):
                    overlap += 1
    rubrics = {}
    for name, count in counts.items():
        rubrics[name] = {**dict(count),
            "abstention_rate": count["abstentions"] / count["total"],
            "missing_rate": count["missing"] / count["total"], "miss_rate": count["misses"] / count["reference_fail"]
            if count["reference_fail"] and not count["missing"] else None,
            "false_alarm_rate": count["false_alarms"] / count["reference_pass"]
            if count["reference_pass"] and not count["missing"] else None}
    return {"rubrics": rubrics, "reports": study["reference_result_ids"],
        "duplicate_references": conflicts, "holdout_overlap": overlap,
        "unknown_holdout_groups": unknown,
        "unknown_development_groups": unknown_development,
        "unattributable_reference_cases": unattributable_cases,
        "unindexable_cases": unindexable_cases,
        "holdout_independence_verified": bool(holdout and not (overlap or unknown or unknown_development or conflicts)),
        "human_calibrated": False, "scope": "Executable references only; human labels unknown."}


def assess(store: Store, study_id: str) -> dict[str, Any]:
    study = store.get("studies", study_id)
    proposal = store.get("proposals", study["proposal_id"])
    spec = store.get("trial_specs", study["trial_spec_id"])
    rows = [store.get("trial_results", key) for key in store.keys("trial_results")]
    rows = [row for row in rows if row["spec_id"] == study["trial_spec_id"]]
    total = 2 * spec["repeats"] * len(spec["cases"])
    trial = {"spec_id": study["trial_spec_id"], "finished": len(rows), "total": total,
        "complete": len(rows) == total, "trial_kind": spec["kind"],
        "accepted": {name: sum(row["condition"] == name and row["accepted"] for row in rows)
                     for name in ("baseline", "candidate")}, "agent_behaviour_verified": False,
        "elapsed_s": sum(row["elapsed_s"] for row in rows),
        "missing_metrics": sum(row.get("metrics") is None for row in rows),
        "conditions": {name: {"runs": len(selected := [row for row in rows
                         if row["condition"] == name]),
            "elapsed_s": sum(row["elapsed_s"] for row in selected),
            "tool_calls": sum(row["metrics"]["tool_calls"] for row in selected
                              if row.get("metrics")) if selected and all(
                                  row.get("metrics") for row in selected) else None,
            "interventions": sum(row["metrics"]["interventions"] for row in selected
                                 if row.get("metrics")) if selected and all(
                                     row.get("metrics") for row in selected) else None}
                         for name in ("baseline", "candidate")}}
    followups = [{"followup_id": key, **row} for key in store.keys("study_followups")
                 if (row := store.get("study_followups", key))["study_id"] == study_id]
    transfers = [{"transfer_id": key, **row} for key in store.keys("study_transfers")
                 if (row := store.get("study_transfers", key))["study_id"] == study_id]
    decision = measure(store, study["trial_spec_id"], study["primary_metric"],
                       study["decision_policy"], proposal)
    fresh_followup = any(row["freshness"]["work_after_policy_verified"] and
        row["independence"]["verified"] and
        measure(store, row["spec"]["trial_spec_id"], study["primary_metric"],
                study["decision_policy"], proposal)["decision"] == "supported" for row in followups)
    return {"study_id": study_id, "scoped_trial_decision": decision,
        "scoped_decision": decision["decision"] if decision["decision"] != "supported"
                           or fresh_followup else "inconclusive",
        "fresh_followup_supported": fresh_followup, "decision_policy": study["decision_policy"],
        "stages": {"discovery": {
        "cases": len(study["discovery_cases"]), "controls": len(study["control_cases"])},
        "references": references(store, study), "trial": trial,
        "followup": followups, "transfer": transfers},
        "provenance": study["provenance"], "cost_inputs": study["cost_inputs"],
        "observed_cost": {"trial_elapsed_s": trial["elapsed_s"]},
        "primary_metric": study["primary_metric"],
        "observed_effects": {"accepted_count_delta":
            trial["accepted"]["candidate"] - trial["accepted"]["baseline"]
            if trial["complete"] and not decision["invalid_observations"] else None,
            "scope": "command/code correctness" if trial["trial_kind"] == "command_check"
                     else "bounded agent-task observations"},
        "quality_constraints": study["quality_constraints"],
        "rollback": {"proposal_id": study["proposal_id"], "target": proposal["target"],
                     "before_sha256": proposal["before_sha256"],
                     "after_sha256": proposal["after_sha256"]},
        "improvement_established": False,
        "unknowns": ["causal attribution", "independent human calibration",
                     "general productivity", "loaded instruction exposure"]}
