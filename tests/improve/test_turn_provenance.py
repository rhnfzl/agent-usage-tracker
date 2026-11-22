import json

from agent_usage_tracker.improve.evidence import read_session


def test_codex_provenance_cites_the_completed_turn_and_rejects_mixed_context(tmp_path):
    path = tmp_path / "rollout.jsonl"

    def record(kind, **payload):
        return {"type": kind, "timestamp": "2026-11-02T09:00:00Z", "payload": payload}

    rows = [record("session_meta", id="session", cwd=str(tmp_path))]
    for turn_id, models in (("one", ["first-model"]), ("two", ["second-model", "third-model"]),
                            ("three", []), ("four", ["first-model", None])):
        rows.append(record("event_msg", type="task_started", turn_id=turn_id))
        for model in models:
            rows.append(record("turn_context", turn_id=turn_id,
                               cwd=str(tmp_path) if model else None, model=model))
        rows.extend([
            record("event_msg", type="user_message", message="Check " + turn_id),
            record("event_msg", type="agent_message", message="Done " + turn_id),
            record("event_msg", type="task_complete", turn_id=turn_id),
        ])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    session = read_session(path, "codex")
    boundaries = [event for event in session["events"] if event["kind"] == "boundary"]
    assert boundaries[0]["turn_context"] == {
        "status": "observed", "model": "first-model", "cwd": str(tmp_path),
        "record": {"line": 3, "byte_offset": None},
    }
    assert boundaries[1]["turn_context"] == {"status": "ambiguous"}
    assert boundaries[2]["turn_context"] == {"status": "missing"}
    assert boundaries[3]["turn_context"] == {"status": "ambiguous"}
    assert session["model"] == "first-model"


def test_turn_context_access_respects_the_grant_in_an_exported_bundle(tmp_path):
    from agent_usage_tracker.improve.access import Evidence
    from agent_usage_tracker.improve.store import Store

    store = Store(tmp_path / "private")
    session = store.put("sessions", {"agent": "codex", "coverage": {}, "events": [{
        "id": "boundary", "kind": "boundary", "text": "turn_complete",
        "turn_context": {"status": "observed", "model": "model",
                         "cwd": "/private/password=fixture-value",
                         "record": {"line": 2, "byte_offset": None}},
    }]})
    audit = store.put("audits", {"session_ids": [session], "coverage": {}})
    redacted = Evidence("redacted", store, audit)
    event = redacted.event(session, "boundary")
    assert event["turn_context"]["cwd"] == "/private/password=[redacted]"
    assert event["turn_context"]["record"]["line"] == 2

    bundle = tmp_path / "bundle.json"
    redacted.export(bundle, [session])
    metadata = Evidence("metadata", bundle=bundle)
    assert metadata.event(session, "boundary")["turn_context"] == {
        "status": "observed", "record": {"line": 2, "byte_offset": None},
    }


def test_context_record_location_does_not_make_copied_work_a_distinct_study_case(tmp_path):
    from agent_usage_tracker.improve.store import Store
    from agent_usage_tracker.improve.studies import identity

    store = Store(tmp_path / "private")
    cases = []
    for line in (1, 4):
        events = [{"id": "boundary", "kind": "boundary", "text": "turn_complete",
                   "turn_context": {"status": "observed", "model": "model",
                                    "cwd": str(tmp_path), "record": {"line": line}}}]
        session = store.put("sessions", {"agent": "codex", "session_id": str(line)})
        key = store.put("cases", {"session_snapshot": session, "events": events, "coverage": {},
                                  "omitted_task_events": 0})
        cases.append(identity(store, key))
    assert cases[0]["content"] == cases[1]["content"]


def test_missing_turn_boundary_cannot_borrow_the_next_turns_context(tmp_path):
    from agent_usage_tracker.improve.judge import freeze_case
    from agent_usage_tracker.improve.store import Store
    from agent_usage_tracker.improve.study_observe import _sample

    path = tmp_path / "rollout.jsonl"

    def row(kind, **payload):
        return {"type": kind, "timestamp": "2026-11-02T09:00:00Z", "payload": payload}

    rows = [
        row("session_meta", id="session", cwd=str(tmp_path)),
        row("event_msg", type="task_started", turn_id="first"),
        row("event_msg", type="user_message", message="Run the setup tests"),
        row("event_msg", type="task_started", turn_id="next"),
        row("turn_context", turn_id="next", cwd=str(tmp_path), model="next-model"),
        row("event_msg", type="agent_message", message="Done"),
        row("event_msg", type="task_complete", turn_id="next"),
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
    session = read_session(path, "codex")
    store = Store(tmp_path / "private")
    key = store.put("sessions", session)
    audit = store.put("audits", {"session_ids": [key], "coverage": {}})
    case = freeze_case(store, audit, key, session["events"][0]["id"])
    assert _sample(store, case)["context_status"] == "ambiguous"
    assert not store.get("cases", case)["coverage"]["complete"]
