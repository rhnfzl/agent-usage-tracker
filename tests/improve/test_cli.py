import json

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


def test_study_shortlist_is_available_through_real_cli(tmp_path, capsys):
    from agent_usage_tracker.improve.store import Store
    root = tmp_path / 'private'
    store = Store(root)
    audit = store.put('audits', {'session_ids': []})
    assert cli.main(['improve', '--root', str(root), 'study', 'shortlist', '--audit', audit]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['audit_id'] == audit
    assert result['distinct_source_sessions'] == 0
