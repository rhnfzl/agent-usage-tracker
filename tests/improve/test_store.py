import json
import os

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.store import Store, audit


def test_snapshot_is_private_immutable_and_tampering_is_rejected(tmp_path):
    store = Store(tmp_path / "private")
    key = store.put("sessions", {"events": ["private evidence"]})
    assert store.put("sessions", {"events": ["private evidence"]}) == key
    path = store.root / "sessions" / f"{key}.json"
    assert store.get("sessions", key) == {"events": ["private evidence"]}
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert store.root.stat().st_mode & 0o077 == 0
    path.write_text('{"events":[]}')
    with pytest.raises(AutError, match="changed"):
        store.get("sessions", key)
    with pytest.raises(AutError, match="changed"):
        store.put("sessions", {"events": ["private evidence"]})


def test_store_refuses_existing_unrelated_directory(tmp_path):
    (tmp_path / "keep.txt").write_text("keep")
    with pytest.raises(AutError, match="nonempty"):
        Store(tmp_path)
    assert (tmp_path / "keep.txt").read_text() == "keep"
    assert not (tmp_path / ".gitignore").exists()


def test_manifest_deduplicates_copied_sources_and_reports_missing_results(tmp_path):
    home = tmp_path / "home"
    folder = home / ".claude/projects/work"
    folder.mkdir(parents=True)
    raw = json.dumps({"timestamp": "2001-09-09T01:46:40Z", "type": "assistant",
        "sessionId": "s", "entrypoint": "cli",
        "message": {"content": [{"type": "tool_use", "id": "c", "name": "Bash",
                                 "input": {"command": "false"}}]}}) + "\n"
    for name in ("one", "copy"):
        path = folder / f"{name}.jsonl"
        path.write_text(raw)
        os.utime(path, (1_000_000_000, 1_000_000_000))
    report = audit(Store(tmp_path / "private"), home, days=90, limit=10, idle_s=0,
                   since_epoch=0)
    assert report["sessions"] == 1
    assert report["duplicates"] == 1
    assert report["calls"] == 1
    assert report["matched_results"] == 0
    assert report["complete_sessions"] == 0
    manifest = Store(tmp_path / "private").get("audits", report["audit_id"])
    assert len(manifest["session_ids"]) == 1


def test_store_rejects_path_traversal(tmp_path):
    store = Store(tmp_path / "private")
    with pytest.raises(AutError):
        store.get("sessions", "../secret")
    with pytest.raises(AutError):
        store.put("../escape", {})


def test_repeat_writes_use_json_identity_not_python_container_types(tmp_path):
    store = Store(tmp_path / "private")
    key = store.put("policies", {"rubrics": ("pass", "fail")})
    assert store.put("policies", {"rubrics": ("pass", "fail")}) == key
    assert store.get("policies", key) == {"rubrics": ["pass", "fail"]}


def test_nonfinite_or_nonobject_records_do_not_enter_store(tmp_path):
    store = Store(tmp_path / "private")
    for value in ({"score": float("nan")}, ["not", "an", "object"]):
        with pytest.raises(AutError, match="JSON"):
            store.put("cases", value)


def test_imported_codex_history_is_not_current_native_evidence(tmp_path):
    home = tmp_path / "home"
    folder = home / ".codex/sessions"
    folder.mkdir(parents=True)
    (home / ".codex/external_agent_session_imports.json").write_text(json.dumps({
        "records": [{"imported_thread_id": "s", "imported_at": 1_000_000_000}]}))
    records = [{"type": "session_meta", "payload": {"id": "s", "source": "cli"}},
        {"type": "response_item", "timestamp": "2001-09-09T01:46:40Z", "payload": {
            "type": "function_call", "call_id": "old", "name": "read", "arguments": "{}"}}]
    path = folder / "rollout-import.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    os.utime(path, (1_000_000_000, 1_000_000_000))
    result = audit(Store(tmp_path / "private"), home, idle_s=0, since_epoch=0)
    assert result["sessions"] == 0
    assert result["no_recent_evidence"] == 1


def test_audit_excludes_automated_bodies_but_keeps_interactive_evidence(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import evidence

    home = tmp_path / "home"
    folder = home / ".codex/sessions"
    folder.mkdir(parents=True)
    for name, source in (("automated", {"subagent": "review"}), ("human", "cli")):
        records = [{"type": "session_meta", "payload": {"id": name, "source": source}},
                   {"type": "event_msg", "timestamp": "2001-09-09T01:46:40Z",
                    "payload": {"type": "user_message", "message": name}}]
        path = folder / f"rollout-{name}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in records))
        os.utime(path, (1_000_000_000, 1_000_000_000))
    seen = []
    monkeypatch.setattr(evidence, "redact", lambda text: seen.append(text) or text)
    store = Store(tmp_path / "private")
    result = audit(store, home, idle_s=0, since_epoch=0)
    assert result["automated_sources"] == 1
    assert result["sessions"] == 1
    assert seen == ["human"]
    assert store.get("sessions", store.keys("sessions")[0])["events"][0]["text"] == "human"


def test_all_divergent_copies_are_ineligible_for_judging(tmp_path):
    from agent_usage_tracker.improve.judge import cases

    home = tmp_path / "home"
    folder = home / ".claude/projects/work"
    folder.mkdir(parents=True)
    for name in ("one", "two"):
        records = [{"type": "assistant", "timestamp": "2001-09-09T01:46:40Z",
            "sessionId": "same", "entrypoint": "cli", "message": {
                "content": [{"type": "text", "text": name}]}},
            {"type": "system", "timestamp": "2001-09-09T01:46:41Z",
             "sessionId": "same", "subtype": "turn_duration"}]
        path = folder / f"{name}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in records))
        os.utime(path, (1_000_000_000, 1_000_000_000))
    store = Store(tmp_path / "private")
    result = audit(store, home, idle_s=0, since_epoch=0)
    assert result["complete_sessions"] == 0
    rows = cases(store, result["audit_id"])
    assert len(rows) == 2
    assert all(not row["coverage"]["complete"] for row in rows)
