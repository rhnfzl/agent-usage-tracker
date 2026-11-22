import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_usage_tracker.improve.judge import cases
from agent_usage_tracker.improve.store import Store, audit

STAMP = "2026-04-12T10:00:00Z"
COMPLETED = datetime.fromisoformat(STAMP).timestamp()


def _pi(home: Path, *, branched: bool = False, retry: bool = False, missing_result: bool = False) -> None:
    path = home / ".pi" / "agent" / "sessions" / "project" / "trace.jsonl"
    path.parent.mkdir(parents=True)
    records = [{"type": "session", "id": "pi-run", "cwd": str(home), "timestamp": STAMP}]
    messages = [
        {"role": "user", "content": "Check the build"},
        {"role": "assistant", "stopReason": "toolUse", "content": [
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "toolCall", "id": "check", "name": "bash",
             "arguments": {"command": "false", "token": "dummy-private-field"}},
        ]},
        {"role": "toolResult", "toolCallId": "check", "toolName": "bash", "isError": True,
         "content": [{"type": "text", "text": "Process exited with code 1"}]},
        {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Failed"}]},
    ]
    if missing_result:
        messages[2].pop("content")
    if retry:
        messages.insert(3, {"role": "assistant", "stopReason": "error", "content": []})
    for index, message in enumerate(messages):
        parent = str(index - 1) if index else None
        records.append({"type": "message", "id": str(index),
                        "parentId": "0" if branched and index == 3 else parent,
                        "timestamp": STAMP, "message": message})
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    os.utime(path, (COMPLETED, COMPLETED))


def _cursor(home: Path, *, compacted: bool = False, image: bool = False, missing_result: bool = False, null_result: bool = False) -> Path:
    path = home / ".cursor" / "chats" / "project" / "cursor-run" / "store.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);"
                         "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
        rows = [
            {"role": "system", "content": "hidden system context"},
            {"role": "user", "content": "<user_query>Check the build</user_query>",
             "providerOptions": {"cursor": {"requestId": "a"}}},
            {"role": "assistant", "content": [
                {"type": "tool-call", "toolCallId": "check", "toolName": "Shell",
                 "args": {"command": "false"}},
            ]},
            {"role": "tool", "content": [
                {"type": "tool-result", "toolCallId": "check", "toolName": "Shell",
                 "result": "Exit code: 1\nCommand output:\n"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "Failed"}]},
            {"role": "user", "content": "Next request", "providerOptions": {"cursor": {"requestId": "b"}}},
        ]
        if missing_result:
            rows[3]["content"][0].pop("result")
        elif null_result:
            rows[3]["content"][0]["result"] = None
        if image:
            rows[1]["content"] = [
                {"type": "text", "text": rows[1]["content"]},
                {"type": "image", "image": "unretained-image-bytes"}]
        if compacted:
            rows.insert(1, {"role": "user", "content": "prior private context",
                            "providerOptions": {"cursor": {"isSummary": True}}})
        ids = []
        for row in rows:
            raw = json.dumps(row).encode()
            identity = hashlib.sha256(raw).hexdigest()
            db.execute("INSERT INTO blobs VALUES (?, ?)", (identity, raw))
            ids.append(identity)
        sequence = b"".join(b"\x0a\x20" + bytes.fromhex(blob) for blob in ids)
        root = hashlib.sha256(sequence).hexdigest()
        db.execute("INSERT INTO blobs VALUES (?, ?)", (root, sequence))
        meta = {"agentId": "cursor-run", "latestRootBlobId": root, "createdAt": 1775988000000}
        db.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(meta).encode().hex(),))
    db.close()
    os.utime(path, (COMPLETED, COMPLETED))
    return path


def test_native_sources_produce_private_grounded_cases(tmp_path):
    home = tmp_path / "home"
    _pi(home)
    path = _cursor(home)
    before = path.read_bytes()
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    assert report["sessions"] == 2
    found = {row["agent"]: row for row in cases(store, report["audit_id"])
             if row["trigger"] == "error_result"}
    assert set(found) == {"pi", "cursor_cli"}
    for case in found.values():
        assert case["coverage"]["complete"]
        assert [event["kind"] for event in case["events"]] == [
            "prompt", "call", "result", "reply", "boundary"]
    assert "hidden system context" not in str(found)
    assert "private reasoning" not in str(found)
    assert "dummy-private-field" not in str(found)
    assert path.read_bytes() == before


def test_branched_pi_trace_is_inspectable_but_ineligible(tmp_path):
    home = tmp_path / "home"
    _pi(home, branched=True)
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    assert report["sessions"] == 1, report
    found = cases(store, report["audit_id"])
    assert found
    assert all(not row["coverage"]["complete"] for row in found)


def test_cursor_skips_registered_automation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _cursor(home)
    monkeypatch.setattr("agent_usage_tracker.improve.cursor_evidence.automated_sessions",
                        lambda: {"cursor-run"})
    report = audit(Store(tmp_path / "private"), home, since_epoch=0, idle_s=0)
    assert report["sessions"] == 0
    assert report["automated_sources"] == 1


def test_cursor_keeps_an_incomplete_root_ineligible(tmp_path):
    home = tmp_path / "home"
    path = _cursor(home)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE blobs SET data=NULL WHERE instr(data, ?) > 0", (b'"role": "system"',))
    db.close()
    os.utime(path, (COMPLETED, COMPLETED))
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    found = cases(store, report["audit_id"])
    assert found
    assert all(not row["coverage"]["complete"] for row in found)


def test_corrupt_cursor_root_leaves_other_sources_readable(tmp_path):
    home = tmp_path / "home"
    _pi(home)
    path = _cursor(home)
    with sqlite3.connect(path) as db:
        (raw,) = db.execute("SELECT value FROM meta WHERE key='0'").fetchone()
        root = json.loads(bytes.fromhex(raw))["latestRootBlobId"]
        db.execute("UPDATE blobs SET data=? WHERE id=?", (b"\x0a", root))
    db.close()
    os.utime(path, (COMPLETED, COMPLETED))
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    assert report["sessions"] == 1
    assert report["unreadable_sources"] == 1
    assert {row["agent"] for row in cases(store, report["audit_id"])} == {"pi"}


def test_cursor_compacted_prefix_cannot_count_as_a_complete_attempt(tmp_path):
    home = tmp_path / "home"
    _cursor(home, compacted=True)
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    [failure] = [row for row in cases(store, report["audit_id"]) if row["trigger"] == "error_result"]
    assert not failure["coverage"]["complete"]
    assert any(e["kind"] == "compaction" for e in failure["events"])
    assert "prior private context" not in str(failure)


def test_cursor_keeps_an_attachment_attempt_ineligible_without_retaining_its_bytes(tmp_path):
    home = tmp_path / "home"
    _cursor(home, image=True)
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    [failure] = [row for row in cases(store, report["audit_id"]) if row["trigger"] == "error_result"]
    assert not failure["coverage"]["complete"]
    assert "[Attachment omitted]" in failure["events"][0]["text"]
    assert "unretained-image-bytes" not in str(failure)


def test_pi_keeps_a_retried_model_error_in_the_same_attempt(tmp_path):
    home = tmp_path / "home"
    _pi(home, retry=True)
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    [failure] = [row for row in cases(store, report["audit_id"]) if row["trigger"] == "error_result"]
    assert failure["coverage"]["complete"]
    assert failure["events"][-2]["text"] == "Failed"


def test_claude_server_web_search_uses_explicit_tool_result(tmp_path):
    import json
    from agent_usage_tracker.improve.evidence import read_session

    trace = tmp_path / "session.jsonl"
    rows = [
        {"type": "user", "uuid": "prompt", "message": {"role": "user", "content": "Find references"}},
        {"type": "assistant", "uuid": "assistant", "message": {"role": "assistant", "content": [
            {"type": "server_tool_use", "id": "search-1", "name": "web_search", "input": {"query": "query"}},
            {"type": "web_search_tool_result", "tool_use_id": "search-1",
             "content": [{"type": "web_search_result", "title": "Reference", "url": "https://example.org/"}]},
            {"type": "server_tool_use", "id": "search-2", "name": "web_search", "input": {"query": "query"}},
            {"type": "web_search_tool_result", "tool_use_id": "search-2",
             "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}},
            {"type": "server_tool_use", "id": "search-3", "name": "web_search", "input": {"query": "query"}},
        ]}},
        {"type": "system", "subtype": "turn_duration"},
    ]
    trace.write_text("".join(json.dumps({"timestamp": "2026-01-02T00:00:00Z", **r}) + "\n" for r in rows))
    report = read_session(trace, "claude_code")
    results = [e for e in report["events"] if e["kind"] == "result"]
    assert [e["status"] for e in results] == ["success", "error"]
    assert "Reference" in results[0]["text"]
    assert report["coverage"]["unmatched_calls"] == 1
    assert not report["coverage"]["complete"]



def test_missing_native_tool_results_remain_gaps_in_pi_and_cursor(tmp_path):
    home = tmp_path / "home"
    _pi(home, missing_result=True)
    _cursor(home, missing_result=True)
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    assert report["sessions"] == 2
    for key in store.get("audits", report["audit_id"])["session_ids"]:
        session = store.get("sessions", key)
        assert session["coverage"]["unmatched_calls"] == 1
        assert not any(event["kind"] == "result" for event in session["events"])
        assert any(event["kind"] == "tool_gap" for event in session["events"])



def test_cursor_preserves_explicit_null_tool_results(tmp_path):
    home = tmp_path / "home"
    _cursor(home, null_result=True)
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    key, = store.get("audits", report["audit_id"])["session_ids"]
    session = store.get("sessions", key)
    result, = [event for event in session["events"] if event["kind"] == "result"]
    assert result["text"] == "null"
    assert session["coverage"]["matched_results"] == 1
