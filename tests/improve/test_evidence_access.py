import json
from datetime import datetime, timezone

from agent_usage_tracker.cli import main
from agent_usage_tracker.improve.store import Store


def invoke(capsys, root, *args):
    assert main(["evidence", "--root", str(root), *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_capture_search_content_modes_and_bundle(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr("agent_usage_tracker.improve.store.codex.session_files", lambda _: [])
    monkeypatch.setattr("agent_usage_tracker.improve.store.codex.imported_until", lambda _: {})
    monkeypatch.setattr("agent_usage_tracker.improve.store.pi.session_files", lambda _: [])
    monkeypatch.setattr("agent_usage_tracker.improve.store.cursor_cli.chat_stores", lambda _: [])
    monkeypatch.setattr("agent_usage_tracker.improve.store.t3.snapshots", lambda *a, **kw: ([], {}))
    root = tmp_path / "private"
    log = home / ".claude" / "projects" / "demo" / "run.jsonl"
    log.parent.mkdir(parents=True)
    stamp = datetime.now(timezone.utc).isoformat()
    text = "A failed setup password=fixture-only " + "x" * 6100
    row = {"timestamp": stamp, "type": "user", "sessionId": "demo", "entrypoint": "cli",
           "uuid": "user", "cwd": str(tmp_path), "message": {"role": "user", "content": text}}
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr("agent_usage_tracker.improve.store.claude_code.main_session_files",
                        lambda _: [str(log)])

    report = invoke(capsys, root, "capture", "--idle-s", "0")
    audit_id = report["audit_id"]
    scope = ("--audit", audit_id)
    hits = invoke(capsys, root, *scope, "search", "--query", "fixture-only")
    hit = hits["items"][0]
    session_id, event_id = hit["session_id"], hit["event_id"]
    store = Store(root, read_only=True)
    assert "fixture-only" not in json.dumps(store.get("sessions", session_id))
    result = invoke(capsys, root, *scope, "event", session_id, event_id, "--limit", "8000")
    assert result["text"] == text
    result = invoke(capsys, root, *scope, "--content", "redacted", "event", session_id, event_id)
    assert "fixture-only" not in result["text"]
    result = invoke(capsys, root, *scope, "--content", "metadata", "session", session_id)
    assert "text" not in result["items"][0]
    assert "fixture-only" not in json.dumps(result)

    bundle = tmp_path / "selected.json"
    invoke(capsys, root, *scope, "--content", "redacted", "export", str(bundle), session_id)
    assert "fixture-only" not in bundle.read_text()
    result = invoke(capsys, root, "--bundle", str(bundle), "event", session_id, event_id)
    assert result["content"] == "redacted"
    assert "[redacted]" in result["text"]
    assert main(["evidence", "--root", str(root), *scope, "session", "0" * 64]) == 1


def test_larger_capture_budget_recovers_a_clipped_prompt_under_a_new_scope(tmp_path, monkeypatch, capsys):
    from agent_usage_tracker.improve import store as evidence_store

    monkeypatch.setattr(evidence_store.codex, "imported_until", lambda _: {})
    monkeypatch.setattr(evidence_store.claude_code, "main_session_files", lambda _: [])
    monkeypatch.setattr(evidence_store.pi, "session_files", lambda _: [])
    monkeypatch.setattr(evidence_store.cursor_cli, "chat_stores", lambda _: [])
    monkeypatch.setattr(evidence_store.t3, "snapshots", lambda *a, **kw: ([], {}))
    log = tmp_path / "rollout.jsonl"
    monkeypatch.setattr(evidence_store.codex, "session_files", lambda _: [str(log)])
    stamp = datetime.now(timezone.utc).isoformat()
    records = [
        ("session_meta", {"id": "source", "source": "cli", "cwd": str(tmp_path)}),
        ("event_msg", {"type": "task_started", "turn_id": "real-turn"}),
        ("turn_context", {"turn_id": "real-turn", "model": "model", "cwd": str(tmp_path)}),
        ("event_msg", {"type": "user_message", "message": "Recover the opening prompt"}),
        ("fixture_padding", {"padding": "x" * (1024 * 1024 + 1024)}),
        ("event_msg", {"type": "agent_message", "message": "Finished"}),
        ("event_msg", {"type": "task_complete", "turn_id": "real-turn"}),
    ]
    log.write_text("".join(json.dumps({"type": kind, "timestamp": stamp, "payload": payload}) + "\n"
                           for kind, payload in records), encoding="utf-8")
    root = tmp_path / "private"
    capture = ("--content", "redacted", "capture", "--idle-s", "0", "--max-mib")
    assert main(["evidence", "--root", str(root), *capture, "257"]) == 1
    assert not root.exists()
    capsys.readouterr()
    clipped = invoke(capsys, root, *capture, "1")["audit_id"]
    wider = invoke(capsys, root, *capture, "2")["audit_id"]
    store = Store(root, read_only=True)
    assert store.get("audits", wider)["source_policy"]["max_bytes"] == 2 * 1024 * 1024
    assert invoke(capsys, root, "--audit", clipped, "search", "--query", "opening prompt")["items"] == []
    recovered = invoke(capsys, root, "--audit", wider, "search", "--query", "opening prompt")
    assert len(recovered["items"]) == 1
    assert recovered["items"][0]["kind"] == "prompt"
    original_session = store.get("audits", clipped)["session_ids"][0]
    recovered_session = recovered["items"][0]["session_id"]
    assert recovered_session != original_session
    assert store.get("sessions", recovered_session)["session_id"] == store.get("sessions", original_session)["session_id"]
    assert store.keys("session_content") == []
    assert invoke(capsys, root, "--audit", clipped, "--content", "metadata", "describe")["coverage"]["byte_cap_reached"] == 1


def test_compare_uses_observed_events_and_audit_scope(tmp_path, capsys):
    store = Store(tmp_path / "private")

    def snapshot(name, events):
        return store.put("sessions", {"agent": "pi", "session_id": name, "model": "test",
                                      "events": events, "coverage": {"complete": False}})

    first = snapshot("first", [{"id": "a", "kind": "result", "status": "error", "text": "failed"}])
    second = snapshot("second", [{"id": "b", "kind": "result", "status": "completed", "text": "ok"}])
    third = snapshot("unshared", [])
    audit_id = store.put("audits", {"session_ids": [first, second], "coverage": {}})
    scope = ("--audit", audit_id)
    result = invoke(capsys, store.root, *scope, "compare", first, second)
    assert result["delta"]["errors"] == -1
    assert not result["baseline"]["coverage"]["complete"]
    assert main(["evidence", "--root", str(store.root), *scope, "compare", first, third]) == 1


def test_freeze_selected_attempt_and_compare_without_a_judge(tmp_path, capsys):
    store = Store(tmp_path / "private")
    events = [
        {"id": "p1", "kind": "prompt", "text": "Set up this repo"},
        {"id": "call1", "kind": "call", "call_id": "1", "tool": "Shell", "text": "uv venv"},
        {"id": "r1", "kind": "result", "call_id": "1", "status": "error", "text": "cache denied"},
        {"id": "end1", "kind": "boundary", "text": "turn_complete"},
        {"id": "p2", "kind": "prompt", "text": "Try with a writable path"},
        {"id": "call2", "kind": "call", "call_id": "2", "tool": "Shell", "text": "uv venv"},
        {"id": "r2", "kind": "result", "call_id": "2", "status": "success", "text": "ready"},
        {"id": "end2", "kind": "boundary", "text": "turn_complete"},
    ]
    session_id = store.put("sessions", {"agent": "pi", "origin": "human", "session_id": "source",
        "events": events, "coverage": {"complete": True}})
    audit_id = store.put("audits", {"session_ids": [session_id], "coverage": {}})
    scope = ("--audit", audit_id)
    first = invoke(capsys, store.root, *scope, "freeze-case", session_id, "r1")
    second = invoke(capsys, store.root, *scope, "freeze-case", session_id, "r2")
    assert first["event_ids"] == ["p1", "call1", "r1", "end1"]
    assert second["event_ids"] == ["p2", "call2", "r2", "end2"]
    assert first["trigger"] == "error_result"
    assert invoke(capsys, store.root, *scope, "case", first["case_id"]) == first

    result = invoke(capsys, store.root, *scope, "compare",
                    first["case_id"], second["case_id"], "--cases")
    assert result["delta"]["errors"] == -1
    assert result["first_tool_difference"]["baseline"]["result_event_id"] == "r1"
    assert result["first_tool_difference"]["candidate"]["result_event_id"] == "r2"


def test_selected_evidence_can_drive_a_prospective_repair(tmp_path, capsys):
    import sys

    from agent_usage_tracker.improve.proposals import file_digest, prepare
    from agent_usage_tracker.improve.studies import assess, create, run

    store = Store(tmp_path / "private")
    sessions = []
    for name, outcome in (("discovery", "error"), ("control", "success")):
        sessions.append(store.put("sessions", {
            "agent": "pi", "origin": "human", "session_id": name, "coverage": {"complete": True},
            "events": [{"id": name, "kind": "result", "status": outcome, "text": name}],
        }))
    audit_id = store.put("audits", {"session_ids": sessions, "coverage": {}})
    discovery, control = [
        invoke(capsys, store.root, "--audit", audit_id, "freeze-case", key, name)["case_id"]
        for key, name in zip(sessions, ("discovery", "control"))
    ]

    roots = {key: tmp_path / key for key in ("source", "baseline", "candidate")}
    for key, path in roots.items():
        path.mkdir()
        (path / "setup").write_text("writable" if key == "candidate" else "denied")
    target = roots["source"] / "setup"
    proposal_id = prepare(store, discovery, roots["source"], target, "denied", "writable")
    acceptance = tmp_path / "check.py"
    acceptance.write_text("from pathlib import Path\n"
                          "assert Path('setup').read_text() == 'writable'\n")
    conditions = ("baseline", "candidate")
    trial_id = store.put("trial_specs", {
        "version": 1, "kind": "command_check", "repeats": 1,
        "directories": {key: str(roots[key]) for key in conditions},
        "files": {key: {"setup": file_digest(roots[key] / "setup")} for key in conditions},
        "cases": [{"id": "setup", "argv": [sys.executable, str(acceptance)], "timeout_s": 5,
                   "acceptance_files": {str(acceptance): file_digest(acceptance)}}],
    })
    study_id = create(store, {
        "version": 1, "discovery_cases": [discovery], "control_cases": [control],
        "task_families": {discovery: "setup", control: "clean setup"},
        "proposal_id": proposal_id, "trial_spec_id": trial_id,
        "primary_metric": "accepted", "quality_constraints": ["setup must pass"],
        "budget_s": 30, "decision_policy": {"direction": "increase", "minimum_improvement": 0,
            "require_no_acceptance_regressions": True, "require_all_candidate_accepted": True},
        "provenance": {"setup": None, "model": None, "tools": None, "instructions": None},
        "reference_result_ids": [], "cost_inputs": {"operator_minutes": None, "money": None},
    })["study_id"]
    assert run(store, study_id, execute=True)["trial"]["accepted"] == {"baseline": 0, "candidate": 1}
    assert not assess(store, study_id)["improvement_established"]
    assert target.read_text() == "denied"


def test_bundle_retains_linked_call_outcomes_at_each_content_level(tmp_path, capsys):
    store = Store(tmp_path / "private")
    keys = []
    for n, status in enumerate(("error", "success")):
        call_id = str(n) * 64
        keys.append(store.put("sessions", {"agent": "codex", "coverage": {"complete": True},
            "events": [
                {"id": f"call{n}", "kind": "call", "call_id": call_id, "tool": "Shell"},
                {"id": f"result{n}", "kind": "result", "call_id": call_id, "status": status},
            ]}))
    audit_id = store.put("audits", {"session_ids": keys, "coverage": {}})
    for mode in ("metadata", "redacted", "full"):
        bundle = tmp_path / f"{mode}.json"
        scope = ("--audit", audit_id, "--content", mode)
        before = invoke(capsys, store.root, *scope, "compare", *keys)
        invoke(capsys, store.root, *scope, "export", str(bundle), *keys)
        after = invoke(capsys, store.root, "--bundle", str(bundle), "compare", *keys)
        assert after["first_tool_difference"] == before["first_tool_difference"]
        assert after["first_tool_difference"]["baseline"]["result_event_id"] == "result0"


def test_project_audit_excludes_mixed_workspace_history(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import store as module

    home = tmp_path / "home"
    home.mkdir()
    wanted = tmp_path / "wanted"
    unrelated = tmp_path / "unrelated"
    log = home / "mixed.jsonl"
    stamp = datetime.now(timezone.utc).isoformat()
    log.write_text("".join(json.dumps({
        "timestamp": stamp, "type": "user", "sessionId": "mixed", "entrypoint": "cli",
        "uuid": str(n), "cwd": str(cwd), "message": {"role": "user", "content": str(n)},
    }) + "\n" for n, cwd in enumerate((unrelated, wanted))), encoding="utf-8")
    monkeypatch.setattr(module, "repository", lambda path: str(path))
    monkeypatch.setattr(module.claude_code, "main_session_files", lambda _: [log])
    monkeypatch.setattr(module.codex, "session_files", lambda _: [])
    monkeypatch.setattr(module.codex, "imported_until", lambda _: {})
    monkeypatch.setattr(module.pi, "session_files", lambda _: [])
    monkeypatch.setattr(module.cursor_cli, "chat_stores", lambda _: [])
    monkeypatch.setattr(module.t3, "snapshots", lambda *a, **kw: ([], {}))

    store = Store(tmp_path / "private")
    report = module.audit(store, home, project=wanted, idle_s=0, capture_full=True)
    assert report["sessions"] == 0
    assert report["other_projects"] == 1
    assert not store.keys("session_content")


def test_rejects_malformed_bundle_metadata_even_with_valid_checksum(tmp_path, capsys):
    from agent_usage_tracker.improve.store import digest

    bundle = tmp_path / "forged.json"
    key = "a" * 64
    payload = {
        "version": 1, "audit_id": "b" * 64, "content": "metadata", "coverage": {},
        "sessions": {key: {"id": key, "agent": "codex", "coverage": {},
                          "related_sessions": [], "links": {},
                          "events": [{"id": "a", "kind": "prompt",
                                      "timestamp": {"text": "private text"}}]}},
    }
    bundle.write_text(json.dumps({"evidence": payload, "sha256": digest(payload)}), encoding="utf-8")
    assert main(["evidence", "--bundle", str(bundle), "session", key]) == 1
    assert "bundle is unreadable, too large, or invalid" in capsys.readouterr().err


def test_related_snapshots_are_readable_but_excluded_from_search(tmp_path, capsys):
    store = Store(tmp_path / "private")
    mirror = store.put("sessions", {"agent": "codex", "events": [
        {"id": "supplement", "kind": "prompt", "text": "Mirror-only context"}], "coverage": {}})
    primary = store.put("sessions", {"agent": "t3", "events": [
        {"id": "primary", "kind": "prompt", "text": "Primary context"}], "coverage": {},
        "supplemental_sources": [mirror]})
    audit_id = store.put("audits", {"session_ids": [primary], "supplemental_session_ids": [mirror],
                                    "coverage": {}})
    scope = ("--audit", audit_id)
    session = invoke(capsys, store.root, *scope, "session", primary)
    assert session["session"]["related_sessions"] == [mirror]
    event = invoke(capsys, store.root, *scope, "event", mirror, "supplement")
    assert event["text"] == "Mirror-only context"
    assert [item["event_id"] for item in invoke(capsys, store.root, *scope, "search")["items"]] == [
        "primary"]


def test_restored_snapshot_without_full_text_stays_readable(tmp_path, capsys):
    store = Store(tmp_path / "restored")
    key = store.put("sessions", {"agent": "codex", "coverage": {"complete": False},
        "full_content_id": "a" * 64, "events": [
            {"id": "event", "kind": "prompt", "text": "password=[redacted]"}]})
    audit_id = store.put("audits", {"session_ids": [key], "coverage": {}})
    event = invoke(capsys, store.root, "--audit", audit_id, "event", key, "event")
    assert event["text"] == "password=[redacted]"
    assert event["text_source"] == "snapshot_redacted"
