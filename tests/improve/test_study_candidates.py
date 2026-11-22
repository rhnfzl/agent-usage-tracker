import json

import pytest

from agent_usage_tracker.cli import main
from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.access import Evidence
from agent_usage_tracker.improve.judge import freeze_case
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.improve.study_observe import _sample


def test_ordinary_work_preserves_grants_and_freezes_the_previewed_attempt(tmp_path, capsys):
    store = Store(tmp_path / "private")
    stamp = "2026-11-02T10:00:00Z"
    scope = str(tmp_path / "private-project-name")
    snapshots = []
    for index in range(3):
        turn = str(index)
        events = [
            {"kind": "prompt", "id": "prompt", "text": "password=private-prompt"},
            {"kind": "call", "id": "call", "call_id": "tool", "text": "check"},
            {"kind": "result", "id": "result", "call_id": "tool", "status": "error",
             "text": "clipped" if index == 2 else "error", "truncated": index == 2},
            {"kind": "boundary", "id": "end", "text": "turn_complete",
             "turn_context": {"status": "observed", "model": "private-model", "cwd": scope}},
        ]
        for event in events:
            event.update(timestamp=stamp, native_turn_key=turn)
        snapshots.append(store.put("sessions", {
            "agent": "codex", "origin": "interactive", "session_id": "shared" if index < 2 else "third",
            "events": events, "coverage": {"tool_events_checked": True},
        }))
    audit_id = store.put("audits", {"session_ids": snapshots, "coverage": {}})
    metadata = Evidence("metadata", store=store, audit_id=audit_id)
    assert store.keys("cases") == []
    argv = ["evidence", "--root", str(store.root), "--audit", audit_id, "--content",
            "metadata", "ordinary-work", "--metric", "tool_errors", "--include-excluded"]
    assert main(argv) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert "private-project-name" not in output and "private-model" not in output
    assert "private-prompt" not in output
    assert report["scanned_attempts"] == 3
    assert report["measurable_attempts"] == 2
    assert report["distinct_measurable_sessions"] == 1
    assert report["exclusions"] == {"measurement_coverage_incomplete": 1}

    page = metadata.ordinary_work("tool_errors", include_excluded=True, limit=1)
    second = metadata.ordinary_work("tool_errors", include_excluded=True, cursor=page["next_cursor"])
    assert page["items"] + second["items"] == report["items"]
    with pytest.raises(AutError, match="cursor"):
        metadata.ordinary_work("tool_calls", include_excluded=True, cursor=page["next_cursor"])
    assert store.keys("cases") == []

    redacted = Evidence("redacted", store=store, audit_id=audit_id)
    selected = redacted.ordinary_work("tool_errors", "tool_events")
    assert selected["measurable_attempts"] == 3
    assert selected["distinct_measurable_sessions"] == 2
    assert selected["items"][0]["model"] == "private-model"
    for item in selected["items"]:
        case = freeze_case(store, audit_id, item["session_id"], item["event_id"])
        sample = _sample(store, case)
        assert sample["tool_events_complete"]
        assert sample["tool_errors"] == item["metric_value"]
    outside = metadata.ordinary_work("tool_errors", "tool_events", before=stamp)
    assert outside["exclusions"] == {"after_cutoff": 3}
