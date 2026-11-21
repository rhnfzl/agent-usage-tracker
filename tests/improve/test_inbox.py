import json
from datetime import date

import pytest

from agent_usage_tracker.improve import groups, inbox, schedule
from agent_usage_tracker.improve.store import Store

from .test_groups import TODAY, failing_case, judge


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(groups, "repository", lambda path: f"/repos/{path.name}/.git")
    return Store(tmp_path / "private")


def test_a_failing_refresh_keeps_the_cycle_report_and_inbox(store, monkeypatch):
    def boom(store):
        raise KeyError("old record shape")

    def worker(argv, cwd, timeout):
        return {"accepted": False, "timeout": True, "exit_code": -9}

    monkeypatch.setattr(schedule.groups, "refresh", boom)
    monkeypatch.setattr(schedule, "command", worker)
    report = schedule.run_once(store, budget_s=1)
    assert report["status"] == "timeout"
    assert (store.root / "last_cycle.json").exists() and (store.root / "inbox.html").exists()
    rows = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert {"kind": "groups_error", "error": "KeyError"}.items() <= rows[-1].items()


def test_the_cycle_refreshes_groups_before_the_inbox(store, monkeypatch):
    for n in range(3):
        judge(store, failing_case(store, f"s{n}"))
    monkeypatch.setattr(schedule, "command",
                        lambda argv, cwd, timeout: {"accepted": False, "timeout": True,
                                                    "exit_code": -9})
    monkeypatch.setattr(schedule.groups, "date", type("D", (date,), {"today": lambda: TODAY}))
    schedule.run_once(store, budget_s=1)
    page = (store.root / "inbox.html").read_text()
    assert "avoidable_recovery · instruction · Bash · python -m pytest · command_not_found" in page


def test_the_inbox_escapes_group_signals_and_survives_a_bad_report(store):
    report = {"version": 1, "day": "2026-11-20", "window_days": 14, "threshold": 3,
              "audit_id": None, "skipped": 0, "groups": [{
                  "group_key": "k", "signals": {"rubric": "<script>x</script>", "cause": "c",
                  "tool": "", "error_class": "", "command_family": ""},
                  "judgment_ids": ["j"], "case_ids": ["c"], "distinct_tasks": 1,
                  "projects": {"<b>": 1}, "first_work_day": "2026-11-20",
                  "last_work_day": "2026-11-20", "recurring": False, "audit_incidents": 0}]}
    store.append("groups", report_id=store.put("group_reports", report))
    inbox.write(store, [])
    page = (store.root / "inbox.html").read_text()
    assert "<script>" not in page and "&lt;script&gt;x&lt;/script&gt; · c" in page
    assert "&lt;b&gt; (1)" in page
    store.append("groups", report_id="0" * 64)            # missing record
    inbox.write(store, [])
    assert "could not be read" in (store.root / "inbox.html").read_text()


def test_groups_command_prints_the_report(store, tmp_path, capsys, monkeypatch):
    from agent_usage_tracker import cli

    for n in range(3):
        judge(store, failing_case(store, f"s{n}"))
    monkeypatch.setattr(groups, "date", type("D", (date,), {"today": lambda: TODAY}))
    cli.main(["improve", "--root", str(store.root), "groups", "--days", "7", "--threshold", "2"])
    result = json.loads(capsys.readouterr().out)
    assert (result["window_days"], result["threshold"]) == (7, 2)
    assert result["groups"][0]["recurring"]
