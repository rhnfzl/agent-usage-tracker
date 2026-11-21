from datetime import date
from zoneinfo import ZoneInfo

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import scores
from agent_usage_tracker.improve.store import Store

TODAY = date(2026, 11, 21)


def case(store, *, trigger="control", stamp="2026-11-19T23:30:00Z", agent="codex"):
    return store.put("cases", {"agent": agent, "trigger": trigger, "events": [
        {"timestamp": None}, {"timestamp": stamp, "text": "private text"}]})


def judge(store, case_id, *, verdict="fail", version=1, policy=None, **extra):
    job = store.put("diagnoses", policy if policy is not None else {"model": verdict})
    row = {"job_id": job, "case_id": case_id, "rubric": "avoidable_recovery",
           "verdict": verdict, **extra}
    if version is not None:
        row["rubric_version"] = version
    return job, store.put("judgments", row)


def test_work_day_uses_ledger_zone_and_never_judged_day(tmp_path):
    store = Store(tmp_path)
    job, key = judge(store, case(store))
    store.append("diagnosis", job_id=job, judgment_ids=[key])
    original = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    rows = scores.daily(Store(tmp_path, read_only=True), today=TODAY, zone=ZoneInfo("Asia/Tokyo"))
    assert rows == [{"day": "2026-11-20", "rubric_id": "avoidable_recovery", "rubric_version": 1,
                     "harness": "codex", "trigger": "control", "passed": 0, "failed": 1,
                     "unknown": 0, "qualified": False}]
    assert {p: p.read_bytes() for p in original} == original
    assert scores.daily(store, today=TODAY, days=1) == []


def test_newest_diagnosis_and_stopped_cycle_win_per_version(tmp_path):
    store = Store(tmp_path)
    c = case(store)
    old_job, old = judge(store, c)
    new_job, new = judge(store, c, verdict="pass")
    store.append("diagnosis", job_id=old_job, judgment_ids=[old])
    store.append("cycle_finished", progress={"diagnosis": {"job_id": new_job}})
    assert scores.daily(store, today=TODAY)[0]["passed"] == 1
    judge(store, c, version=2, reason="incomplete_evidence")
    rows = scores.daily(store, today=TODAY)
    assert len(rows) == 2
    assert rows[1]["rubric_version"] == 2 and rows[1]["unknown"] == 1
    store.append("cycle_finished", diagnosis={"job_id": old_job, "judgment_ids": [old]})
    assert scores.daily(store, today=TODAY)[0]["failed"] == 1


def test_legacy_versions_and_separate_triggers_and_harnesses(tmp_path):
    from .test_rubric import OLD_CHOICES, OLD_RUBRICS
    store = Store(tmp_path)
    c = case(store)
    judge(store, c, version=None, policy={"rubrics": OLD_RUBRICS,
                                         "choice_descriptions": OLD_CHOICES})
    judge(store, c, version=None, policy={})
    judge(store, case(store, trigger="error_result"))
    judge(store, c, trigger="merge")
    judge(store, case(store, agent="t3"))
    rows = scores.daily(store, today=TODAY)
    assert len(rows) == 5
    assert {r["rubric_version"] for r in rows} == {0, 1}
    assert {r["trigger"] for r in rows} == {"control", "error_result", "merge"}
    assert sum(r["failed"] for r in rows) == 5


def test_newest_qualification_wins_and_future_work_is_excluded(tmp_path):
    store = Store(tmp_path)
    judge(store, case(store))
    judge(store, case(store, stamp="2026-12-01T12:00:00Z"))
    for stamp, qualified in [("2026-11-21T12:00:00Z", False),
                             ("2026-11-20T12:00:00Z", True)]:
        store.put("qualifications", {"rubric_id": "avoidable_recovery", "rubric_version": 1,
                                     "computed_at": stamp, "qualified": qualified})
    rows = scores.daily(store, today=TODAY)
    assert len(rows) == 1 and rows[0]["failed"] == 1 and not rows[0]["qualified"]
    store.put("qualifications", {"rubric_id": "avoidable_recovery", "rubric_version": 1,
                                 "computed_at": "2026-11-22T12:00:00Z", "qualified": True})
    assert scores.daily(store, today=TODAY)[0]["qualified"]


def test_read_only_store_refuses_missing_marker_and_writes(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(AutError, match="no improvement store"):
        Store(root, read_only=True)
    assert not root.exists()
    root.mkdir()
    with pytest.raises(AutError, match="no improvement store"):
        Store(root, read_only=True)
    Store(root)
    store = Store(root, read_only=True)
    with pytest.raises(AutError, match="read-only"):
        store.put("cases", {})
    with pytest.raises(AutError, match="read-only"):
        store.append("publish")
