import json

import pytest

from agent_usage_tracker.improve.evidence import read_session
from agent_usage_tracker.improve.judge import _case_record
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.improve.study_observe import _sample


@pytest.mark.parametrize(("damage", "countable"), [
    (None, True),
    ("tools_missing", False),
    ("mixed_run", False),
    ("missing_clock", False),
    ("reused_call", False),
    ("incomplete_source", False),
    ("older_snapshot", False),
])
def test_count_evidence_uses_full_snapshot_and_requires_complete_event_links(
    tmp_path, damage, countable,
):
    store = Store(tmp_path / "private")
    stamp = "2026-11-02T09:00:00Z"
    events = [
        {"id": "p", "kind": "prompt", "text": "Check", "run_id": "run"},
        {"id": "c", "kind": "call", "call_id": "tool", "text": "Check", "run_id": "run"},
        {"id": "r", "kind": "result", "call_id": "tool", "status": "error",
         "text": "Clipped output", "truncated": True, "run_id": "run"},
        {"id": "e", "kind": "boundary", "text": "turn_complete", "run_id": "run"},
    ]
    for event in events:
        event["timestamp"] = stamp
    if damage == "tools_missing":
        events.pop(2)
    elif damage == "mixed_run":
        events[2]["run_id"] = "next"
    elif damage == "missing_clock":
        del events[2]["timestamp"]
    elif damage == "reused_call":
        events.insert(2, dict(events[1], id="duplicate"))
    session = store.put("sessions", {"agent": "t3", "session_id": "native",
                                    "origin": "interactive", "events": events,
                                    "coverage": {"tool_events_checked": damage != "older_snapshot"}})
    key = store.put("cases", {"session_snapshot": session, "events": [events[0], events[-1]],
                             "omitted_task_events": len(events) - 2,
                             "coverage": {"complete": False,
                                          "source_incomplete": damage == "incomplete_source"}})
    sample = _sample(store, key)
    assert not sample["complete"]
    assert sample["tool_events_complete"] == countable
    if countable:
        assert sample["tool_calls"] == sample["tool_errors"] == 1


@pytest.mark.parametrize(("extra", "expected"), [
    (None, True),
    ("repeated", False),
    ("web_search_call", False),
    ("local_shell_call", False),
    ("tool_search_output", False),
    ("mixed_representation", False),
    ("uncounted_item", False),
    ("compacted", False),
])
def test_count_evidence_checks_original_codex_records(tmp_path, extra, expected):
    stamp = "2026-11-02T09:00:00Z"
    call = {"type": "function_call", "call_id": "tool", "name": "run", "arguments": "{}"}
    result = {"type": "function_call_output", "call_id": "tool", "output": "output line\n" * 1000}
    rows = [
        ("session_meta", {"id": "native", "cwd": str(tmp_path), "originator": "codex_cli_rs"}),
        ("event_msg", {"type": "task_started", "turn_id": "turn"}),
        ("turn_context", {"turn_id": "turn", "model": "model", "cwd": str(tmp_path)}),
        ("event_msg", {"type": "user_message", "message": "Check"}),
        ("response_item", call),
        ("response_item", result),
    ]
    if extra == "repeated":
        rows.extend([("response_item", call), ("response_item", result)])
    elif extra == "mixed_representation":
        rows.append(("event_msg", {"type": "item_completed", "item": {
            "type": "CommandExecution", "id": "other", "exit_code": 0}}))
    elif extra == "uncounted_item":
        rows.append(("event_msg", {"type": "item_completed", "item": {"type": "FileChange"}}))
    elif extra == "compacted":
        rows.append(("compacted", {"message": "private compaction text"}))
    elif extra:
        rows.append(("response_item", {"type": extra}))
    rows.append(("event_msg", {"type": "task_complete", "turn_id": "turn"}))
    path = tmp_path / "codex.jsonl"
    path.write_text("".join(json.dumps({"type": kind, "timestamp": stamp, "payload": payload})
                            + "\n" for kind, payload in rows))
    session = read_session(path, "codex")
    assert session is not None
    assert any(event["truncated"] for event in session["events"] if event["kind"] == "result")
    store = Store(tmp_path / "private")
    snapshot = store.put("sessions", session)
    case = store.put("cases", _case_record(
        session, snapshot, session["coverage"], len(session["events"]) - 1, "selected", False))
    sample = _sample(store, case)
    assert not sample["complete"]
    assert sample["tool_events_complete"] == expected
    assert "private compaction text" not in str(session["events"])
