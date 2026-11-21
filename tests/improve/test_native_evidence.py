import hashlib
import json
import sqlite3
from pathlib import Path

from agent_usage_tracker.improve.judge import cases
from agent_usage_tracker.improve.store import Store, audit

STAMP = "2026-04-12T10:00:00Z"


def _pi(home: Path, *, branched: bool = False) -> None:
    path = home / ".pi" / "agent" / "sessions" / "project" / "trace.jsonl"
    path.parent.mkdir(parents=True)
    records = [{"type": "session", "id": "pi-run", "cwd": str(home), "timestamp": STAMP}]
    for index, message in enumerate([
        {"role": "user", "content": "Check the build"},
        {"role": "assistant", "stopReason": "toolUse", "content": [
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "toolCall", "id": "check", "name": "bash", "arguments": {"command": "false"}},
        ]},
        {"role": "toolResult", "toolCallId": "check", "toolName": "bash", "isError": True,
         "content": [{"type": "text", "text": "Process exited with code 1"}]},
        {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Failed"}]},
    ]):
        parent = str(index - 1) if index else None
        records.append({"type": "message", "id": str(index),
                        "parentId": "0" if branched and index == 3 else parent,
                        "timestamp": STAMP, "message": message})
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")


def _cursor(home: Path) -> Path:
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
    assert path.read_bytes() == before


def test_branched_pi_trace_is_inspectable_but_ineligible(tmp_path):
    home = tmp_path / "home"
    _pi(home, branched=True)
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
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
    store = Store(tmp_path / "private")
    report = audit(store, home, since_epoch=0, idle_s=0)
    found = cases(store, report["audit_id"])
    assert found
    assert all(not row["coverage"]["complete"] for row in found)
