import sys
import json

import pytest

from agent_usage_tracker.improve.schedule import run_once
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.state import _locked


def test_overlapping_cycle_defers_without_starting_worker(tmp_path):
    store = Store(tmp_path / "private")
    with _locked(store.root / "cycle"):
        result = run_once(store, budget_s=1)
    assert result["status"] == "busy"
    assert not store.keys("audits")


def test_cycle_records_timeout_and_leaves_existing_evidence(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule
    from agent_usage_tracker.improve.trial import command

    store = Store(tmp_path / "private")
    old = store.put("sessions", {"existing": True})
    monkeypatch.setattr(schedule, "command", lambda argv, cwd, timeout: command(
        [sys.executable, "-c", "import time; time.sleep(10)"], cwd, timeout))
    result = run_once(store, budget_s=1)
    assert result["status"] == "timeout"
    assert store.get("sessions", old) == {"existing": True}
    assert (store.root / "last_cycle.json").exists()


def test_failed_worker_preserves_its_diagnostic_output_privately(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    failure = {"accepted": False, "timeout": False, "exit_code": 2,
               "output": "model weights could not be read", "output_truncated": False}
    monkeypatch.setattr(schedule, "command", lambda *args: failure)
    result = run_once(store, budget_s=1)
    assert result["status"] == "error"
    assert "output" not in result
    assert store.get("cycle_errors", result["error_id"])["output"] == failure["output"]


def test_worker_does_not_require_a_source_checkout_as_working_directory(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")

    def launch(argv, cwd, timeout):
        assert cwd == store.root
        return {"accepted": False, "timeout": True, "exit_code": -9}

    monkeypatch.setattr(schedule, "command", launch)
    assert run_once(store, budget_s=1)["status"] == "timeout"


def test_inbox_shows_only_current_judgments_and_escapes_model_text(tmp_path):
    from agent_usage_tracker.improve.schedule import inbox

    store = Store(tmp_path / "private")
    store.put("judgments", {"rubric": "old", "verdict": "fail", "case_id": "old",
                            "review": {"rationale": "obsolete assessment"}})
    current = store.put("judgments", {"rubric": "current", "verdict": "unknown", "case_id": "new",
                                     "review": {"rationale": "<script>alert(1)</script>"}})
    inbox(store, [current])
    text = (store.root / "inbox.html").read_text()
    assert "obsolete assessment" not in text
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_worker_resumes_its_frozen_audit_after_a_budget_stop(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    audit_id = store.put("audits", {"session_ids": []})
    policy_id = store.put("cycle_policies", {"days": 7, "limit": 30, "budget_s": 60,
        "case_limit": 4, "max_calls": 4, "models": ["local"], "explain_model": None})
    frozen = {"audit_id": audit_id, "sessions": 0}
    monkeypatch.setattr(schedule, "audit", lambda *args, **kwargs: frozen)
    monkeypatch.setattr(schedule, "diagnose", lambda *args, **kwargs: {"complete": False})
    first = schedule._worker(store, policy_id, "first")
    assert first["status"] == "deferred"

    def rescan(*args, **kwargs):
        raise AssertionError("An unfinished cycle must keep its frozen inputs")

    monkeypatch.setattr(schedule, "audit", rescan)
    monkeypatch.setattr(schedule, "diagnose", lambda *args, **kwargs: {"complete": True})
    second = schedule._worker(store, policy_id, "second")
    assert second["status"] == "ok"
    assert second["audit"]["audit_id"] == audit_id


def test_direct_diagnosis_preserves_the_scheduled_pending_audit(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    audit_id = store.put("audits", {"session_ids": []})
    pending = {"policy_id": "scheduled", "audit": {"audit_id": "other"}}
    (store.root / "pending_cycle.json").write_text(json.dumps(pending))
    policy = store.put("cycle_policies", {"audit_id": audit_id, "budget_s": 5,
        "case_limit": 4, "max_calls": 4, "models": ["local"], "explain_model": None})
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: pytest.fail("Direct diagnosis rescanned"))
    monkeypatch.setattr(schedule, "diagnose", lambda *a, **kw: {"complete": True})
    assert schedule._worker(store, policy, "direct")["audit"] == {"audit_id": audit_id}
    assert json.loads((store.root / "pending_cycle.json").read_text()) == pending
