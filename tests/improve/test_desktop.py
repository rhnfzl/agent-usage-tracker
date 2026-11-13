import json

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.desktop import import_desktop
from agent_usage_tracker.improve.store import Store, digest

MAPPING = dict(task_id="task", session_id="aut", parent_call_id="parent", desktop_session_id="desk")


def event(seq, name, **fields):
    return dict(
        event=name,
        seq=seq,
        ts_ms=1000 + seq,
        writer_pid=7,
        writer_proc_start_ms=900,
        session_id="desk",
        **fields,
    )


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_spans_duplicates_privacy_and_uncertainty(tmp_path):
    trace = tmp_path / "trace"
    trace.mkdir()
    rows = [
        event(0, "trace.meta", schema=1),
        event(1, "command.start", command="click", arguments="SECRET"),
        event(2, "command.start", command="snapshot"),
        event(3, "command.end", command="snapshot", ok=True, duration_ms=1),
        event(4, "command.end", command="click", ok=False, duration_ms=3, code="PERM_DENIED"),
    ]
    write(trace / "one.jsonl", rows)
    write(trace / "copy.jsonl", rows)
    response = write(
        tmp_path / "responses.jsonl",
        [
            dict(
                writer_pid=7,
                writer_proc_start_ms=900,
                command_seq=1,
                response=dict(
                    ok=False,
                    command="click",
                    error=dict(
                        code="PERM_DENIED",
                        message="SECRET",
                        disposition=dict(delivery="delivery_uncertain", retry="safe"),
                    ),
                ),
            )
        ],
    )
    store = Store(tmp_path / "store")
    result = import_desktop(store, trace, response, MAPPING)
    assert result["coverage"]["duplicate_events"] == 5
    assert len(result["commands"]) == 2
    click = next(row for row in result["commands"] if row["command_hash"] == digest("click"))
    assert click["delivery"] == "delivery_uncertain"
    assert click["disposition_conflict"] is True
    assert click["retry"] == "safe"
    assert (
        next(row for row in result["commands"] if row["command_hash"] == digest("snapshot"))[
            "delivery"
        ]
        == "unknown"
    )
    assert "SECRET" not in json.dumps(result)
    assert result["mapping"]["parent_call_id"] == digest("parent")
    assert result["task_acceptance"] == "unknown"
    assert import_desktop(store, trace, response, MAPPING)["commands"] == result["commands"]


def test_conflicts_rejected_before_persistence(tmp_path):
    trace = tmp_path / "trace"
    trace.mkdir()
    write(trace / "a.jsonl", [event(0, "trace.meta", schema=1)])
    write(trace / "b.jsonl", [event(0, "trace.meta", schema=2)])
    store = Store(tmp_path / "store")
    with pytest.raises(AutError, match="conflict"):
        import_desktop(store, trace, None, MAPPING)
    assert not store.keys("desktop_imports")


def test_incomplete_and_orphan_response(tmp_path):
    trace = write(
        tmp_path / "trace.jsonl",
        [
            event(2, "command.start", command="click"),
            event(4, "command.end", command="type", ok=True),
        ],
    )
    with trace.open("a") as out:
        out.write("{")
    response = write(
        tmp_path / "response.jsonl",
        [
            dict(
                writer_pid=99,
                writer_proc_start_ms=1,
                command_seq=1,
                response=dict(ok=False, command="permissions", error=dict(code="PERM_DENIED")),
            )
        ],
    )
    result = import_desktop(Store(tmp_path / "store"), trace, response, MAPPING)
    assert not result["coverage"]["complete"]
    assert result["coverage"]["orphan_responses"] == 1
    assert all(row["delivery"] == "unknown" for row in result["commands"])
    assert result["coverage"]["unpaired_starts"] == 1
    assert result["coverage"]["unpaired_ends"] == 1


def test_explicit_mapping_and_session(tmp_path):
    trace = write(tmp_path / "trace.jsonl", [event(0, "trace.meta", schema=1)])
    store = Store(tmp_path / "store")
    with pytest.raises(AutError):
        import_desktop(store, trace, None, {})
    with pytest.raises(AutError, match="session"):
        import_desktop(store, trace, None, {**MAPPING, "desktop_session_id": "other"})


def test_missing_trace_permission_response_never_fabricates_span(tmp_path):
    response = write(
        tmp_path / "response.jsonl",
        [
            dict(
                writer_pid=99,
                writer_proc_start_ms=1,
                command_seq=1,
                response=dict(
                    ok=False,
                    command="permissions",
                    error=dict(
                        code="PERM_DENIED", disposition=dict(delivery="not_delivered", retry="safe")
                    ),
                ),
            )
        ],
    )
    result = import_desktop(Store(tmp_path / "store"), tmp_path / "absent", response, MAPPING)
    assert result["commands"] == []
    assert result["coverage"]["missing_sources"] == 1
    assert result["coverage"]["orphan_responses"] == 1
    assert result["task_acceptance"] == "unknown"


@pytest.mark.parametrize("delivery,retry", [("alien", "safe"), ("unknown", "alien")])
def test_unknown_disposition_is_visible(tmp_path, delivery, retry):
    trace = write(
        tmp_path / "trace.jsonl",
        [
            event(0, "trace.meta", schema=1),
            event(1, "command.start", command="click"),
            event(2, "command.end", command="click", ok=True, duration_ms=1),
        ],
    )
    response = write(
        tmp_path / "response.jsonl",
        [
            dict(
                writer_pid=7,
                writer_proc_start_ms=900,
                command_seq=1,
                response=dict(
                    ok=True,
                    command="click",
                    data=dict(disposition=dict(delivery=delivery, retry=retry)),
                ),
            )
        ],
    )
    result = import_desktop(Store(tmp_path / "store"), trace, response, MAPPING)
    assert result["commands"][0]["delivery"] == "unknown"
    assert result["commands"][0]["disposition_conflict"]


def test_trace_show_wrapper_truncation_and_response_mismatch(tmp_path):
    trace = tmp_path / "show.json"
    trace.write_text(
        json.dumps(
            dict(
                ok=True,
                data=dict(
                    events=[
                        event(0, "trace.meta", schema=2),
                        event(1, "command.start", command="click"),
                    ],
                    truncated=True,
                    total_events=9,
                    returned_events=2,
                    warnings=["SECRET"],
                ),
            )
        )
    )
    response = write(
        tmp_path / "responses.jsonl",
        [
            dict(
                writer_pid=7,
                writer_proc_start_ms=900,
                command_seq=1,
                response=dict(
                    ok=True,
                    command="type",
                    data=dict(disposition=dict(delivery="delivered_verified", retry="unsafe")),
                ),
            )
        ],
    )
    result = import_desktop(Store(tmp_path / "store"), trace, response, MAPPING)
    assert result["coverage"]["truncated_exports"] == 1
    assert result["coverage"]["unknown_schemas"] == 1
    assert result["coverage"]["mismatched_responses"] == 1
    assert result["commands"][0]["delivery"] == "unknown"
    assert "SECRET" not in json.dumps(result)


def test_nested_same_commands_keep_start_identity(tmp_path):
    from agent_usage_tracker.observations import records

    trace = write(
        tmp_path / "trace.jsonl",
        [
            event(0, "trace.meta", schema=1),
            event(1, "command.start", command="click"),
            event(2, "command.start", command="click"),
            event(3, "command.end", command="click", ok=True, duration_ms=2),
            event(4, "command.end", command="click", ok=True, duration_ms=8),
        ],
    )
    store = Store(tmp_path / "store")
    result = import_desktop(store, trace, None, MAPPING)
    rows = records(store.root / "observations.sqlite3")
    assert {row["duration_ms"] for row in rows} == {2, 8}
    assert {row["source"] for row in rows} == {"agent-desktop"}
    assert all(row["id"] != MAPPING["parent_call_id"] for row in rows)
    assert result["coverage"]["complete"]


def test_caps_fail_before_ingestion(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import desktop

    trace = write(
        tmp_path / "trace.jsonl",
        [event(0, "trace.meta", schema=1), event(1, "command.start", command="click")],
    )
    store = Store(tmp_path / "store")
    monkeypatch.setattr(desktop, "MAX_EVENTS", 1)
    with pytest.raises(AutError, match="event cap"):
        import_desktop(store, trace, None, MAPPING)
    assert not (store.root / "observations.sqlite3").exists()
    monkeypatch.setattr(desktop, "MAX_EVENTS", 100000)
    monkeypatch.setattr(desktop, "MAX_BYTES", 8)
    with pytest.raises(AutError, match="byte cap"):
        import_desktop(store, trace, None, MAPPING)


def test_response_copies_conflict_before_ingestion(tmp_path):
    trace = write(tmp_path / "trace.jsonl", [event(0, "trace.meta", schema=1)])
    response = write(
        tmp_path / "response.jsonl",
        [
            dict(
                writer_pid=1,
                writer_proc_start_ms=1,
                command_seq=1,
                response=dict(ok=ok, command="click"),
            )
            for ok in (True, False)
        ],
    )
    store = Store(tmp_path / "store")
    with pytest.raises(AutError, match="conflicting response"):
        import_desktop(store, trace, response, MAPPING)
    assert not (store.root / "observations.sqlite3").exists()


def test_multiple_sessions_share_primary_stream(tmp_path):
    from agent_usage_tracker.observations import records

    store = Store(tmp_path / "store")
    for session in ("desk", "other"):
        rows = [
            event(0, "trace.meta", schema=1),
            event(1, "command.start", command="click"),
            event(2, "command.end", command="click", ok=True, duration_ms=1),
        ]
        for row in rows:
            row["session_id"] = session
        trace = write(tmp_path / (session + ".jsonl"), rows)
        mapping = {**MAPPING, "desktop_session_id": session}
        first = import_desktop(store, trace, None, mapping)
        again = import_desktop(store, trace, None, mapping)
        assert first["ingestion"]["accepted"] == 1
        assert again["ingestion"]["duplicates"] == 1
    stored = records(store.root / "observations.sqlite3")
    assert len(stored) == 2
    assert {row["stream"] for row in stored} == {"commands"}


def test_preflight_without_native_identity_stays_orphan(tmp_path):
    response = write(
        tmp_path / "response.jsonl",
        [
            dict(
                writer_pid=123,
                response=dict(
                    version="2.6",
                    ok=False,
                    command="snapshot",
                    error=dict(
                        code="PERM_DENIED",
                        message="SECRET",
                        disposition=dict(delivery="not_delivered", retry="safe"),
                    ),
                ),
            )
        ],
    )
    result = import_desktop(Store(tmp_path / "store"), tmp_path / "missing", response, MAPPING)
    assert result["commands"] == []
    assert result["orphan_responses"] == [
        dict(
            association="unknown",
            status="error",
            response_code="PERM_DENIED",
            delivery="unknown",
            retry="unknown",
        )
    ]
    assert result["coverage"]["orphan_responses"] == 1
    assert "SECRET" not in json.dumps(result)


def test_partial_then_completed_trace_ingests_once(tmp_path):
    from agent_usage_tracker.observations import records

    store = Store(tmp_path / "store")
    rows = [event(0, "trace.meta", schema=1), event(1, "command.start", command="click")]
    trace = write(tmp_path / "trace.jsonl", rows)
    partial = import_desktop(store, trace, None, MAPPING)
    assert partial["commands"][0]["status"] == "unknown"
    assert records(store.root / "observations.sqlite3") == []
    write(trace, [*rows, event(2, "command.end", command="click", ok=True, duration_ms=1)])
    completed = import_desktop(store, trace, None, MAPPING)
    assert completed["commands"][0]["id"] == partial["commands"][0]["id"]
    assert completed["ingestion"]["accepted"] == 1
    assert records(store.root / "observations.sqlite3")[0]["status"] == "ok"


def test_completed_result_conflict_is_disputed_without_replacing_observation(tmp_path):
    from agent_usage_tracker.observations import records

    store = Store(tmp_path / "store")
    rows = [event(0, "trace.meta", schema=1), event(1, "command.start", command="click")]
    trace = write(
        tmp_path / "trace.jsonl",
        [*rows, event(2, "command.end", command="click", ok=True, duration_ms=1)],
    )
    import_desktop(store, trace, None, MAPPING)
    write(trace, [*rows, event(2, "command.end", command="click", ok=False, duration_ms=1)])
    with pytest.raises(AutError, match="observation conflict"):
        import_desktop(store, trace, None, MAPPING)
    receipts = [store.get("desktop_imports", key) for key in store.keys("desktop_imports")]
    disputed = next(row for row in receipts if row["ingestion"]["conflicts"])
    assert disputed["persistence"] == "disputed"
    assert not disputed["coverage"]["complete"]
    assert records(store.root / "observations.sqlite3")[0]["status"] == "unknown"
    import sqlite3
    with sqlite3.connect(store.root / "observations.sqlite3") as db:
        assert json.loads(db.execute("SELECT body FROM observations").fetchone()[0])["status"] == "ok"
        assert json.loads(db.execute("SELECT body FROM tool_disputes").fetchone()[0])["status"] == "error"


def test_mixed_conflicts_preserve_partial_writes_and_disputed_retry(tmp_path):
    from agent_usage_tracker.observations import records

    store = Store(tmp_path / "store")
    rows = [event(0, "trace.meta", schema=1), event(1, "command.start", command="click")]
    trace = write(
        tmp_path / "trace.jsonl",
        [*rows, event(2, "command.end", command="click", ok=True, duration_ms=1)],
    )
    import_desktop(store, trace, None, MAPPING)
    write(
        trace,
        [
            *rows,
            event(2, "command.end", command="click", ok=False, duration_ms=1),
            event(3, "command.start", command="snapshot"),
            event(4, "command.end", command="snapshot", ok=True, duration_ms=2),
        ],
    )
    for attempt in range(2):
        with pytest.raises(
            AutError, match="some observations may already be stored; conflict unresolved"
        ):
            import_desktop(store, trace, None, MAPPING)
        points = records(store.root / "observations.sqlite3")
        assert len(points) == 2
        assert [point["status"] for point in points] == ["unknown", "ok"]
    receipts = [store.get("desktop_imports", key) for key in store.keys("desktop_imports")]
    disputed = [row for row in receipts if row["persistence"] == "disputed"]
    assert len(disputed) == 2
    assert all(row["success"] is False and not row["coverage"]["complete"] for row in disputed)
    assert {row["ingestion"]["accepted"] for row in disputed} == {0, 1}
    assert {row["ingestion"]["duplicates"] for row in disputed} == {0, 1}
    assert all(row["ingestion"]["conflicts"] == 1 for row in disputed)
    assert all(row["task_acceptance"] == "unknown" for row in receipts)


@pytest.mark.parametrize('late', [False, True])
def test_outcome_dispute_stays_unknown_in_receipt_and_reports(tmp_path, late):
    import sqlite3
    from agent_usage_tracker.observations import records
    from agent_usage_tracker.observation_report import build_report, public_draft
    trace = write(tmp_path / 'trace.jsonl', [
        event(0, 'trace.meta', schema=1),
        event(1, 'command.start', command='click'),
        event(2, 'command.end', command='click', ok=True),
    ])
    response = write(tmp_path / 'response.jsonl', [dict(
        writer_pid=7, writer_proc_start_ms=900, command_seq=1,
        response=dict(command='click', ok=False, error={}))])
    store = Store(tmp_path / 'store')
    db = store.root / 'observations.sqlite3'
    if late:
        import_desktop(store, trace, None, MAPPING)
        with sqlite3.connect(db) as connection:
            original = connection.execute('SELECT body FROM observations').fetchone()[0]
        with pytest.raises(AutError, match='some observations may already be stored'):
            import_desktop(store, trace, response, MAPPING)
        with sqlite3.connect(db) as connection:
            assert connection.execute('SELECT body FROM observations').fetchone()[0] == original
        receipts = [store.get('desktop_imports', key) for key in store.keys('desktop_imports')]
        receipt = next(row for row in receipts if row['coverage']['response_outcome_conflicts'])
    else:
        receipt = import_desktop(store, trace, response, MAPPING)
    assert receipt['commands'][0]['status'] == 'unknown'
    assert receipt['coverage']['response_outcome_conflicts'] == 1
    assert receipt['commands'][0]['trace_ok'] is True
    assert receipt['commands'][0]['response_ok'] is False
    assert records(db)[0]['status'] == 'unknown'
    report = build_report(db)
    assert next(iter(report['tools'].values()))['unknown'] == 1
    assert public_draft(report)['observed_tools']['desktop'] == dict(ok=0, error=0, unknown=1)
    with pytest.raises(AutError, match='conflict'):
        import_desktop(store, trace, None, MAPPING)
    assert records(db)[0]['status'] == 'unknown'
