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


@pytest.mark.parametrize("timed_out", [True, False])
@pytest.mark.parametrize("matching", [True, False])
def test_failed_cycle_keeps_its_status_and_only_its_matching_checkpoint(
        tmp_path, monkeypatch, timed_out, matching):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    judgment = store.put("judgments", {"rubric": "current", "verdict": "unknown",
                                       "case_id": "synthetic"})

    def launch(argv, cwd, timeout):
        deadline = float(argv[argv.index("--deadline") + 1])
        assert deadline > schedule.time.monotonic()
        assert 0 < timeout < 10
        run_id = argv[argv.index("--run-id") + 1]
        policy_id = argv[argv.index("--policy") + 1]
        saved = {"run_id": run_id if matching else "stale", "report": {
            "status": "running", "policy_id": policy_id,
            "diagnosis": {"complete": False, "finished": 1, "judgment_ids": [judgment]}}}
        (store.root / "worker_result.json").write_text(json.dumps(saved))
        return {"accepted": False, "timeout": timed_out, "exit_code": -9 if timed_out else 2}

    monkeypatch.setattr(schedule, "command", launch)
    report = run_once(store, budget_s=10)
    assert report["status"] == ("timeout" if timed_out else "error")
    assert ("progress" in report) == matching
    assert store.keys("cycle_errors")
    assert ("current" in (store.root / "inbox.html").read_text()) == matching


def test_changed_cycle_policy_preserves_previous_evidence(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    old_audit = store.put("audits", {"session_ids": [], "synthetic": "old"})
    new_audit = store.put("audits", {"session_ids": [], "synthetic": "new"})
    (store.root / "pending_cycle.json").write_text(json.dumps({
        "policy_id": "old", "audit": {"audit_id": old_audit}}))
    judgment = store.put("judgments", {"synthetic": "prior"})
    policy = store.put("cycle_policies", {"days": 7, "limit": 30, "budget_s": 10,
        "case_limit": 1, "max_calls": 24, "models": ["local"], "explain_model": None})
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: {"audit_id": new_audit})
    monkeypatch.setattr(schedule, "diagnose", lambda *a, **kw: {"complete": False})
    report = schedule._worker(store, policy, "synthetic")
    assert report["audit"]["audit_id"] == new_audit
    assert store.get("audits", old_audit)["synthetic"] == "old"
    assert store.get("judgments", judgment) == {"synthetic": "prior"}


def test_unreaped_worker_stays_a_timeout_with_private_cleanup_diagnostics(
        tmp_path, monkeypatch, capsys):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    failure = {"accepted": False, "timeout": True, "exit_code": None,
               "cleanup_timeout": True, "pid": 424242}
    monkeypatch.setattr(schedule, "command", lambda *a, **kw: failure)
    from agent_usage_tracker import cli
    code = cli.main(["improve", "--root", str(store.root), "run", "--budget-s", "2"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["status"] == "timeout" and report["exit_code"] is None
    error = store.get("cycle_errors", report["error_id"])
    assert error["cleanup_timeout"] and error["pid"] == 424242


def frozen_audit(store, days=7, limit=30, tag="a"):
    key = store.put("audits", {"session_ids": [], "tag": tag,
                               "source_policy": {"days": days, "limit": limit}})
    return {"audit_id": key, "sessions": 0}


def cycle_policy(store, **changes):
    return store.put("cycle_policies", {"days": 7, "limit": 30, "budget_s": 300, "case_limit": 4,
        "max_calls": 40, "models": ["local"], "explain_model": None, **changes})


def write_pending(store, value):
    (store.root / "pending_cycle.json").write_text(json.dumps(value))


def test_budget_change_keeps_the_frozen_audit(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    frozen = frozen_audit(store)
    write_pending(store, {"policy_id": cycle_policy(store), "audit": frozen})
    policy = cycle_policy(store, budget_s=1200, max_calls=48)
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: pytest.fail("Budget change rescanned"))
    monkeypatch.setattr(schedule, "diagnose", lambda *a, **kw: {"complete": False})
    report = schedule._worker(store, policy, "run")
    assert report["audit"] == frozen and report["audit_s"] == 0
    assert report["worker_started_at"]


def test_days_change_rebuilds_the_audit(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    old = frozen_audit(store)
    write_pending(store, {"audit": old})
    fresh = frozen_audit(store, days=14, tag="new")
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: fresh)
    monkeypatch.setattr(schedule, "diagnose", lambda *a, **kw: {"complete": False})
    report = schedule._worker(store, cycle_policy(store, days=14), "run")
    assert report["audit"] == fresh
    assert store.get("audits", old["audit_id"])["tag"] == "a"
    assert json.loads((store.root / "pending_cycle.json").read_text())["audit"] == fresh


@pytest.mark.parametrize("pending", [None, {"audit": None}])
def test_freeze_cycle_audit_writes_pending(tmp_path, monkeypatch, pending):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    if pending is not None:
        write_pending(store, pending)
    fresh = frozen_audit(store)
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: fresh)
    assert schedule.freeze_cycle_audit(store) == {"status": "frozen", **fresh}
    assert json.loads((store.root / "pending_cycle.json").read_text()) == {"audit": fresh}


def test_freeze_cycle_audit_queues_nothing_when_a_cycle_finishes_during_the_build(
        tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    write_pending(store, {"audit": None})
    fresh = frozen_audit(store)
    finished = {"policy_id": "reviewer", "audit": None}

    def build(*args, **kwargs):
        write_pending(store, finished)      # the reviewer completes its cycle meanwhile
        return fresh

    monkeypatch.setattr(schedule, "audit", build)
    assert schedule.freeze_cycle_audit(store) == {"status": "superseded",
                                                  "audit_id": fresh["audit_id"]}
    assert json.loads((store.root / "pending_cycle.json").read_text()) == finished


@pytest.mark.parametrize("days, kept", [(7, True), (14, False)])
def test_a_pre_v4_pending_audit_is_kept_by_its_policy_days_and_limit(
        tmp_path, monkeypatch, days, kept):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    old = {"audit_id": store.put("audits", {"session_ids": [], "tag": "pre-v4"}), "sessions": 0}
    write_pending(store, {"policy_id": cycle_policy(store), "audit": old})
    fresh = frozen_audit(store, days=days, tag="new")
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: fresh)
    monkeypatch.setattr(schedule, "diagnose", lambda *a, **kw: {"complete": False})
    # The audit budget changes the policy id even at zero; days and limit still match.
    report = schedule._worker(store, cycle_policy(store, days=days, audit_budget_s=0), "run")
    assert report["audit"] == (old if kept else fresh)


def test_freeze_cycle_audit_keeps_a_matching_pending_audit(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    frozen = frozen_audit(store)
    write_pending(store, {"policy_id": "x", "audit": frozen})
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: pytest.fail("Kept audit rescanned"))
    assert schedule.freeze_cycle_audit(store) == {"status": "kept", **frozen}


def test_freeze_cycle_audit_leaves_pending_to_a_running_cycle(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    write_pending(store, {"audit": None})
    fresh = frozen_audit(store)
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: fresh)
    with _locked(store.root / "cycle"):
        result = schedule.freeze_cycle_audit(store)
    assert result == {"status": "busy", "audit_id": fresh["audit_id"]}
    assert json.loads((store.root / "pending_cycle.json").read_text()) == {"audit": None}


def test_freeze_cycle_audit_returns_budget_without_writing_pending(tmp_path, monkeypatch, capsys):
    from agent_usage_tracker import cli
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    write_pending(store, {"audit": None})

    def expired(*args, **kwargs):
        raise schedule._BudgetExpired

    monkeypatch.setattr(schedule, "audit", expired)
    code = cli.main(["improve", "--root", str(store.root), "audit", "--cycle", "--budget-s", "5"])
    assert code == 1
    assert json.loads(capsys.readouterr().out) == {"status": "budget"}
    assert json.loads((store.root / "pending_cycle.json").read_text()) == {"audit": None}


def test_freeze_cycle_audit_skips_while_another_freeze_runs(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    monkeypatch.setattr(schedule, "audit", lambda *a, **kw: pytest.fail("Busy freeze scanned"))
    with _locked(store.root / "cycle_audit"):
        assert schedule.freeze_cycle_audit(store) == {"status": "busy"}
    assert not (store.root / "pending_cycle.json").exists()


def worker_timeout(store, monkeypatch, **kwargs):
    from agent_usage_tracker.improve import schedule

    seen = {}

    def launch(argv, cwd, timeout):
        seen["timeout"] = timeout
        return {"accepted": False, "timeout": True, "exit_code": -9}

    monkeypatch.setattr(schedule, "command", launch)
    run_once(store, budget_s=300, **kwargs)
    return seen["timeout"]


def test_outer_timeout_adds_the_audit_allowance_only_when_an_audit_must_be_built(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    assert worker_timeout(store, monkeypatch) < 300
    assert 300 < worker_timeout(store, monkeypatch, audit_budget_s=600) <= 900
    write_pending(store, {"audit": frozen_audit(store)})
    assert worker_timeout(store, monkeypatch, audit_budget_s=600) < 300


@pytest.mark.parametrize("allowance", [0, 600])
def test_judge_deadline_starts_after_the_worker_audit(tmp_path, monkeypatch, allowance):
    from agent_usage_tracker.improve import schedule

    store = Store(tmp_path / "private")
    clock = [1000.0]
    monkeypatch.setattr(schedule.time, "monotonic", lambda: clock[0])
    fresh = frozen_audit(store)

    def slow_audit(*args, **kwargs):
        clock[0] += 100
        return fresh

    seen = {}
    monkeypatch.setattr(schedule, "audit", slow_audit)
    monkeypatch.setattr(schedule, "diagnose",
                        lambda *a, **kw: seen.update(kw) or {"complete": False})
    report = schedule._worker(store, cycle_policy(store, audit_budget_s=allowance), "run")
    # Without an allowance the audit shares the budget as before; with one the judge keeps all of it.
    assert seen["deadline"] == (1295 if allowance == 0 else 1395)
    assert report["audit_s"] == 100


def test_cycle_audit_rejects_a_project(tmp_path, capsys):
    from agent_usage_tracker import cli

    assert cli.main(["improve", "--root", str(tmp_path / "private"), "audit", "--cycle",
                     "--project", str(tmp_path)]) == 1
    assert "--cycle" in capsys.readouterr().err
