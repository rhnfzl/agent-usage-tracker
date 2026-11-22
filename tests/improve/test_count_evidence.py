import json

import pytest

from agent_usage_tracker.improve.access import Evidence
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


@pytest.mark.parametrize(("extra", "expected", "model_calls"), [
    (None, True, 1),
    ("repeated", False, None),
    ("web_search_call", False, 1),
    ("local_shell_call", False, 1),
    ("tool_search_output", False, 1),
    ("mixed_representation", False, 1),
    ("same_id_execution", False, 1),
    ("uncounted_item", False, 1),
    ("compacted", False, None),
    ("wrong_result_type", True, None),
    ("orphan", False, None),
    ("result_before_call", True, None),
    ("new_turn_without_close", True, None),
    ("unknown_origin", True, None),
])
def test_count_evidence_checks_original_codex_records(tmp_path, extra, expected, model_calls):
    stamp = "2026-11-02T09:00:00Z"
    call = {"type": "function_call", "call_id": "tool", "name": "run", "arguments": "{}"}
    result = {"type": "function_call_output", "call_id": "tool", "output": "output line\n" * 1000}
    rows = [
        ("session_meta", {"id": "native", "cwd": str(tmp_path),
                          "source": "unknown" if extra == "unknown_origin" else "cli"}),
        ("event_msg", {"type": "task_started", "turn_id": "turn"}),
        ("turn_context", {"turn_id": "turn", "model": "model", "cwd": str(tmp_path)}),
        ("event_msg", {"type": "user_message", "message": "Check"}),
        ("response_item", call),
        ("response_item", result),
    ]
    if extra == "repeated":
        rows.extend([("response_item", call), ("response_item", result)])
    elif extra in {"mixed_representation", "same_id_execution"}:
        rows.append(("event_msg", {"type": "item_completed", "item": {
            "type": "CommandExecution", "id": "tool" if extra == "same_id_execution" else "other",
            "exit_code": 0}}))
    elif extra == "wrong_result_type":
        rows[-1] = ("response_item", dict(result, type="custom_tool_call_output"))
    elif extra == "orphan":
        rows.append(("response_item", dict(result, call_id="orphan")))
    elif extra == "new_turn_without_close":
        rows.append(("event_msg", {"type": "task_started", "turn_id": "turn"}))
    elif extra == "uncounted_item":
        rows.append(("event_msg", {"type": "item_completed", "item": {"type": "FileChange"}}))
    elif extra == "compacted":
        rows.append(("compacted", {"message": "private compaction text"}))
    elif extra and extra not in {"result_before_call", "unknown_origin"}:
        rows.append(("response_item", {"type": extra}))
    rows.append(("event_msg", {"type": "task_complete", "turn_id": "turn"}))
    path = tmp_path / "codex.jsonl"
    path.write_text("".join(json.dumps({"type": kind, "timestamp":
                            "2026-11-02T08:59:59Z" if extra == "result_before_call" and payload is result
                            else stamp, "payload": payload})
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
    assert sample["codex_model_calls"] == model_calls
    assert "private compaction text" not in str(session["events"])
    if extra == "mixed_representation":
        audit = store.put("audits", {"session_ids": [snapshot], "coverage": {}})
        evidence = Evidence("metadata", store=store, audit_id=audit)
        row = evidence.event(snapshot, session["events"][-1]["id"])
        assert row["codex_model_call_receipt"] == {"count": 1, "started_at": "2026-11-02T09:00:00+00:00"}
        assert "text" not in row


@pytest.mark.parametrize("mode", ["original", "moved", "fork", "limited"])
def test_native_session_revision_is_bound_to_the_first_observed_turn(tmp_path, mode):
    rev = "b" * 40
    path = tmp_path / "session.jsonl"
    meta = {"id": "native", "source": "cli", "cwd": str(tmp_path), "git": {"commit_hash": rev}}
    if mode == "fork":
        meta.update(forked_from_id="parent", subagent_history_start_ordinal=1)
    rows = [("session_meta", meta)]
    for i in ("1", "2"):
        cwd = tmp_path / "other" if mode == "moved" else tmp_path
        rows.extend([
            ("event_msg", {"type": "task_started", "turn_id": i}),
            ("turn_context", {"turn_id": i, "model": "model", "cwd": str(cwd)}),
            ("event_msg", {"type": "user_message", "message": "check " + i}),
            ("event_msg", {"type": "task_complete", "turn_id": i}),
        ])
    path.write_text("".join(json.dumps({"type": kind, "timestamp": "2026-11-02T09:00:00Z",
                                      "payload": payload}) + "\n" for kind, payload in rows))
    limit = len(path.read_bytes()) - 25 if mode == "limited" else 16 * 1024 * 1024
    session = read_session(path, "codex", recent=True, max_bytes=limit)
    assert session is not None
    boundaries = [event for event in session["events"] if event["kind"] == "boundary"]
    assert boundaries
    assert [row["turn_context"].get("session_start_revision") for row in boundaries] == (
        [rev, None] if mode == "original" else [None] * len(boundaries))
    if mode == "original":
        store = Store(tmp_path / "private")
        snapshot = store.put("sessions", session)
        audit = store.put("audits", {"session_ids": [snapshot], "coverage": {}})
        for grant in ("metadata", "redacted"):
            evidence = Evidence(grant, store=store, audit_id=audit)
            event = evidence.event(snapshot, boundaries[0]["id"])
            assert event["turn_context"].get("session_start_revision") == (
                rev if grant == "redacted" else None)
