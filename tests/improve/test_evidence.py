"""Frozen evidence keeps failures attributable without changing collector state."""

import json

from agent_usage_tracker.improve.evidence import read_session


def write_log(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_claude_matches_results_and_preserves_unknown_calls(tmp_path):
    path = write_log(tmp_path / "session.jsonl", [
        {"type": "user", "sessionId": "session", "cwd": "/work", "message": {
            "content": "Fix the service"}},
        {"type": "assistant", "uuid": "one", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "false"}},
            {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "x"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "failed", "is_error": True}]}},
    ])
    evidence = read_session(path, "claude_code")
    assert evidence["session_id"] == "session"
    assert evidence["coverage"]["calls"] == 2
    assert evidence["coverage"]["matched_results"] == 1
    assert evidence["coverage"]["unmatched_calls"] == 1
    assert evidence["coverage"]["complete"] is False
    assert [e["status"] for e in evidence["events"] if e["kind"] == "result"] == ["error"]


def test_codex_reads_shell_exit_status_and_skips_copied_history(tmp_path):
    path = write_log(tmp_path / "codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "child", "source": "cli",
         "cwd": "/work", "forked_from_id": "parent", "subagent_history_start_ordinal": 3}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "copied",
         "name": "exec_command", "arguments": "{}"}},
        {"type": "response_item", "payload": {"type": "function_call_output",
         "call_id": "copied", "output": "Process exited with code 0"}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "live",
         "name": "exec_command", "arguments": "{\"cmd\":\"false\"}"}},
        {"type": "response_item", "payload": {"type": "function_call_output",
         "call_id": "live", "output": "Process exited with code 2\nOutput:\nmissing config"}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    evidence = read_session(path, "codex")
    assert evidence["coverage"]["calls"] == 1
    assert evidence["coverage"]["matched_results"] == 1
    assert evidence["coverage"]["complete"] is True
    assert [e["status"] for e in evidence["events"] if e["kind"] == "result"] == ["error"]
    assert evidence["coverage"]["copied_records"] == 3


def test_incomplete_and_clipped_evidence_is_reported(tmp_path):
    path = write_log(tmp_path / "session.jsonl", [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "A long natural language reply. " * 400}]}},
    ])
    path.write_text(path.read_text() + '{"unfinished":')
    evidence = read_session(path, "claude_code")
    assert evidence["coverage"]["partial_tail"] is True
    assert evidence["coverage"]["truncated_events"] == 1
    assert evidence["coverage"]["complete"] is False
    assert evidence["events"][0]["truncated"] is True


def test_duplicate_tool_blocks_do_not_inflate_coverage(tmp_path):
    call = {"type": "assistant", "uuid": "one", "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "x"}}]}}
    result = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}}
    path = write_log(tmp_path / "session.jsonl", [call, result, call, result])
    evidence = read_session(path, "claude_code")
    assert evidence["coverage"]["calls"] == 1
    assert evidence["coverage"]["matched_results"] == 1


def test_byte_cap_never_passes_as_complete(tmp_path):
    path = write_log(tmp_path / "session.jsonl", [{"type": "user", "message": {
        "content": "private context"}}] * 20)
    evidence = read_session(path, "claude_code", max_bytes=200)
    assert evidence["coverage"]["byte_cap_reached"] is True
    assert evidence["coverage"]["complete"] is False


def test_desktop_mcp_and_messages_are_observable(tmp_path):
    path = write_log(tmp_path / "desktop.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "source": "vscode"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "id": "p", "content": [{"type": "text", "text": "Read it"}]}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "McpToolCall", "id": "m", "tool": "read", "arguments": {},
            "status": "failed", "error": "unavailable"}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "id": "a", "text": "The tool failed"}}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    evidence = read_session(path, "codex")
    assert [e["kind"] for e in evidence["events"]] == ["prompt", "call", "result", "reply", "boundary"]
    assert evidence["coverage"]["calls"] == 1
    assert evidence["events"][2]["status"] == "error"


def test_recent_window_does_not_include_imported_old_activity(tmp_path):
    path = write_log(tmp_path / "session.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "source": "cli"}},
        {"timestamp": "2020-01-01T00:00:00Z", "type": "response_item", "payload": {
            "type": "function_call", "call_id": "old", "name": "read", "arguments": "{}"}},
        {"timestamp": "2026-11-12T10:00:00Z", "type": "response_item", "payload": {
            "type": "function_call", "call_id": "new", "name": "read", "arguments": "{}"}},
    ])
    evidence = read_session(path, "codex", since_epoch=1_793_836_800)
    assert [e["call_id"] for e in evidence["events"]] == ["new"]
    assert evidence["coverage"]["outside_window"] == 1


def test_opposite_result_status_is_a_conflict_even_when_text_matches(tmp_path):
    path = write_log(tmp_path / "session.jsonl", [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "c",
            "name": "Bash", "input": {"command": "check"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result",
            "tool_use_id": "c", "is_error": False, "content": "same text"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result",
            "tool_use_id": "c", "is_error": True, "content": "same text"}]}},
        {"type": "system", "subtype": "turn_duration"},
    ])
    evidence = read_session(path, "claude_code")
    assert evidence["coverage"]["conflicting_records"] == 1
    assert not evidence["coverage"]["complete"]


def test_codex_compaction_is_visible_to_attempt_review(tmp_path):
    path = write_log(tmp_path / "codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Check it"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "id": "compact", "type": "ContextCompaction"}}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    evidence = read_session(path, "codex")
    assert "compaction" in [e["kind"] for e in evidence["events"]]


def test_timestamp_free_metadata_does_not_hide_complete_claude_attempt(tmp_path):
    path = write_log(tmp_path / "session.jsonl", [
        {"timestamp": "2026-11-12T10:00:00Z", "type": "user", "message": {"content": "Check it"}},
        {"timestamp": "2026-11-12T10:00:01Z", "type": "assistant",
         "message": {"content": [{"type": "text", "text": "Checked"}]}},
        {"timestamp": "2026-11-12T10:00:02Z", "type": "system", "subtype": "turn_duration"},
        {"type": "last-prompt", "timestamp": None},
        {"type": "file-history-snapshot", "timestamp": None},
    ])
    evidence = read_session(path, "claude_code", since_epoch=0)
    assert not evidence["coverage"].get("missing_timestamps")
    assert evidence["coverage"]["complete"]
