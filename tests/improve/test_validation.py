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


def test_reference_reads_original_job_bound_judgment(tmp_path):
    store, spec, previous, _ = setup_reference(tmp_path)
    policy = {**store.get("diagnoses", previous), "version": 1,
              "rubrics": {"completion_supported": ["Historic question", "Historic definition"]}}
    job = store.put("diagnoses", policy)
    key = policy["cases"][0]
    identity = store.put("judgments", {"case_id": key, "rubric": "completion_supported",
        "verdict": "fail", "job_id": job, "evaluation_key": digest({
            "job": job, "case": key, "rubric": "completion_supported"})})
    report = validation.validate(store, spec, job, execute=True)
    assert report["counts"].get("detected_failures", 0) == 1
    assert report["cases"][0]["judgment_id"] == identity


@pytest.mark.parametrize("trigger", ["control", "merge"])
def test_reference_uses_frozen_output_identity_with_legacy_fallback(tmp_path, trigger):
    from agent_usage_tracker.improve import judge, rubric

    store, spec, old_job, _ = setup_reference(tmp_path, verdict="unknown")
    assert validation.validate(store, spec, old_job, execute=True)["counts"]["abstentions"] == 1
    case_id = store.get("diagnoses", old_job)["cases"][0]
    item = next(r for r in rubric.load() if r.id == "completion_supported")
    policy = {"version": judge.VERSION, "cases": [case_id],
              "rubrics": {item.id: rubric.snapshot(store, item)}}
    evaluator = judge.evaluator(policy, item.id)
    key = judge.evaluation_key(evaluator, case_id, item.id, "a" * 64, trigger=trigger)
    policy["evaluation_keys"] = {case_id: {item.id: key}}
    assert judge.evaluator(policy, item.id) == evaluator
    job = store.put("diagnoses", policy)
    identity = store.put("judgments", {"evaluation_key": key, "case_id": case_id,
                        "rubric": item.id, "verdict": "fail", "outputs_id": "a" * 64,
                        "trigger": trigger})
    report = validation.validate(store, spec, job, execute=True)
    assert report["counts"]["detected_failures"] == 1
    assert report["cases"][0]["judgment_id"] == identity


def test_abstaining_on_a_failure_is_not_counted_as_detection(tmp_path):
    store, spec, job, _ = setup_reference(tmp_path)
    report = validation.validate(store, spec, job, execute=True)
    assert report["counts"]["abstentions"] == 1
    assert report["failure_recall"] == 0
    assert report["false_alarm_rate"] is None
    assert report["scope"] == "executable references only; not real-work accuracy"
    assert not report["human_verified"]
    assert not report["automation_qualified"]


def test_executable_report_matches_shared_scoring_without_changing_its_schema(tmp_path):
    store, spec, job, _ = setup_reference(tmp_path, verdict="fail")
    report = validation.validate(store, spec, job, execute=True)
    assert {key: report[key] for key in validation.score([])} == validation.score([("fail", "fail")])
    assert report["counts"] == {"reference_fail": 1, "abstentions": 0,
        "detected_failures": 1, "missed_failures": 0, "false_alarms": 0,
        "supported_passes": 0, "decisions_on_unknown": 0}


def test_executable_reference_compares_each_saved_vote(tmp_path):
    store, spec, job, _ = setup_reference(tmp_path)
    [old_key] = store.keys("judgments")
    old = store.get("judgments", old_key)
    first = store.put("votes", {"model": "first", "model_rev": "v1"})
    second = store.put("votes", {"model": "second", "model_rev": "v2"})
    store.path("judgments", old_key).unlink()
    store.put("judgments", {**old, "vote_ids": [first, second], "votes": ["fail", "pass"]})

    report = validation.validate(store, spec, job, execute=True)
    assert report["failure_recall"] == 0
    rows = report["model_scores"]["completion_supported"]
    assert [(row["model"], row["model_rev"], row["failure_recall"]) for row in rows] \
        == [("first", "v1", 1), ("second", "v2", 0)]
    assert not report["automation_qualified"]


def test_reference_reports_saved_vote_even_if_the_next_model_never_runs(tmp_path):
    from agent_usage_tracker.improve import judge, rubric

    store, spec, previous, _ = setup_reference(tmp_path)
    [case] = store.get("diagnoses", previous)["cases"]
    item = next(r for r in rubric.load() if r.id == "completion_supported")
    first = {"id": "model-a", "revision": "v1", "protocol": "test"}
    other = {"id": "model-b", "revision": "v1", "protocol": "test"}
    policy = {"version": judge.VERSION, "cases": [case], "models": ["a", "b"],
              "rubrics": {item.id: rubric.snapshot(store, item)},
              "model_identity": {"a": first, "b": other}}
    evaluation = judge.policy_evaluation_key(policy, case, item.id)
    policy["evaluation_keys"] = {case: {item.id: evaluation}}
    job = store.put("diagnoses", policy)
    store.put("votes", {"evaluation_key": digest({"decision": evaluation, "model": first}),
        "model": "model-a", "model_rev": "v1", "case_id": case, "rubric": item.id,
        "distribution": {"pass": 0., "fail": 1., "unknown": 0.}})
    report = validation.validate(store, spec, job, execute=True)
    assert report["counts"]["missing"] == 1
    available, missing = report["model_scores"][item.id]
    assert available["counts"]["detected_failures"] == 1
    assert missing["counts"]["missing"] == 1 and missing["failure_recall"] is None


@pytest.mark.parametrize("pair", [("invalid", "pass"), ("pass", "invalid")])
def test_shared_scoring_rejects_invalid_labels(pair):
    with pytest.raises(AutError, match="scoring"):
        validation.score([pair])


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
