import json

from agent_usage_tracker import cli


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
