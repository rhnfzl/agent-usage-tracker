import json

from agent_usage_tracker.cli import main
from agent_usage_tracker.improve.store import Store


def test_ranked_search_finds_reordered_terms_and_pages_within_the_grant(tmp_path, capsys):
    store = Store(tmp_path / "private")
    primary = []
    for event_id, text in (
        ("precise", "Cache permissions failed. The workspace directory is read-only."),
        ("weaker", "Cache entries expire."),
        ("secret", "password=fixtureonly"),
    ):
        primary.append(store.put("sessions", {
            "agent": "codex", "coverage": {"complete": True},
            "events": [{"id": event_id, "kind": "result", "status": "error", "text": text}],
        }))
    outside = store.put("sessions", {"agent": "codex", "coverage": {}, "events": [
        {"id": "other", "kind": "result", "text": "permissions cache"}]})
    assert outside not in primary
    audit_id = store.put("audits", {"session_ids": primary, "coverage": {}})
    args = ["evidence", "--root", str(store.root), "--audit", audit_id]
    query = ["search", "--query", "permissions cache", "--limit", "1"]

    assert main([*args, *query]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    assert main([*args, *query, "--mode", "ranked"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["items"][0]["event_id"] == "precise"
    assert first["next_cursor"]
    assert main([*args, *query, "--mode", "ranked", "--cursor", first["next_cursor"]]) == 0
    second = json.loads(capsys.readouterr().out)
    assert [hit["event_id"] for hit in second["items"]] == ["weaker"]
    assert second["next_cursor"] is None
    assert main([*args, "--content", "redacted", *query, "--mode", "ranked",
                 "--cursor", first["next_cursor"]]) == 1
    capsys.readouterr()
    assert main([*args, "--content", "redacted", "search", "--query", "fixtureonly",
                 "--mode", "ranked"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []


def test_commit_provenance_uses_matched_calls_and_the_selected_scope(tmp_path, capsys):
    store = Store(tmp_path / "private")

    def snapshot(command):
        return store.put("sessions", {"agent": "codex", "coverage": {}, "events": [
            {"id": "c", "kind": "call", "call_id": "commit", "text": command},
            {"id": "r", "kind": "result", "call_id": "commit", "status": "success",
             "text": "[branch abcdef1] private subject"},
        ]})

    first = snapshot("git commit -m change")
    false = snapshot("echo 'git commit -m change'")
    outside = snapshot("git commit -m another")
    assert outside != first
    audit = store.put("audits", {"session_ids": [first, false], "coverage": {}})
    args = ["evidence", "--root", str(store.root), "--audit", audit, "--content", "metadata"]
    assert main([*args, "commits", "abcdef1234567890"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["items"] == [{
        "session_id": first, "call_event_id": "c", "result_event_id": "r",
        "observed_sha": "abcdef1", "match": "observed_prefix",
    }]
