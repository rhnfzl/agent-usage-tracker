import json

import pytest

from agent_usage_tracker import cli


def test_desktop_preflight_import_keeps_unknown_evidence(tmp_path, monkeypatch, capsys):
    from agent_usage_tracker.improve.store import Store

    def no_sink():
        raise AssertionError("Desktop evidence must stay private")

    monkeypatch.setattr(cli, "load_config", no_sink)
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps(dict(task_id="task", session_id="aut-session",
                                     parent_call_id="parent", desktop_session_id="desktop")))
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"response": {"ok": False, "command": "snapshot",
                                                "error": {"code": "PERM_DENIED"}}}) + "\n")
    root = tmp_path / "private"
    assert cli.main(["improve", "--root", str(root), "desktop", "--trace",
                     str(tmp_path / "missing.jsonl"), "--responses", str(responses),
                     "--mapping", str(mapping)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["task_acceptance"] == "unknown"
    assert result["coverage"]["complete"] is False
    assert result["commands"] == []
    assert result["orphan_responses"][0]["response_code"] == "PERM_DENIED"
    assert Store(root).get("desktop_imports", result["import_id"])["commands"] == []


def test_desktop_cli_rejects_unreadable_mapping(tmp_path, capsys):
    assert cli.main(["improve", "--root", str(tmp_path / "private"), "desktop", "--trace",
                     str(tmp_path / "missing.jsonl"), "--mapping",
                     str(tmp_path / "missing-mapping.json")]) == 1
    assert "mapping" in capsys.readouterr().err



def test_improve_audit_and_status_do_not_load_sink_configuration(tmp_path, monkeypatch, capsys):
    def forbidden():
        raise AssertionError("Private improvement must not load sink configuration")
    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "private"
    assert cli.main(["improve", "--root", str(root), "audit", "--limit", "2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sessions"] == 0
    assert cli.main(["improve", "--root", str(root), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["records"]["audits"] == 1


def test_improve_rejects_invalid_bounds_without_scanning(tmp_path, capsys):
    assert cli.main(["improve", "--root", str(tmp_path / "private"), "audit",
                     "--limit", "0"]) == 1
    assert "limit" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["run", "diagnose"])
@pytest.mark.parametrize("timed_out", [True, False])
def test_cycle_cli_reports_worker_failure(tmp_path, monkeypatch, capsys, action, timed_out):
    from agent_usage_tracker.improve import schedule
    from agent_usage_tracker.improve.store import Store

    root = tmp_path / "private"
    store = Store(root)
    evidence = store.put("sessions", {"existing": True})
    pending = {"policy_id": "scheduled", "audit": {"audit_id": "unfinished"}}
    (root / "pending_cycle.json").write_text(json.dumps(pending))
    failure = {"accepted": False, "timeout": timed_out, "exit_code": -9 if timed_out else 2,
               "output": "private worker diagnostic", "output_truncated": False}
    monkeypatch.setattr(schedule, "command", lambda *args: failure)
    argv = ["improve", "--root", str(root), action, "--budget-s", "1"]
    if action == "diagnose":
        argv += ["--audit", store.put("audits", {"session_ids": []})]

    code = cli.main(argv)
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == ("timeout" if timed_out else "error")
    assert result["exit_code"] == failure["exit_code"]
    assert json.loads((root / "last_cycle.json").read_text()) == result
    assert store.get("cycle_errors", result["error_id"])["output"] == failure["output"]
    assert store.get("sessions", evidence) == {"existing": True}
    assert json.loads((root / "pending_cycle.json").read_text()) == pending
    assert "output" not in result
    assert failure["output"] not in captured.out + captured.err
    assert code == 1


@pytest.mark.parametrize("action", ["run", "diagnose"])
@pytest.mark.parametrize("status", ["ok", "deferred", "busy"])
def test_cycle_cli_accepts_completion_deferral_and_contention(
        tmp_path, monkeypatch, capsys, action, status):
    from agent_usage_tracker.improve import cli as improve_cli
    from agent_usage_tracker.improve.store import Store

    root = tmp_path / "private"
    report = {"status": status}
    if status != "busy":
        report["diagnosis"] = {"complete": status == "ok", "reason":
                               "complete" if status == "ok" else "budget"}
    monkeypatch.setattr(improve_cli, "run_once", lambda *args, **kwargs: report)
    argv = ["improve", "--root", str(root), action, "--budget-s", "1"]
    if action == "diagnose":
        argv += ["--audit", Store(root).put("audits", {"session_ids": []})]

    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    expected = report["diagnosis"] if action == "diagnose" and status != "busy" else report
    assert json.loads(captured.out) == expected
    assert captured.err == ""


def test_study_shortlist_is_available_through_real_cli(tmp_path, capsys):
    from agent_usage_tracker.improve.store import Store
    root = tmp_path / 'private'
    store = Store(root)
    audit = store.put('audits', {'session_ids': []})
    assert cli.main(['improve', '--root', str(root), 'study', 'shortlist', '--audit', audit]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['audit_id'] == audit
    assert result['distinct_source_sessions'] == 0
