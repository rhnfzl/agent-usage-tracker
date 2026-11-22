import json

from agent_usage_tracker.cli import main
from agent_usage_tracker.improve.evidence import trace_event
from agent_usage_tracker.improve.store import Store, _save_session


def trace(session, target="docs.md", retry_target="docs.md", status="success"):
    def event(kind, identity, value, call="", tool="", outcome="unknown"):
        return trace_event("claude_code", session, kind, identity, value, None,
                           call=call, tool=tool, status=outcome)
    return [
        event("prompt", "prompt", "Edit the file"),
        event("call", "first", {"file_path": target, "old_string": "wrong"}, "1", "Edit"),
        event("result", "error", "Missing match", "1", outcome="error"),
        event("call", "retry", {"file_path": retry_target, "old_string": "correct"}, "2", "Edit"),
        event("result", "result", "Done", "2", outcome=status),
        event("boundary", "end", "turn_complete"),
    ]


def test_corrected_arguments_need_same_visible_target_and_attempt():
    from agent_usage_tracker.improve.retries import retries

    events = trace("one")
    assert list(retries(events)) == [(1, 2, 3, 4, "changed_arguments")]
    assert not list(retries(trace("one", retry_target="other.md")))
    assert not list(retries(trace("one", "x" * 40, "y" * 40)))
    assert list(retries(trace("one", "x" * 40, "x" * 40))) == [(1, 2, 3, 4, "changed_arguments")]
    for event in events:
        event.pop("retry_target_sha256", None)
        event.pop("retry_arguments_sha256", None)
    assert list(retries(events)) == [(1, 2, 3, 4, "changed_arguments")]
    assert not list(retries([*events[:3], dict(events[0], id="new"), *events[3:]]))
    assert not list(retries([*events[:3], dict(events[3], run_id="other"), *events[4:]]))


def test_discovery_counts_sessions_and_preserves_references(tmp_path, capsys):
    store = Store(tmp_path / "private")
    keys = []
    for session, revision, status in [("one", 1, "success"), ("one", 2, "success"),
                                      ("two", 1, "error"), ("three", 1, "success")]:
        keys.append(_save_session(store, {"session_id": session, "agent": "claude_code",
                                           "revision": revision, "coverage": {"complete": True},
                                           "events": trace(session, "private/" + "x" * 40, "private/" + "x" * 40, status)}))
    saved = store.get("sessions", keys[0])
    assert saved["retry_keys"]
    assert not any("retry_target_sha256" in e for e in saved["events"])
    audit = store.put("audits", {"session_ids": keys, "supplemental_session_ids": [],
                                 "ambiguous_session_ids": keys[-1:]})
    assert main(["evidence", "--root", str(store.root), "--audit", audit, "--content", "metadata",
                 "retries"]) == 0
    value = json.loads(capsys.readouterr().out)
    group, = value["groups"]
    assert group["distinct_source_sessions"] == 2
    assert group["outcomes"] == {"error": 1, "success": 1}
    assert {item["session_id"] for item in group["examples"]} <= set(keys[:3])
    assert "error_result" in group["examples"][0]
    for private in ("private/", "Missing match", "Edit", "retry_target_sha256"):
        assert private not in json.dumps(value)


def test_retry_discovery_prefers_observed_result_in_later_snapshot(tmp_path):
    from agent_usage_tracker.improve.access import Evidence

    store = Store(tmp_path / "private")
    events = trace("same")
    snapshots = [
        store.put("sessions", {"session_id": "same", "agent": "claude_code",
                               "coverage": {"complete": False}, "events": events[:4]}),
        store.put("sessions", {"session_id": "same", "agent": "claude_code",
                               "coverage": {"complete": True}, "events": events}),
    ]
    for order in (snapshots, snapshots[::-1]):
        audit = store.put("audits", {"session_ids": order})
        group, = Evidence("metadata", store, audit).retries(minimum_sessions=1)["groups"]
        assert group["occurrences"] == 1
        assert group["outcomes"] == {"success": 1}
        assert group["examples"][0]["session_id"] == snapshots[1]

    conflicting = store.put("sessions", {"session_id": "same", "agent": "claude_code",
                                          "coverage": {"complete": True},
                                          "events": trace("same", status="error")})
    for order in ([*snapshots, conflicting], [conflicting, *snapshots]):
        audit = store.put("audits", {"session_ids": order})
        result = Evidence("metadata", store, audit).retries(minimum_sessions=1)
        assert result["groups"] == []
        assert result["conflicting_incidents"] == 1
