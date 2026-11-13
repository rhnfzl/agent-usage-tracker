"""Run fresh trusted agent tasks and retain only scoped, reviewed transfer summaries."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import AutError
from .proposals import file_digest
from .store import Store
from .studies import fields, identity, strings
from .study_decision import experiment_identity, observed_experiment
from .trial import _acceptance_inputs, _spec, run_trial


def followup(store: Store, study_id: str, spec: dict[str, Any], *,
             execute: bool = False) -> dict[str, Any]:
    study = store.get("studies", study_id)
    try:
        fields(spec, "version trial_spec_id evidence_cases task_families producer_id "
               "project_scope provenance trial_case_evidence")
        if spec["version"] != 1:
            raise ValueError
        strings(spec["evidence_cases"])
        if not isinstance(spec["producer_id"], str) or not spec["producer_id"].strip():
            raise ValueError
        if not isinstance(spec["project_scope"], str) or not spec["project_scope"].strip():
            raise ValueError
        if set(spec["task_families"]) != set(spec["evidence_cases"]) or any(
                not isinstance(v, str) or not v.strip() for v in spec["task_families"].values()):
            raise ValueError
        fields(spec["provenance"], "setup model tools instructions")
        if any(v is not None and (not isinstance(v, str) or not v.strip())
               for v in spec["provenance"].values()):
            raise ValueError
        trial = _spec(store, spec["trial_spec_id"])
        mapping = spec["trial_case_evidence"]
        if not isinstance(mapping, dict) or set(mapping) != {c["id"] for c in trial["cases"]}:
            raise AutError("improve: trial evidence mapping must cover every executed case")
        associated = set()
        for linked in mapping.values():
            strings(linked)
            if not set(linked) <= set(spec["evidence_cases"]):
                raise AutError("improve: trial evidence mapping cites unavailable evidence")
            associated.update(linked)
        if associated != set(spec["evidence_cases"]):
            raise AutError("improve: trial evidence mapping must use every declared evidence case")
        overlap = sorted(set(spec["task_families"].values()) & {
            study["task_families"][key] for key in
            study["discovery_cases"] + study["control_cases"]})
        independence = {"declared_family_overlap": overlap, "verified": not overlap,
            "association_authority": "operator_declared_case_mapping",
            "trial_case_evidence": mapping,
            "scope": "Declared grouping and association only; no independent provenance attestation."}
        if trial["kind"] == "command_check":
            for case in trial["cases"]:
                if not case.get("acceptance_files"):
                    raise AutError("improve: follow-up requires protected command acceptance")
                protected = {"acceptance_argv": case["argv"],
                             "acceptance_files": case["acceptance_files"]}
                roots = [Path(root).resolve() for root in trial["directories"].values()]
                for root in roots:
                    _acceptance_inputs(protected, roots, root)
        old_cases = list(study["discovery_cases"] + study["control_cases"])
        for key in store.keys("study_followups"):
            prior = store.get("study_followups", key)
            if prior["study_id"] == study_id and prior["spec"] != spec:
                old_cases.extend(prior["spec"]["evidence_cases"])
        old = [identity(store, key) for key in old_cases]
        old_events = {(identity(store, key)["session"], event["id"])
                      for key in old_cases for event in store.get("cases", key)["events"]}
        fresh_events = {(identity(store, key)["session"], event["id"])
                        for key in spec["evidence_cases"]
                        for event in store.get("cases", key)["events"]}
        if old_events & fresh_events:
            raise AutError("improve: follow-up reuses prior source event identities")
        fresh = [identity(store, key) for key in spec["evidence_cases"]]
        if len({row["content"] for row in fresh}) != len(fresh) or any(
                row["content"] == prior["content"]
                for row in fresh for prior in old):
            raise AutError("improve: follow-up evidence must be fresh, not copied discovery")
        observed_times = [event.get("timestamp") for key in spec["evidence_cases"]
                          for event in store.get("cases", key)["events"]]
        temporal_verified = bool(observed_times)
        for stamp in observed_times:
            try:
                temporal_verified = temporal_verified and isinstance(stamp, str) and (
                    datetime.fromisoformat(stamp) > datetime.fromisoformat(study["created_at"]))
            except (ValueError, TypeError):
                temporal_verified = False
        freshness = {"observed_event_times": observed_times,
            "policy_created_at": study["created_at"],
            "work_after_policy_verified": temporal_verified,
            "source_event_overlap": False,
            "scope": "Observed timestamps only; participant and loaded instruction exposure unknown."}
        proposal = store.get("proposals", study["proposal_id"])
        relative = proposal["target"]
        for condition, expected in (("baseline", proposal["before_sha256"]),
                                    ("candidate", proposal["after_sha256"])):
            path = Path(trial["directories"][condition]) / relative
            if trial["files"][condition].get(relative) != expected or file_digest(path) != expected:
                raise AutError("improve: follow-up candidate exposure is not hash-pinned")
        if experiment_identity(store, spec["trial_spec_id"]) == experiment_identity(
                store, study["trial_spec_id"]):
            raise AutError("improve: follow-up requires a fresh trial specification")
        resume = any(store.get("study_followup_specs", saved) ==
                     {"study_id": study_id, "spec": spec}
                     for saved in store.keys("study_followup_specs"))
        if observed_experiment(store, spec["trial_spec_id"], allow_resume=resume):
            raise AutError("improve: follow-up cannot reuse prior trial results")
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        raise AutError("improve: invalid follow-up specification") from exc
    if not execute:
        return {"study_id": study_id, "execution_required": True,
                "trial_spec_id": spec["trial_spec_id"], "evidence_cases": spec["evidence_cases"],
                "candidate_exposure": "protected fixture hashes", "freshness": freshness,
                "independence": independence, "executed": 0}
    store.put("study_followup_specs", {"study_id": study_id, "spec": spec})
    result = run_trial(store, spec["trial_spec_id"], execute=True, budget_s=study["budget_s"])
    if trial["kind"] == "command_check":
        for case in trial["cases"]:
            protected = {"acceptance_argv": case["argv"],
                         "acceptance_files": case["acceptance_files"]}
            for root in roots:
                _acceptance_inputs(protected, roots, root)
    record = {"study_id": study_id, "spec": spec, "trial": result, "freshness": freshness,
              "independence": independence,
              "scope": "command/code correctness" if trial["kind"] == "command_check"
                       else "bounded agent-task observations",
              "general_productivity_verified": False}
    key = store.put("study_followups", record)
    store.append("study_followup", study_id=study_id, followup_id=key)
    return {"followup_id": key, **record}


def transfer(store: Store, study_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    study = store.get("studies", study_id)
    try:
        fields(spec, "version participant_id producer_id project_scope consent "
               "outcome_ids reviewed_outcome_ids")
        fields(spec["consent"], "participant_id project_scope status")
        if spec["version"] != 1 or any(not isinstance(spec[k], str) or not spec[k].strip()
                for k in ("participant_id", "producer_id", "project_scope")):
            raise ValueError
        consent = spec["consent"]
        if consent != {"participant_id": spec["participant_id"],
                       "project_scope": spec["project_scope"], "status": "granted"}:
            raise AutError("improve: participant consent is absent, revoked or out of scope")
        strings(spec["outcome_ids"])
        strings(spec["reviewed_outcome_ids"])
        if set(spec["outcome_ids"]) != set(spec["reviewed_outcome_ids"]):
            raise AutError("improve: only explicitly reviewed outcomes can transfer")
        findings = []
        for key in spec["outcome_ids"]:
            outcome = store.get("study_followups", key)
            source = outcome["spec"]
            if outcome["study_id"] != study_id or source["producer_id"] == spec["producer_id"] \
                    or source["project_scope"] != spec["project_scope"]:
                raise AutError("improve: transfer needs distinct producer and matching project scope")
            if set(source["evidence_cases"]) & set(study["discovery_cases"] + study["control_cases"]):
                raise AutError("improve: transfer outcome reuses discovery")
            findings.append({"outcome_id": key, "finished": outcome["trial"]["finished"],
                "total": outcome["trial"]["total"], "accepted": outcome["trial"]["accepted"],
                "complete": outcome["trial"]["complete"]})
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AutError("improve: invalid participant transfer specification") from exc
    record = {"study_id": study_id, "participant_id": spec["participant_id"],
        "producer_id": spec["producer_id"], "project_scope": spec["project_scope"],
        "consent": consent, "findings": findings, "actual_participant_pilot_verified": False,
        "scope": "Reviewed stored outcomes only; participant identity and independence unverified."}
    key = store.put("study_transfers", record)
    store.append("study_transfer", study_id=study_id, transfer_id=key)
    return {"transfer_id": key, **record}
