import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from agent_usage_tracker.cli import main
from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.access import Evidence
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


def test_ranked_reader_reuses_only_the_current_scope_across_queries_and_threads(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    session = store.put("sessions", {"agent": "codex", "coverage": {}, "events": [
        {"id": "alpha", "kind": "result", "status": "success", "text": "python tests"},
        {"id": "beta", "kind": "result", "status": "error", "text": "sqlite tests"},
    ]})
    audit = store.put("audits", {"session_ids": [session], "coverage": {}})
    reader = Evidence("redacted", store, audit)
    read = Mock(wraps=reader._read)
    monkeypatch.setattr(reader, "_read", read)

    first = reader.search("tests", mode="ranked", limit=1)
    second = reader.search("tests", mode="ranked", limit=1, cursor=first["next_cursor"])
    assert [part["items"][0]["event_id"] for part in (first, second)] == ["alpha", "beta"]
    with ThreadPoolExecutor(max_workers=4) as workers:
        responses = list(workers.map(lambda term: reader.search(term, mode="ranked"),
                                     ["python", "sqlite"] * 4))
    assert [result["items"][0]["event_id"] for result in responses] == ["alpha", "beta"] * 4
    assert read.call_count == 1

    filtered = reader.search("tests", mode="ranked", status="error")
    assert [hit["event_id"] for hit in filtered["items"]] == ["beta"]
    assert filtered["indexed_events"] == 1
    assert read.call_count == 2
    assert reader.search("tests", mode="ranked", limit=1) == first
    assert read.call_count == 3


def test_ranked_reader_rechecks_capture_availability_and_snapshot_integrity(tmp_path):
    store = Store(tmp_path / "private")
    capture = store.put("session_content", {
        "note": {"text": "password=fixtureonly", "attachment_omitted": False},
    })
    capture_path = store.path("session_content", capture)
    archived = capture_path.rename(tmp_path / "archived.json")
    session = store.put("sessions", {"agent": "codex", "coverage": {}, "full_content_id": capture,
                                    "events": [{"id": "note", "kind": "reply", "text": "fallback"}]})
    audit = store.put("audits", {"session_ids": [session], "coverage": {}})
    reader = Evidence("full", store, audit)
    assert reader.search("fixtureonly", mode="ranked")["items"] == []
    archived.rename(capture_path)
    assert reader.search("fixtureonly", mode="ranked")["items"][0]["event_id"] == "note"
    redacted = Evidence("redacted", store, audit)
    assert redacted.search("fixtureonly", mode="ranked")["items"] == []

    capture_path.rename(archived)
    assert reader.search("fixtureonly", mode="ranked")["items"] == []
    assert reader.search("fallback", mode="ranked")["items"][0]["event_id"] == "note"
    store.path("sessions", session).write_text("{}\n")
    with pytest.raises(AutError, match="private record changed"):
        reader.search("fallback", mode="ranked")


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
