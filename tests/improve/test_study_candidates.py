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


def test_study_preflight_reserves_skill_evidence_and_linked_native_sessions(tmp_path, capsys):
    from datetime import datetime, timezone

    from agent_usage_tracker.improve.study_independence import reserved_evidence
    from agent_usage_tracker.improve.study_observe import check_baselines

    store = Store(tmp_path / "private")
    scope = str(tmp_path / "scope")
    snapshots = []

    def add(agent, sid, text, links=()):
        events = [
            {"id": "start", "kind": "prompt", "text": text},
            {"id": "end", "kind": "boundary", "text": "turn_complete",
             "turn_context": {"status": "observed", "model": "model", "cwd": scope}},
        ]
        for event in events:
            event.update(timestamp="2026-11-01T09:00:00Z", native_turn_key=sid)
        key = store.put("sessions", {"agent": agent, "session_id": sid, "origin": "interactive",
                                    "events": events, "coverage": {"tool_events_checked": True},
                                    "native_sessions": links})
        snapshots.append(key)
        return key

    discovery = add("codex", "discovery", "discovery prompt")
    control = add("codex", "control", "control prompt")
    supporting = add("codex", "support", "corroborating prompt")
    add("codex", "hatched-copy", "discovery prompt")
    add("t3", "linked-support", "different prompt", [["codex", "support"]])
    first = add("t3", "first", "independent prompt one", [["codex", "shared-native"]])
    alias = add("t3", "alias", "independent prompt two", [["codex", "shared-native"]])
    second = add("codex", "second", "independent prompt three")
    audit = store.put("audits", {"session_ids": snapshots, "coverage": {}})
    discovery_case, control_case, supporting_case = (
        freeze_case(store, audit, key, "end") for key in (discovery, control, supporting))
    proposal = {"scope": scope, "case_id": discovery_case, "target": "skill.md",
                "supporting_cases": [{"case_id": supporting_case}]}
    proposal_id = store.put("proposals", proposal)
    draft = {"proposal_id": proposal_id, "discovery_cases": [discovery_case],
             "control_cases": [control_case]}
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps(draft))
    store_before = {key: store.keys(key) for key in ("cases", "studies", "study_observations")}
    args = ["improve", "--root", str(store.root), "study", "preflight",
            "--scope-file", str(scope_file), "--audit", audit, "--include-excluded"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["exclusions"] == {"reserved_study_evidence": 5}
    assert result["measurable_attempts"] == 3
    assert result["independent_candidates_on_page"] == 2
    assert {item["session_id"] for item in result["items"] if item["measurable"]} == {first, alias, second}
    assert {key: store.keys(key) for key in store_before} == store_before

    unique, duplicate, independent = (freeze_case(store, audit, key, "end")
                                      for key in (first, alias, second))
    reserved = reserved_evidence(store, proposal, [discovery_case], [control_case])
    policy = {"metric": "tool_calls", "minimum_pairs": 2, "require_turn_context": True}
    before = datetime(2026, 11, 2, tzinfo=timezone.utc)
    with pytest.raises(AutError, match="eligible baseline"):
        check_baselines(store, policy | {"baseline_cases": {unique: "task", duplicate: "task"}},
                        proposal, before, reserved)
    check_baselines(store, policy | {"baseline_cases": {unique: "task", independent: "task"}},
                    proposal, before, reserved)


def test_retry_metric_excludes_unknown_or_unverifiable_calls(tmp_path, capsys):
    from agent_usage_tracker.improve.evidence import trace_event

    store = Store(tmp_path / "private")
    stamp = "2026-01-02T12:00:00Z"
    snapshots = []
    for sid in ("retry", "clean", "unknown", "clipped", "missing_fingerprint"):
        def event(kind, key, text, **extra):
            return trace_event("claude_code", sid, kind, key, text, stamp, **extra)

        events = [
            event("prompt", "p", "Verify the change"),
            event("call", "first", "pytest", call="a", tool="Bash"),
            event("result", "r1", "result", call="a", status={
                "clean": "success", "unknown": "unknown",
            }.get(sid, "error")),
            event("call", "second", "pytest", call="b", tool="Bash"),
            event("result", "r2", "result", call="b", status="success"),
            event("boundary", "end", "turn_complete"),
        ]
        if sid == "clipped":
            events[1]["truncated"] = True
        if sid == "missing_fingerprint":
            events[1].pop("source_sha256")
        snapshots.append(store.put("sessions", {
            "agent": "claude_code", "origin": "interactive", "session_id": sid,
            "cwd": str(tmp_path), "model": "test-model",
            "coverage": {"complete": True, "tool_events_checked": True}, "events": events,
        }))
    audit = store.put("audits", {"session_ids": snapshots, "coverage": {}})
    assert main(["evidence", "--root", str(store.root), "--audit", audit,
                 "ordinary-work", "--coverage-basis", "tool_events", "--metric",
                 "identical_tool_retries", "--include-excluded"]) == 0
    report = json.loads(capsys.readouterr().out)
    for key, expected in zip(snapshots, (1, 0, None, None, None)):
        item = next(item for item in report["items"] if item["session_id"] == key)
        assert item["metric_value"] == expected, item
        if expected is None:
            assert item["reason"] == "metric_unknown"
        else:
            case_id = freeze_case(store, audit, key, item["event_id"])
            assert _sample(store, case_id)["identical_tool_retries"] == expected
