import json

from agent_usage_tracker import cli


def test_observe_entrypoint_import_report_and_local_public_draft(tmp_path, monkeypatch, capsys):
    def no_sink():
        raise AssertionError("Observation commands must not load sink credentials")

    monkeypatch.setattr(cli, "load_config", no_sink)
    db = tmp_path / "observations.sqlite3"
    source = tmp_path / "tools.jsonl"
    source.write_text(json.dumps({"version": 1, "kind": "tool", "id": "attempt-1",
                                  "timestamp": "2026-11-13T12:00:00Z", "status": "error"}) + "\n")
    prefix = ["observe", "--db", str(db)]
    assert cli.main([*prefix, "import", str(source), "--format", "jsonl",
                     "--source", "fixture", "--stream", "commands", "--role", "primary"]) == 0
    json.loads(capsys.readouterr().out)
    assert cli.main([*prefix, "report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["coverage"]["observations"] == 1
    assert sum(group["error"] for group in report["tools"].values()) == 1
    target = tmp_path / "public-draft.json"
    assert cli.main([*prefix, "export-public", "--out", str(target)]) == 0
    capsys.readouterr()
    public = json.loads(target.read_text())
    assert public["publication_status"] == "local_draft"
    assert public["observed_tools"]["other"]["error"] == 1
    assert public["observed_tools"]["desktop"]["error"] is None
    assert "fixture" not in target.read_text()


def test_observe_serve_requires_explicit_authentication(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AUT_OBSERVE_TOKEN", raising=False)
    assert cli.main(["observe", "--db", str(tmp_path / "observations.sqlite3"), "serve"]) == 1
    assert capsys.readouterr().err
