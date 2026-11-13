"""Frozen evidence keeps failures attributable without changing collector state."""

import json

import pytest

from agent_usage_tracker.improve.evidence import read_session


def test_audit_can_skip_structurally_automated_codex_before_reading_body(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import evidence

    path = write_log(tmp_path / "automated.jsonl", [
        {"type": "session_meta", "payload": {"id": "automated", "source": "exec"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "private body"}},
    ])
    original = evidence.redact
    seen = []

    def track(text):
        seen.append(text)
        return original(text)

    monkeypatch.setattr(evidence, "redact", track)
    assert read_session(path, "codex", exclude_automated=True) is None
    assert seen == []
    assert read_session(path, "codex")["origin"] == "automated"
    assert seen == ["private body"]


@pytest.mark.parametrize("source", ["cli", "vscode", "future-client", {"future": "client"}])
def test_audit_keeps_interactive_and_unknown_codex_sources(tmp_path, source):
    path = write_log(tmp_path / "kept.jsonl", [
        {"type": "session_meta", "payload": {"id": "kept", "source": source}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Keep this"}},
    ])
    assert read_session(path, "codex", exclude_automated=True) == read_session(path, "codex")


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



def test_bounded_recent_window_keeps_latest_complete_attempt(tmp_path):
    path = write_log(tmp_path / "long.jsonl", [
        {"type": "session_meta", "payload": {"id": "recent", "source": "cli", "cwd": "/work"}},
        {"type": "turn_context", "payload": {"model": "old-model"}},
        *[{"type": "padding", "text": "x" * 500} for _ in range(20)],
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Check latest"}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "latest",
         "name": "exec_command", "arguments": "false"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "latest",
         "output": "Process exited with code 1"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Failed"}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    evidence = read_session(path, "codex", max_bytes=4096, recent=True)
    assert [e["call_id"] for e in evidence["events"] if e["kind"] == "call"] == ["latest"]
    assert evidence["coverage"]["matched_results"] == 1
    assert evidence["coverage"]["prefix_omitted"]
    assert not evidence["coverage"]["complete"]
    assert evidence["model"] is None
    assert evidence["cwd"] == "/work"
    assert evidence["source"]["bytes"] <= 4096
    assert len(evidence["source"]["ranges"]) == 2


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_short_recent_session_equals_prefix(tmp_path, agent):
    path = write_log(tmp_path / "short.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "source": "cli"}},
        {"type": "user", "sessionId": "s", "message": {"content": "hello"}},
    ])
    assert read_session(path, agent, recent=True) == read_session(path, agent)


@pytest.mark.parametrize("ordinal", [None, 3])
def test_recent_fork_without_known_tail_ordinal_excludes_copied_history(tmp_path, ordinal):
    path = write_log(tmp_path / "fork.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "source": "cli",
         "forked_from_id": "parent", "subagent_history_start_ordinal": ordinal}},
        *[{"type": "padding", "text": "x" * 300} for _ in range(20)],
        {"type": "event_msg", "payload": {"type": "user_message", "message": "copied?"}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    evidence = read_session(path, "codex", max_bytes=1024, recent=True)
    assert evidence["events"] == []
    assert evidence["coverage"]["unknown_copy_boundary"]
    assert not evidence["coverage"]["complete"]


def test_recent_oversized_header_does_not_guess_metadata(tmp_path):
    path = write_log(tmp_path / "huge.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "cwd": "x" * 2000}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "later"}},
    ])
    evidence = read_session(path, "codex", max_bytes=512, recent=True)
    assert evidence["events"] == []
    assert evidence["coverage"]["partial_tail"]
    assert evidence["source"]["bytes"] == 512


def test_recent_claude_partial_tail_and_large_line_preserve_only_complete_records(tmp_path):
    path = write_log(tmp_path / "claude.jsonl", [
        {"type": "metadata", "sessionId": "s", "cwd": "/work", "entrypoint": "cli"},
        {"type": "user", "message": {"content": "x" * 5000}},
        {"type": "user", "message": {"content": "recent prompt"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "reply"}]}},
        {"type": "system", "subtype": "turn_duration"},
    ])
    path.write_text(path.read_text() + '{"partial":')
    evidence = read_session(path, "claude_code", max_bytes=1024, recent=True)
    assert [e["text"] for e in evidence["events"]] == ["recent prompt", "reply", "turn_complete"]
    assert evidence["coverage"]["partial_tail"]
    assert evidence["coverage"]["prefix_omitted"]
    assert evidence["source"]["bytes"] <= 1024
    assert evidence["cwd"] == "/work"


def test_recent_claude_header_does_not_supply_old_goal_or_model(tmp_path):
    path = write_log(tmp_path / "claude.jsonl", [
        {"type": "assistant", "sessionId": "s", "message": {"model": "old-model",
         "content": [{"type": "text", "text": "old goal"}]}},
        {"type": "user", "message": {"content": "x" * 5000}},
        {"type": "user", "message": {"content": "recent"}},
    ])
    evidence = read_session(path, "claude_code", max_bytes=1024, recent=True)
    assert evidence["model"] is None
    assert [e["text"] for e in evidence["events"]] == ["recent"]



def test_recent_short_source_does_not_read_concurrent_append(tmp_path):
    from agent_usage_tracker.improve.evidence import _recent_records

    path = write_log(tmp_path / "growing.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "source": "cli"}},
    ])
    records = _recent_records(path, 1024)
    assert next(records)[0] == 1
    with path.open("ab") as stream:
        stream.write(b"x" * 1_000_000 + b"\n")
    assert list(records) == []


def test_recent_window_uses_size_from_opened_source(tmp_path, monkeypatch):
    path = write_log(tmp_path / "growing.jsonl", [
        {"type": "session_meta", "payload": {"id": "s", "source": "cli"}},
    ])
    original_open = type(path).open
    appended = False

    def open_after_append(self, mode="r", *args, **kwargs):
        nonlocal appended
        if self == path and mode == "rb" and not appended:
            appended = True
            with original_open(path, "ab") as stream:
                padding = {"type": "padding", "text": "x" * 500}
                boundary = {"type": "event_msg", "payload": {"type": "task_complete"}}
                stream.write(((json.dumps(padding) + "\n") * 20
                              + json.dumps(boundary) + "\n").encode())
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", open_after_append)
    evidence = read_session(path, "codex", max_bytes=1024, recent=True)
    assert evidence["coverage"]["prefix_omitted"]
    assert evidence["coverage"]["byte_cap_reached"]
    assert not evidence["coverage"]["complete"]
    assert evidence["source"]["size"] == path.stat().st_size
    assert len(evidence["source"]["ranges"]) == 2
