"""Labelled extra cases reuse diagnosis budgets without holding the audit open."""

import json
from dataclasses import replace

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import judge
from agent_usage_tracker.improve.store import Store
from .test_qualify import case


class Backend:
    model_id = "fixture"
    model_rev = "fixed"

    def __init__(self, calls, callback=lambda: None):
        self.calls, self.callback = calls, callback

    def distribution(self, request):
        self.calls.append((request.unit_id, request.task))
        self.callback()
        return {"pass": .99, "fail": .005, "unknown": .005}


def setup(tmp_path, monkeypatch, count):
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config.toml")
    store = Store(tmp_path / "store")
    keys = [case(store, str(i)) for i in range(count)]
    judge.update_queue(store, add=keys)
    audit = store.put("audits", {"session_ids": []})
    return store, keys, audit


def test_next_diagnosis_grades_at_most_eight_queued_cases_and_preserves_new_ids(tmp_path, monkeypatch):
    store, keys, audit = setup(tmp_path, monkeypatch, 10)
    added = case(store, "concurrently-enqueued")
    calls = []
    def callback():
        judge.update_queue(store, add=[added])
    report = judge.diagnose(store, audit, models=("fixture",),
                            backend_for=lambda _: Backend(calls, callback))
    assert len(calls) == 16 and len({key for key, _ in calls}) == 8
    assert report["complete"] and report["total"] == report["finished"] == 0
    assert report["optional_finished"] == report["optional_total"] == 16
    assert set(judge.queued(store)) == set(sorted(keys)[8:] + [added])
    rows = [store.get("judgments", key) for key in report["judgment_ids"]]
    assert all(row["rubric_version"] == 1 and row["verdict"] == "pass" for row in rows)


def test_budget_keeps_partial_case_queued_and_resume_reuses_votes(tmp_path, monkeypatch):
    store, keys, audit = setup(tmp_path, monkeypatch, 2)
    calls = []
    first = judge.diagnose(store, audit, models=("fixture",), max_calls=1,
                          backend_for=lambda _: Backend(calls))
    assert first["complete"] and first["reason"] == "budget"
    assert len(calls) == 1 and set(judge.queued(store)) == set(keys)
    second = judge.diagnose(store, audit, models=("fixture",),
                           backend_for=lambda _: Backend(calls))
    assert second["complete"] and second["calls"] == 3
    assert len(calls) == 4 and judge.queued(store) == []


def test_normal_case_is_not_duplicated_and_blocking_completion_precedes_extras(tmp_path, monkeypatch):
    store, keys, audit = setup(tmp_path, monkeypatch, 2)
    normal, extra = keys
    monkeypatch.setattr(judge, "cases", lambda *args, **kw: [
        {**store.get("cases", normal), "case_id": normal}])
    seen, calls = [], []
    report = judge.diagnose(store, audit, models=("fixture",), max_calls=2,
                            backend_for=lambda _: Backend(calls), progress=seen.append)
    assert report["complete"] and report["total"] == 2 and report["finished"] == 2
    assert calls == [(normal, "improve_avoidable_recovery"),
                     (normal, "improve_completion_supported")]
    assert any(row["complete"] and row["optional_finished"] == 0 for row in seen)
    assert judge.queued(store) == [extra]


def test_queued_model_error_cannot_turn_a_completed_audit_into_failure(tmp_path, monkeypatch):
    store, keys, audit = setup(tmp_path, monkeypatch, 1)

    class Broken(Backend):
        def distribution(self, request):
            raise AutError("fixture failure")

    report = judge.diagnose(store, audit, models=("fixture",),
                            backend_for=lambda _: Broken([]))
    assert report["complete"] and report["reason"] == "optional_error"
    assert judge.queued(store) == keys


@pytest.mark.parametrize("value", [["../bad"], [True], {}, ["a" * 64] * 2,
                                   ["a" * 64] * 10001])
def test_invalid_queue_rejected_before_model_load(tmp_path, monkeypatch, value):
    store, _, audit = setup(tmp_path, monkeypatch, 0)
    (store.root / "judge_queue.json").write_text(json.dumps(value))
    with pytest.raises(AutError, match="queue"):
        judge.diagnose(store, audit, backend_for=lambda _: pytest.fail("Model loaded"))


def test_missing_or_incomplete_queued_evidence_is_rejected_before_models(tmp_path, monkeypatch):
    store, _, audit = setup(tmp_path, monkeypatch, 0)
    key = case(store, "incomplete", coverage={"complete": False})
    judge.update_queue(store, add=[key])
    with pytest.raises(AutError, match="incomplete"):
        judge.diagnose(store, audit, backend_for=lambda _: pytest.fail("Model loaded"))


def test_cached_queued_case_is_removed_without_inference(tmp_path, monkeypatch):
    store, keys, audit = setup(tmp_path, monkeypatch, 1)
    calls = []
    judge.diagnose(store, audit, models=("fixture",), backend_for=lambda _: Backend(calls))
    judge.update_queue(store, add=keys)
    report = judge.diagnose(store, audit, models=("fixture",), backend_for=lambda _: Backend(calls))
    assert report["calls"] == 0 and len(calls) == 2
    assert judge.queued(store) == []


def test_full_normal_rubric_cap_cannot_starve_the_label_queue(tmp_path, monkeypatch, core_rubrics):
    store, keys, audit = setup(tmp_path, monkeypatch, 2)
    normal, extra = keys
    item = replace(core_rubrics[0], max_cases=1)
    monkeypatch.setattr(judge, "_rubrics", lambda: [item])
    monkeypatch.setattr(judge, "cases", lambda *args, **kwargs: [
        {**store.get("cases", normal), "case_id": normal}])
    calls = []
    report = judge.diagnose(store, audit, models=("fixture",),
                            backend_for=lambda _: Backend(calls))
    assert report["complete"] and report["total"] == report["finished"] == 1
    assert report["optional_finished"] == report["optional_total"] == 1
    assert {key for key, _ in calls} == {normal, extra}
    assert judge.queued(store) == []
