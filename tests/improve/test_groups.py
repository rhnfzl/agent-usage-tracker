import json
from datetime import date, datetime

import pytest

from agent_usage_tracker.improve import groups
from agent_usage_tracker.improve.rubric import command_family
from agent_usage_tracker.improve.store import Store

TODAY = date(2026, 11, 20)


def noon(day):      # local midday, so the work day is the same in every time zone
    return datetime(2026, 11, day, 12).astimezone().isoformat()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(groups, "repository", lambda path: f"/repos/{path.name}/.git")
    return Store(tmp_path / "private")


def failing_case(store, session="s1", *, day=19, error="Exit code 1\nzsh: command not found: x",
                 command="cd repo && python -m pytest -q", cwd="/work/aut", agent="codex"):
    events = [
        {"id": f"{session}-p", "kind": "prompt", "text": "run", "call_id": "", "tool": "",
         "status": "unknown", "timestamp": noon(day)},
        {"id": f"{session}-c", "kind": "call", "text": json.dumps({"command": command}),
         "call_id": f"{session}-x", "tool": "Bash", "status": "unknown", "timestamp": None},
        {"id": f"{session}-r", "kind": "result", "text": error, "call_id": f"{session}-x",
         "tool": "", "status": "error", "timestamp": None}]
    snapshot = store.put("sessions", {"agent": agent, "session_id": session, "cwd": cwd,
                                      "events": events})
    return store.put("cases", {"session_snapshot": snapshot, "events": events,
                               "coverage": {"complete": True}, "omitted_task_events": 0})


def judge(store, case_id, *, rubric="avoidable_recovery", verdict="fail", votes=None,
          version=1, cause="instruction", review=True):
    row = {"case_id": case_id, "rubric": rubric, "verdict": verdict, "rubric_version": version,
           "votes": votes or [verdict, verdict],
           "review": {"verdict": verdict, "cause": cause, "evidence_ids": [], "rationale": "r"}
           if review else None}
    return store.put("judgments", row)


def test_error_class_ignores_paths_numbers_and_quotes():
    one = 'Exit code 2\nError: ENOENT reading "/a/b.txt" at line 3 (0xdeadbeef1)'
    two = "Exit code 7\nError: ENOENT reading '/c/d/e.txt' at line 99 (0x1234567aa)"
    assert groups.error_class(one) == groups.error_class(two) == "nonzero_exit"
    other = groups.error_class("Traceback in /x/y.py line 4")
    assert other.startswith("other:") and len(other) == 14
    assert groups.error_class("Traceback in /p/q/r.py line 80") == other
    assert groups.error_class("Unknown tool: frobnicate") == "unknown_tool"
    assert groups.error_class("zsh: no matches found: *.tmp") == "shell_glob"
    # a specific class wins over the exit code line that Claude Code and Codex print first
    assert groups.error_class("Exit code 127\nzsh: command not found: uvx") == "command_not_found"
    # incidental words in a long output do not pick a specific class
    declined = "Exit code 1\nThe user doesn't want to proceed. The tool use was rejected."
    assert groups.error_class(declined) == "user_declined"
    assert groups.error_class("Exit code 2\nmake: Error 2\nsee conflict in docs") == "nonzero_exit"
    assert groups.error_class("Exit code 1\nBuild failed") == "nonzero_exit"
    assert groups.error_class("! [rejected]  main -> main (fetch first)") == "push_rejected"
    assert groups.error_class("CONFLICT (content): Merge conflict in a.py") == "conflict"
    assert groups.error_class("Exit code 1\n== 2 failed, 3 passed in 1s ==") == "test_failure"
    assert groups.error_class("FAILED tests/x.py::test_a - assert 1") == "test_failure"
    assert groups.error_class("") == ""
    assert "frobnicate" not in groups.error_class("frobnicate went wrong")


def test_command_family_and_tool_signals(store):
    assert command_family("cd repo && python -m pytest -q", "Bash") == "python -m pytest"
    assert command_family('{"command": "git push origin x"}', "Bash") == "git push"
    assert command_family('{"owner": "x"}', "mcp__github__list_issues") == "mcp:github"
    case = failing_case(store, command="rtk git push origin main", error="! [rejected] main")
    values = groups.signals({"rubric": "avoidable_recovery", "review": None},
                            store.get("cases", case), {"events": []})
    assert values == {"rubric": "avoidable_recovery", "cause": "unreviewed", "tool": "Bash",
                      "error_class": "push_rejected", "command_family": "git push"}


def test_a_transcript_word_never_becomes_a_long_signal(store):
    case = failing_case(store, command="'weird name with <html> and a very long tail" + "x" * 80)
    values = groups.signals({"rubric": "r", "review": None}, store.get("cases", case), {})
    assert values["command_family"] == "other"


def test_three_tasks_make_one_recurring_group(store):
    ids = [judge(store, failing_case(store, f"s{n}", day=17 + n)) for n in range(3)]
    second = failing_case(store, "s0", day=18)          # another attempt in the same session
    ids.append(judge(store, second))
    judge(store, failing_case(store, "s9"), verdict="pass")
    flagged = judge(store, failing_case(store, "s8"), verdict="unknown", votes=["fail", "unknown"])
    judge(store, failing_case(store, "s7"), verdict="unknown", votes=["unknown", "pass"])
    report = groups.refresh(store, today=TODAY)
    top = report["groups"][0]
    assert top["recurring"] and top["distinct_tasks"] == 4
    assert sorted(top["judgment_ids"]) == sorted([*ids, flagged])
    assert top["projects"] == {"aut": 4}
    assert (top["first_work_day"], top["last_work_day"]) == ("2026-11-17", "2026-11-19")
    assert top["signals"] == {"rubric": "avoidable_recovery", "cause": "instruction",
        "tool": "Bash", "error_class": "command_not_found", "command_family": "python -m pytest"}
    assert len(report["groups"]) == 1
    assert groups.latest(store)["groups"] == report["groups"]


def test_two_cases_of_one_session_are_one_task(store):
    for day in (18, 19):
        judge(store, failing_case(store, "same", day=day))
    group = groups.refresh(store, today=TODAY)["groups"][0]
    assert len(group["case_ids"]) == 2 and group["distinct_tasks"] == 1
    assert not group["recurring"]


def test_a_judgment_older_than_the_window_is_excluded(store):
    judge(store, failing_case(store, "old", day=6))       # 15 days before TODAY
    judge(store, failing_case(store, "edge", day=7))      # first day of a 14-day window
    report = groups.refresh(store, today=TODAY)
    assert report["groups"][0]["first_work_day"] == "2026-11-07"
    assert report["groups"][0]["distinct_tasks"] == 1


def test_a_future_dated_case_is_excluded(store):
    judge(store, failing_case(store, "future", day=21))    # the day after TODAY
    assert groups.refresh(store, today=TODAY)["groups"] == []


def test_a_quoted_phrase_never_reaches_the_report(store):
    judge(store, failing_case(store, command='git "Confidential client A"'))
    text = json.dumps(groups.refresh(store, today=TODAY))
    assert "Confidential" not in text and "git other" in text


def test_only_the_newest_rubric_version_counts(store):
    case = failing_case(store)
    judge(store, case, version=1)
    judge(store, case, version=2, verdict="pass")
    assert groups.refresh(store, today=TODAY)["groups"] == []


def test_the_last_diagnosis_breaks_a_same_version_tie(store):
    case = failing_case(store)
    failed = judge(store, case)
    passed = judge(store, case, verdict="pass", cause="unknown")
    store.append("diagnosis", judgment_ids=[failed])
    store.append("diagnosis", judgment_ids=[passed])
    assert groups.refresh(store, today=TODAY)["groups"] == []
    store.append("diagnosis", judgment_ids=[failed])
    assert len(groups.refresh(store, today=TODAY)["groups"]) == 1


def test_group_key_is_stable_when_members_change(store):
    for n in range(3):
        judge(store, failing_case(store, f"s{n}"))
    before = groups.refresh(store, today=TODAY)["groups"][0]
    judge(store, failing_case(store, "s3", cwd="/elsewhere/other"))
    after = groups.refresh(store, today=TODAY)["groups"][0]
    assert after["group_key"] == before["group_key"]
    assert after["distinct_tasks"] == 4 and after["projects"] == {"aut": 3, "other": 1}


def test_a_broken_record_is_skipped_not_fatal(store):
    judge(store, failing_case(store))
    store.put("judgments", {"case_id": "f" * 64, "rubric": "x", "verdict": "fail"})
    report = groups.refresh(store, today=TODAY)
    assert report["skipped"] == 1 and len(report["groups"]) == 1


def test_audit_incidents_count_unsampled_errors_with_the_same_signals(store):
    judge(store, failing_case(store))
    events = [
        {"id": f"{kind}{n}", "kind": kind, "call_id": f"x{n}", "status": status,
         "tool": "Bash" if kind == "call" else "",
         "text": "python -m pytest" if kind == "call" else f"Exit code 1\nzsh: command not found: y{n}"}
        for n in range(2) for kind, status in (("call", "unknown"), ("result", "error"))]
    snapshot = store.put("sessions", {"agent": "codex", "session_id": "audited", "events": events})
    audit_id = store.put("audits", {"session_ids": [snapshot], "project": None})
    store.append("audit", audit_id=audit_id)
    project = store.put("audits", {"session_ids": [], "project": "/x"})
    store.append("audit", audit_id=project)              # project audits are not the nightly one
    report = groups.refresh(store, today=TODAY)
    assert report["audit_id"] == audit_id
    assert report["groups"][0]["audit_incidents"] == 2


def test_the_report_holds_no_evidence_text(store):
    secret = "Exit code 1\nsecret-token-value leaked here"
    judge(store, failing_case(store, error=secret, command="pytest -q"))
    text = json.dumps(groups.refresh(store, today=TODAY))
    assert "secret-token-value" not in text and "leaked" not in text


def test_refresh_rejects_bad_arguments(store):
    from agent_usage_tracker.config import AutError

    with pytest.raises(AutError):
        groups.refresh(store, window_days=0)
    with pytest.raises(AutError):
        groups.refresh(store, threshold=0)


def test_t3_shell_items_without_a_tool_name_keep_their_command():
    assert command_family("git push origin main", "command_execution") == "git push"
    assert command_family("pytest -q", "command_execution") == "pytest"


def test_a_json_shaped_result_is_classified_by_its_message():
    def codex(output, code=1):
        return json.dumps({"exit_code": code, "output": output}, sort_keys=True)
    missing = codex("zsh: command not found: uvx", 127)
    failed = codex("== 2 failed, 3 passed in 1s ==")
    assert groups.error_class(missing) == "command_not_found"
    assert groups.error_class(failed) == "test_failure"
    assert groups.error_class(codex("make: Error 2", 2)) == "nonzero_exit"
    assert groups.error_class(json.dumps({"message": "Unknown tool: x"})) == "unknown_tool"
    assert groups.error_class(json.dumps({"isError": True,
                                          "content": "permission denied"})) == "permission_denied"
    nested = json.dumps({"error": {"message": "request timed out"}})
    assert groups.error_class(nested) == "timeout"
    assert groups.error_class(json.dumps(["a", "b"])).startswith("other:")


def test_a_cited_error_beats_a_cited_success_as_the_anchor(store):
    for session in ("a", "b"):
        ok = {"id": f"{session}-ok", "kind": "result", "text": f"On branch {session}",
              "call_id": f"{session}-g", "tool": "", "status": "success", "timestamp": None}
        git = {"id": f"{session}-gc", "kind": "call", "text": json.dumps({"command": "git status"}),
               "call_id": f"{session}-g", "tool": "Bash", "status": "unknown", "timestamp": None}
        case_id = failing_case(store, session, error="Exit code 1\n2 failed",
                               command="uv run pytest")
        case = store.get("cases", case_id)
        case["events"] = [case["events"][0], git, ok, *case["events"][1:]]
        case_id = store.put("cases", case)
        row = {"case_id": case_id, "rubric": "completion_supported", "verdict": "fail",
               "rubric_version": 1, "votes": ["fail", "fail"],
               "review": {"verdict": "fail", "cause": "model", "rationale": "r",
                          "evidence_ids": [f"{session}-ok", f"{session}-r"]}}
        store.put("judgments", row)
    [group] = groups.refresh(store, today=TODAY)["groups"]
    assert group["signals"]["command_family"] == "uv run pytest"
    assert group["signals"]["error_class"] == "test_failure"
    assert group["distinct_tasks"] == 2


def test_a_far_future_timestamp_is_skipped_not_fatal(store, monkeypatch):
    judge(store, failing_case(store, "ok"))
    judge(store, failing_case(store, "bad"))
    real = groups._work_day
    def work_day(case):
        if case["events"][0]["id"].startswith("bad"):
            raise OverflowError("date value out of range")
        return real(case)
    monkeypatch.setattr(groups, "_work_day", work_day)
    report = groups.refresh(store, today=TODAY)
    assert report["skipped"] == 1 and report["groups"][0]["distinct_tasks"] == 1
