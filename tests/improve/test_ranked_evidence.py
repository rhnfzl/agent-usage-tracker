import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from agent_usage_tracker.cli import main
from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.access import Evidence
from agent_usage_tracker.improve.semantic_search import SemanticUnavailable
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

    assert main([*args, *query, "--mode", "exact"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    assert main([*args, *query]) == 0
    automatic = json.loads(capsys.readouterr().out)
    assert automatic["search_mode"] == "ranked"
    assert automatic["items"][0]["event_id"] == "precise"
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


def test_session_search_groups_before_paging_and_binds_cursor(tmp_path):
    store = Store(tmp_path / "private")

    def session(texts):
        return store.put("sessions", {"agent": "codex", "coverage": {}, "events": [
            {"id": str(i), "kind": "result", "text": text} for i, text in enumerate(texts)
        ]})

    noisy = session(["cache", "cache failure", "cache failure", "unrelated"])
    other = session(["cache was unavailable"])
    audit = store.put("audits", {"session_ids": [noisy, other], "coverage": {}})
    reader = Evidence("redacted", store, audit)
    first = reader.search("cache failure", mode="sessions", limit=1)
    hit, = first["items"]
    assert (hit["session_id"], hit["event_id"], hit["matching_events"]) == (noisy, "1", 3)
    cursor = first["next_cursor"]
    with pytest.raises(AutError, match="cursor"):
        reader.search("cache failure", mode="ranked", cursor=cursor)
    second = reader.search("cache failure", mode="sessions", cursor=cursor)
    hit, = second["items"]
    assert (hit["session_id"], hit["matching_events"]) == (other, 1)
    assert second["next_cursor"] is None


def test_semantic_and_hybrid_share_the_authorized_index_and_page(tmp_path):
    class Embedder:
        model_id = "local-test"
        prepared = 0

        def prepare(self, rows):
            self.prepared += 1
            assert list(rows) == [(1, "restart cache"), (2, "daemon recovered")]
            return self

        def rank(self, query):
            assert query == "restart"
            return {2: "daemon recovered", 1: "restart cache"}

    store = Store(tmp_path / "private")
    first = store.put("sessions", {"agent": "codex", "coverage": {}, "events": [
        {"id": "words", "kind": "reply", "text": "restart cache"},
        {"id": "meaning", "kind": "reply", "text": "daemon recovered"},
    ]})
    other = store.put("sessions", {"agent": "pi", "coverage": {}, "events": [
        {"id": "outside", "kind": "reply", "text": "restart"},
    ]})
    audit = store.put("audits", {"session_ids": [first, other], "coverage": {}})
    model = Embedder()
    reader = Evidence("full", store, audit, embedder=model)

    semantic = reader.search("restart", session_id=first, mode="semantic")
    assert [hit["event_id"] for hit in semantic["items"]] == ["meaning", "words"]
    hybrid = reader.search("restart", session_id=first, mode="hybrid", limit=1)
    assert hybrid["embedding_model"] == "local-test"
    hit, = hybrid["items"]
    assert (hit["event_id"], hit["word_rank"], hit["semantic_rank"]) == ("words", 1, 2)
    second = reader.search("restart", session_id=first, mode="hybrid", cursor=hybrid["next_cursor"])
    assert [hit["event_id"] for hit in second["items"]] == ["meaning"]
    with pytest.raises(AutError, match="cursor"):
        reader.search("restart", session_id=other, mode="hybrid", cursor=hybrid["next_cursor"])
    assert model.prepared == 1
    with pytest.raises(AutError, match="embedding-model"):
        Evidence("full", store, audit).search("restart", mode="hybrid")


def test_scoped_exact_search_and_exclusion_respect_cursor(tmp_path):
    store = Store(tmp_path / "private")

    def snapshot(name):
        return store.put("sessions", {"agent": name, "coverage": {}, "events": [
            {"id": "first", "kind": "reply", "text": "cache"},
            {"id": "second", "kind": "reply", "text": "cache"},
        ]})

    first, second = snapshot("codex"), snapshot("pi")
    audit = store.put("audits", {"session_ids": [first, second], "coverage": {}})
    reader = Evidence("full", store, audit)
    page = reader.search("cache", session_id=second, limit=1)
    assert [(hit["session_id"], hit["event_id"]) for hit in page["items"]] == [(second, "first")]
    with pytest.raises(AutError, match="cursor"):
        reader.search("cache", session_id=first, cursor=page["next_cursor"])
    with pytest.raises(AutError, match="outside"):
        reader.search("cache", session_id="0" * 64)
    assert reader.search("cache", session_id=second, cursor=page["next_cursor"])["items"][0][
        "event_id"] == "second"
    assert [hit["session_id"] for hit in reader.search(
        "cache", mode="sessions", exclude_sessions=[second])["items"]] == [first]


def test_auto_exposes_hybrid_and_retains_lexical_fallback_on_continuation(tmp_path, monkeypatch):
    class LocalModel:
        model_id = "test"

        def prepare(self, rows):
            assert len(list(rows)) == 3
            return self

        def rank(self, query):
            return {2: "similar", 3: "closest meaning", 1: "literal marker"}

    store = Store(tmp_path / "private")
    key = store.put("sessions", {"agent": "codex", "coverage": {}, "events": [
        {"id": "literal", "kind": "result", "text": "literal marker"},
        {"id": "reordered", "kind": "result", "text": "marker literal"},
        {"id": "meaning", "kind": "result", "text": "closest meaning"},
    ]})
    audit = store.put("audits", {"session_ids": [key], "coverage": {}})
    model = LocalModel()
    reader = Evidence("full", store, audit, embedder=model)
    assert reader.search("literal marker", mode="hybrid", limit=1)["items"][0]["event_id"] == "reordered"
    first = reader.search("literal marker", limit=1)
    assert first["search_mode"] == "hybrid"
    assert first["items"][0]["event_id"] == "literal"
    assert reader.search("literal marker", cursor=first["next_cursor"])["search_mode"] == "hybrid"
    with pytest.raises(AutError, match="cursor"):
        reader.search("literal marker", mode="hybrid", cursor=first["next_cursor"])

    def unavailable(_):
        raise SemanticUnavailable("test model unavailable")

    monkeypatch.setattr(model, "rank", unavailable)
    ranked = reader.search("literal marker", limit=1)
    assert ranked["search_mode"] == "ranked"
    assert ranked["fallback_reason"] == "test model unavailable"
    page = reader.search("literal marker", cursor=ranked["next_cursor"])
    assert [hit["event_id"] for hit in page["items"]] == ["reordered"]
    with pytest.raises(SemanticUnavailable, match="unavailable"):
        reader.search("literal marker", mode="hybrid")
