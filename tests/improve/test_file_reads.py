import hashlib

import pytest

from agent_usage_tracker.improve.file_reads import capture_file_read, observed_file_read, path_fingerprint


@pytest.mark.parametrize("command", [
    ["cat", "--", ".agents/folder x/SKILL.md"],
    ["/bin/zsh", "-lc", "cat -- '.agents/folder x/SKILL.md'"],
    "/bin/bash -lc \"cat -- '.agents/folder x/SKILL.md'\"",
])
def test_capture_exact_file_output(tmp_path, command):
    receipt = capture_file_read({"command": command, "cwd": tmp_path.as_uri(), "exit_code": 0,
                                 "aggregated_output": "verified bytes\r\n", "status": "completed"})
    assert receipt == {"path_sha256": path_fingerprint(tmp_path / ".agents/folder x/SKILL.md"),
                       "output_sha256": hashlib.sha256(b"verified bytes\r\n").hexdigest()}


@pytest.mark.parametrize("command", [
    "cat a | head", "sed -n '1,40p' a", "cat a; cat b", "cat -- \"$SKILL\"",
    "cat -- foo/../a", "cat -- /tmp/*", "cat ./a ./b", "cat\na", "cat -- ~/a",
])
def test_capture_rejects_ambiguous_or_partial_commands(tmp_path, command):
    assert capture_file_read({"command": command, "cwd": str(tmp_path), "exit_code": 0,
                              "aggregated_output": "x"}) is None


def test_matching_read_is_bound_to_one_turn(tmp_path):
    path = tmp_path / "SKILL.md"
    receipt = capture_file_read({"command": "cat SKILL.md", "cwd": str(tmp_path),
                                 "exit_code": 0, "aggregated_output": "x"})
    events = [
        {"id": "call", "kind": "call", "call_id": "id", "native_turn_key": "earlier",
         "tool_representation": "execution_item"},
        {"id": "result", "kind": "result", "call_id": "id", "native_turn_key": "earlier",
         "native_file_read": receipt, "status": "success"},
    ]
    for turn in ("earlier", "later"):
        events.append({"id": turn + "-end", "kind": "boundary", "text": "turn_complete",
                       "native_turn_key": turn, "turn_context": {"status": "observed"}})
    source = {"agent": "codex", "origin": "interactive", "coverage": {}, "events": events}
    expected = hashlib.sha256(b"x").hexdigest()
    assert observed_file_read(source, "later-end", "later", path, expected) is None
    assert observed_file_read(source, "earlier-end", "earlier", path, "0" * 64) is None
    assert observed_file_read(source, "earlier-end", "earlier", path, expected) == {
        "call_event_id": "call", "result_event_id": "result",
    }
