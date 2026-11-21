import html
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
    page = (store.root / "inbox.html").read_text(encoding="utf-8")
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
    page = (store.root / "inbox.html").read_text(encoding="utf-8")
    assert "<script>" not in page and "&lt;script&gt;x&lt;/script&gt; · c" in page
    assert "&lt;b&gt; (1)" in page
    store.append("groups", report_id=store.put("group_reports", {"groups": [{"old": 1}]}))
    inbox.write(store, [])                                 # an older record shape
    assert "could not be read" in (store.root / "inbox.html").read_text(encoding="utf-8")
    store.append("groups", report_id="0" * 64)            # missing record
    inbox.write(store, [])
    assert "could not be read" in (store.root / "inbox.html").read_text(encoding="utf-8")


def test_groups_command_prints_the_report(store, tmp_path, capsys, monkeypatch):
    from agent_usage_tracker import cli

    for n in range(3):
        judge(store, failing_case(store, f"s{n}"))
    monkeypatch.setattr(groups, "date", type("D", (date,), {"today": lambda: TODAY}))
    cli.main(["improve", "--root", str(store.root), "groups", "--days", "7", "--threshold", "2"])
    result = json.loads(capsys.readouterr().out)
    assert (result["window_days"], result["threshold"]) == (7, 2)
    assert result["groups"][0]["recurring"]


def _proposal(store, tmp_path, title, **extra):
    from agent_usage_tracker.improve.proposals import prepare

    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / f"{len(store.keys('proposals'))}.md").write_text("Use the old command.\n")
    case = store.put("cases", {"events": [{"id": "e", "kind": "result"}]})
    return prepare(store, case, source, source / f"{len(store.keys('proposals'))}.md", "old",
                   "<new>", extra={"title": title, **extra})


def test_the_page_lists_open_proposals_with_safe_commands(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import decide

    monkeypatch.setattr(groups, "repository", lambda path: None)
    store = Store(tmp_path / "my store; rm -rf x")
    shown = _proposal(store, tmp_path, "<script>alert(1)</script>")
    hidden = _proposal(store, tmp_path, "Already decided")
    decide.reject(store, hidden, "no")
    inbox.write(store, [])
    page = (store.root / "inbox.html").read_text(encoding="utf-8")
    short = decide.short_id(store, shown)
    root = f"--root '{store.root}'"
    shown_text = html.unescape(page)
    assert f"aut improve {root} inbox accept {short}" in shown_text
    assert f"aut improve {root} inbox reject {short} --reason" in shown_text
    assert "<script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;new&gt;" in page and "Already decided" not in page and hidden[:8] not in page


def test_the_page_uses_the_plain_command_for_the_default_store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    store = Store(tmp_path / "aut" / "improve")
    key = _proposal(store, tmp_path, "T")
    inbox.write(store, [])
    assert f"aut improve inbox accept {key[:8]}" in (store.root / "inbox.html").read_text("utf-8")


def test_judgments_show_rubric_version_and_trigger(store):
    case = store.put("cases", {"trigger": "error_result", "events": []})
    inbox.write(store, [judge(store, case, version=3)])
    page = (store.root / "inbox.html").read_text(encoding="utf-8")
    assert "<td>avoidable_recovery</td><td>3</td><td>error_result</td><td>fail</td>" in page


def test_a_decision_rewrites_the_page_and_groups_show_their_status(store, tmp_path, capsys,
                                                                   monkeypatch):
    from agent_usage_tracker import cli

    for n in range(3):
        judge(store, failing_case(store, f"s{n}"))
    monkeypatch.setattr(groups, "date", type("D", (date,), {"today": lambda: TODAY}))
    group = groups.refresh(store)["groups"][0]["group_key"]
    key = _proposal(store, tmp_path, "Drafted fix", group_key=group)
    inbox.write(store, [])
    page = (store.root / "inbox.html").read_text(encoding="utf-8")
    assert "Drafted fix" in page and "<td>proposed</td>" in page
    listing = cli.main(["improve", "--root", str(store.root), "inbox", "list"])
    summary = json.loads(capsys.readouterr().out)
    assert listing == 0 and summary["proposals"][0]["id"] == key
    assert summary["groups"][0]["status"] == "proposed" and "Drafted" not in json.dumps(summary)
    assert cli.main(["improve", "--root", str(store.root), "inbox", "reject", key[:8],
                     "--reason", "wrong layer"]) == 0
    page = (store.root / "inbox.html").read_text(encoding="utf-8")
    assert "Drafted fix" not in page and "<td>rejected on " in page


def test_one_broken_proposal_does_not_hide_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(groups, "repository", lambda path: None)
    store = Store(tmp_path / "private")
    good = _proposal(store, tmp_path, "Good one", cause="<i>c</i>", expected_effect="<u>e</u>",
                     rollback="<s>r</s>")
    broken = store.put("proposals", {"case_id": "0" * 64, "scope": "/x", "target": "y"})
    inbox.write(store, [])
    page = (store.root / "inbox.html").read_text(encoding="utf-8")
    assert "Good one" in page and f"Proposal <code>{broken[:8]}</code> could not be read" in page
    assert good[:8] in page
    assert all(tag not in page for tag in ("<i>c", "<u>e", "<s>r"))
    assert "&lt;i&gt;c&lt;/i&gt;" in page
