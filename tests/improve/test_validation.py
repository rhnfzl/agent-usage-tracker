import sys

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import validation
from agent_usage_tracker.improve.proposals import file_digest
from agent_usage_tracker.improve.store import Store, digest


def setup_reference(tmp_path, verdict="unknown"):
    store = Store(tmp_path / "private")
    case = {"events": [{"id": "p", "kind": "prompt", "text": "Check"}],
            "coverage": {"complete": True}, "omitted_task_events": 0}
    case_id = store.put("cases", case)
    policy = {"version": 7, "cases": [case_id], "audit_id": "a", "models": ["local"]}
    job = store.put("diagnoses", policy)
    evaluator = digest({k: v for k, v in policy.items() if k not in {"audit_id", "cases"}})
    store.put("judgments", {"case_id": case_id, "rubric": "completion_supported",
        "evaluation_key": digest({"evaluator": evaluator, "case": case_id,
                                  "rubric": "completion_supported"}), "verdict": verdict})
    refs = [{"evidence_sha256": validation.evidence_key(case),
             "rubric": "completion_supported", "expected": "fail"}]
    oracle = tmp_path / "oracle.py"
    oracle.write_text("import json,sys\nfrom pathlib import Path\n"
        "rows=json.loads(Path(sys.argv[1]).read_text())['references']\n"
        "print(json.dumps({'references':[{'evidence_sha256_parts':"
        "[r['evidence_sha256'][i:i+16] for i in range(0,64,16)],"
        "'rubric':r['rubric'],'expected':'fail'} for r in rows]}))\n")
    spec = store.put("reference_specs", {"version": 1, "split": "holdout",
        "references": refs, "oracle_argv": [sys.executable, str(oracle), "{reference_input}"],
        "oracle_cwd": str(tmp_path), "oracle_files": {str(oracle): file_digest(oracle)},
        "timeout_s": 5})
    return store, spec, job, oracle


def test_reference_preview_never_executes_oracle(tmp_path, monkeypatch):
    store, spec, job, _ = setup_reference(tmp_path)
    monkeypatch.setattr(validation, "command", lambda *a: pytest.fail("Preview executed oracle"))
    assert validation.validate(store, spec, job)["execution_required"]


def test_abstaining_on_a_failure_is_not_counted_as_detection(tmp_path):
    store, spec, job, _ = setup_reference(tmp_path)
    report = validation.validate(store, spec, job, execute=True)
    assert report["counts"]["abstentions"] == 1
    assert report["failure_recall"] == 0
    assert report["false_alarm_rate"] is None
    assert report["scope"] == "executable references only; not real-work accuracy"
    assert not report["human_verified"]
    assert not report["automation_qualified"]


def test_oracle_and_evidence_must_match_the_frozen_reference(tmp_path):
    store, spec, job, oracle = setup_reference(tmp_path, verdict="fail")
    report = validation.validate(store, spec, job, execute=True)
    assert report["counts"]["detected_failures"] == 1
    assert report["failure_recall"] == 1
    oracle.write_text("raise RuntimeError('changed')")
    with pytest.raises(AutError, match="oracle.*changed"):
        validation.validate(store, spec, job, execute=True)


def test_missing_judgments_remain_missing_not_model_abstentions(tmp_path):
    store, spec, _, _ = setup_reference(tmp_path)
    other = store.put("diagnoses", {"version": 8, "cases": [], "audit_id": "a"})
    with pytest.raises(AutError, match="evidence"):
        validation.validate(store, spec, other, execute=True)


def test_unfinished_diagnosis_does_not_produce_accuracy_rates(tmp_path):
    store, spec, job, _ = setup_reference(tmp_path)
    policy = {**store.get("diagnoses", job), "version": 9}
    other = store.put("diagnoses", policy)
    report = validation.validate(store, spec, other, execute=True)
    assert not report["complete"]
    assert report["counts"]["missing"] == 1
    assert report["failure_recall"] is None


def test_disagreeing_oracle_is_not_treated_as_model_truth(tmp_path, monkeypatch):
    store, spec, job, _ = setup_reference(tmp_path)
    monkeypatch.setattr(validation, "command", lambda *a, **kw: {
        "accepted": True, "output_truncated": False, "output": '{"expected":["pass"]}'})
    with pytest.raises(AutError, match="oracle failed or disagreed"):
        validation.validate(store, spec, job, execute=True)


def test_oracle_mutating_frozen_inputs_cannot_publish_rates(tmp_path, monkeypatch):
    store, spec, job, oracle = setup_reference(tmp_path)
    def mutate(*args, **kwargs):
        oracle.write_text("changed")
        return {"accepted": True, "output_truncated": False, "output": '{}'}
    monkeypatch.setattr(validation, "command", mutate)
    with pytest.raises(AutError, match="oracle input changed"):
        validation.validate(store, spec, job, execute=True)
    assert store.keys("reference_oracles")
    assert not store.keys("reference_results")


def test_relative_oracle_script_cannot_escape_frozen_input_checks(tmp_path):
    store, spec_id, job, oracle = setup_reference(tmp_path)
    spec = store.get("reference_specs", spec_id)
    spec["oracle_argv"][1] = oracle.name
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged")
    spec["oracle_files"] = {str(sentinel): file_digest(sentinel)}
    with pytest.raises(AutError, match="command input is not frozen"):
        validation.validate(store, store.put("reference_specs", spec), job, execute=True)
