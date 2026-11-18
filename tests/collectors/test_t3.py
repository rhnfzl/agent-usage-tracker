"""T3 projection reads preserve human provenance and native token identities."""

import json
import sqlite3

import pytest

from agent_usage_tracker.collectors import t3, drain
from agent_usage_tracker.config import Config
from agent_usage_tracker.event import ActivityEvent, CollectorUnavailable, EventSession, UsageEvent

P = "orchestration_v2_projection_"
NATIVE = "11111111-2222-4333-8444-555555555555"
T = "2026-11-14T12:00:"


def config(home):
    return Config(
        "test",
        ("t3", "codex", "claude_code"),
        None,
        None,
        None,
        home,
        t3_activity_since="2026-11-14T00:00:00Z",
    )


def database(home, wal=False):
    path = home / ".t3/userdata/statev2.sqlite"
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
    c.executescript(f"""
    CREATE TABLE projection_projects(project_id TEXT, workspace_root TEXT);
    CREATE TABLE {P}threads(thread_id TEXT, project_id TEXT, payload_json TEXT, updated_at TEXT);
    CREATE TABLE {P}runs(run_id TEXT,thread_id TEXT,provider TEXT,provider_thread_id TEXT,
      status TEXT,requested_at TEXT,completed_at TEXT,payload_json TEXT);
    CREATE TABLE {P}messages(message_id TEXT,thread_id TEXT,run_id TEXT,node_id TEXT,
      role TEXT,created_at TEXT,payload_json TEXT);
    CREATE TABLE {P}turn_items(run_id TEXT,type TEXT,status TEXT,payload_json TEXT);
    CREATE TABLE {P}provider_threads(provider_thread_id TEXT,thread_id TEXT,provider TEXT,payload_json TEXT);
    """)
    c.execute("INSERT INTO projection_projects VALUES (?,?)", ("p", "/private/project"))
    c.execute(
        f"INSERT INTO {P}threads VALUES (?,?,?,?)",
        ("app", "p", json.dumps({"worktreePath": "/private/worktree"}), T + "20Z"),
    )
    return c


def message(c, mid="m", source="web", author="user", ts="00Z", run="r"):
    c.execute(
        f"INSERT INTO {P}messages VALUES (?,?,?,?,?,?,?)",
        (
            mid,
            "app",
            run,
            None,
            "user",
            T + ts,
            json.dumps(
                {"text": "private prompt words", "createdBy": author, "creationSource": source}
            ),
        ),
    )


def run(c, rid="r", provider="codex", binding="pt", start="01Z", end="10Z"):
    c.execute(
        f"INSERT INTO {P}runs VALUES (?,?,?,?,?,?,?,?)",
        (
            rid,
            "app",
            provider,
            binding,
            "completed",
            T + start,
            T + end,
            json.dumps({"userMessageId": "m"}),
        ),
    )


def binding(c, native=NATIVE, driver="codex", pt="pt", strength="strong"):
    c.execute(
        f"INSERT INTO {P}provider_threads VALUES (?,?,?,?)",
        (
            pt,
            "app",
            driver,
            json.dumps(
                {"nativeThreadRef": {"driver": driver, "nativeId": native, "strength": strength}}
            ),
        ),
    )


def collect(cfg, wm=None, ack=True):
    rows = []
    for batch, advance in t3.collect(cfg, {} if wm is None else wm):
        rows.extend(batch)
        if ack:
            advance()
    return rows


def test_in_run_human_steer_is_counted_once_with_its_initial_prompt(tmp_path):
    c = database(tmp_path)
    message(c)
    message(c, mid='steer', ts='05Z')
    run(c)
    binding(c)
    c.commit()
    wm = {}
    rows = collect(config(tmp_path), wm)
    assert sum(isinstance(r, UsageEvent) and r.kind == 'human' for r in rows) == 2
    assert sum(isinstance(r, ActivityEvent) and r.kind == 'steer' for r in rows) == 2
    assert sum(isinstance(r, ActivityEvent) and r.kind == 'turn' for r in rows) == 1
    assert collect(config(tmp_path), wm) == []
    c.close()


def test_malformed_app_provenance_does_not_stop_native_linking(tmp_path):
    c = database(tmp_path)
    message(c)
    c.execute(f'UPDATE {P}messages SET payload_json=?',
              (json.dumps({'createdBy': 'user', 'creationSource': ['bad']}),))
    run(c)
    binding(c)
    c.commit()
    with pytest.warns(UserWarning, match='linking unavailable'):
        assert t3.native_bindings(config(tmp_path)) == {}
    c.close()


def test_t3_tools_without_app_database_are_not_an_installed_collector(tmp_path):
    (tmp_path / '.t3/tools').mkdir(parents=True)
    cfg = config(tmp_path)
    cfg.t3_activity_since = None
    assert collect(cfg) == []
    assert t3.describe(cfg, {}) == 'not installed'


def test_missing_t3_label_index_reports_source_unavailable(tmp_path):
    from agent_usage_tracker.label.context import session_paths

    with pytest.raises(CollectorUnavailable, match='t3'):
        session_paths(tmp_path, 't3')


def test_t3_label_context_reads_only_requested_session(tmp_path):
    from agent_usage_tracker.label.context import t3_context
    from agent_usage_tracker.event import make_event_id

    c = database(tmp_path)
    message(c)
    huge = json.dumps({'createdBy': 'user', 'creationSource': 'web',
                       'text': 'x' * (t3._MAX_RECORD_BYTES + 1)})
    c.execute(f'INSERT INTO {P}messages VALUES (?,?,?,?,?,?,?)',
              ('other', 'unrelated', 'other-run', None, 'user', T+'01Z', huge))
    c.commit()
    eid = make_event_id('t3', 'steer', 'm')
    assert t3_context(tmp_path, {'t3:app'}, {eid}, None)[eid].text == 'private prompt words'
    c.close()


def test_humans_only_private_and_acknowledged(tmp_path):
    c = database(tmp_path, wal=True)
    message(c)
    for i, (source, author) in enumerate(
        [
            ("mobile", "user"),
            ("server", "user"),
            ("mcp", "agent"),
            ("provider", "agent"),
            ("server", "agent"),
        ]
    ):
        message(c, str(i), source, author)
        if source == "mobile":
            run(c, "mobile-run")
            c.execute(
                f"UPDATE {P}runs SET payload_json=? WHERE run_id=?",
                (json.dumps({"userMessageId": str(i)}), "mobile-run"),
            )
    run(c)
    binding(c)
    c.commit()
    wm = {}
    before = collect(config(tmp_path), wm, ack=False)
    assert wm == {}
    rows = collect(config(tmp_path), wm)
    assert [r.event_id for r in rows] == [r.event_id for r in before]
    humans = [r for r in rows if isinstance(r, UsageEvent)]
    assert len(humans) == 2
    assert all(
        r.total_tokens == 0
        and r.est_cost_usd is None
        and r.project == "worktree"
        and r.session.session_id == "t3:app"
        and r.session.run_origin == "interactive"
        for r in humans
    )
    assert len([r for r in rows if isinstance(r, ActivityEvent) and r.kind == "steer"]) == 2
    assert "private prompt" not in repr(rows) and "/private/" not in repr(rows)
    assert collect(config(tmp_path), wm) == []
    # An appended imported message has an old timestamp but remains a new app message.
    message(c, "later", ts="00Z", run="r3")
    run(c, "r3")
    c.execute(
        f"UPDATE {P}runs SET payload_json=? WHERE run_id=?",
        (json.dumps({"userMessageId": "later"}), "r3"),
    )
    c.commit()
    assert len([r for r in collect(config(tmp_path), wm) if isinstance(r, UsageEvent)]) == 1
    c.close()


def test_native_overlap_switch_and_old_history(tmp_path):
    c = database(tmp_path)
    message(c)
    run(c)
    binding(c)
    message(c, "m2", ts="11Z", run="r2")
    run(c, "r2", "claudeAgent-Ultra", "pt2", "12Z", "20Z")
    c.execute(
        f"UPDATE {P}runs SET payload_json=? WHERE run_id=?",
        (json.dumps({"userMessageId": "m2"}), "r2"),
    )
    binding(c, "other", "claudeAgent", "pt2")
    c.commit()
    c.close()
    native = tmp_path / ".codex/sessions/rollout-native.jsonl"
    native.parent.mkdir(parents=True)
    native.write_text(json.dumps({"type": "session_meta", "payload": {"id": NATIVE}}) + "\n")
    rows = collect(config(tmp_path))
    assert len([r for r in rows if isinstance(r, ActivityEvent) and r.kind == "turn"]) == 2

    def source(cfg, wm):
        yield (
            [
                UsageEvent(
                    "token",
                    T + "05Z",
                    "codex",
                    "model",
                    "openai",
                    total_tokens=23,
                    session=EventSession(NATIVE, None, "interactive", None),
                ),
                UsageEvent(
                    "mirror",
                    T + "00Z",
                    "codex",
                    None,
                    None,
                    kind="human",
                    session=EventSession(NATIVE, None, "interactive", None),
                ),
                UsageEvent(
                    "old",
                    "2026-11-13T12:00:00Z",
                    "codex",
                    None,
                    None,
                    kind="human",
                    session=EventSession(NATIVE, None, "interactive", None),
                ),
                ActivityEvent("steer", T + "00Z", "codex", "steer", NATIVE, None, {}),
            ],
            lambda: None,
        )

    out = [
        r
        for batches in drain(source, config(tmp_path), {}, "codex")
        for rs, ack in batches
        for r in rs
    ]
    assert [r.event_id for r in out] == ["token", "old"]
    assert out[0].total_tokens == 23 and out[0].session.session_id == "t3:app"
    assert out[1].session.session_id == NATIVE


@pytest.mark.parametrize("broken", ["missing", "corrupt", "schema", "json"])
def test_degraded_sources(tmp_path, broken):
    if broken == "missing":
        cfg = config(tmp_path)
        cfg.t3_activity_since = None
        assert collect(cfg) == []  # T3 is not installed.
        (tmp_path / ".t3").mkdir()
    elif broken == "corrupt":
        path = tmp_path / ".t3/userdata/statev2.sqlite"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"bad sqlite")
    else:
        c = database(tmp_path)
        if broken == "schema":
            c.execute(f"DROP TABLE {P}messages")
        else:
            message(c)
            c.execute(f"UPDATE {P}messages SET payload_json='invalid'")
        c.commit()
        c.close()
    with pytest.raises(CollectorUnavailable):
        collect(config(tmp_path))


def test_cutover_no_reads_without_configuration_and_pending_binding(tmp_path, monkeypatch):
    from dataclasses import replace

    c = database(tmp_path)
    message(c)
    run(c)
    c.commit()
    cfg = config(tmp_path)
    wm = {}
    with pytest.raises(CollectorUnavailable, match="overlap is unproved"):
        collect(cfg, wm)
    assert wm == {}
    binding(c)
    c.commit()
    assert len([r for r in collect(cfg, wm) if isinstance(r, UsageEvent)]) == 1
    assert collect(replace(cfg, t3_activity_since="2026-11-15T00:00:00Z")) == []

    def forbidden(*args, **kwargs):
        raise AssertionError("must not read before explicit cutover")

    monkeypatch.setattr(t3, "connection", forbidden)
    with pytest.raises(CollectorUnavailable, match="cutover"):
        collect(replace(cfg, t3_activity_since=None))
    assert t3.native_bindings(replace(cfg, t3_activity_since=None)) == {}
    c.close()


def test_read_only_consistent_wal_snapshot(tmp_path):
    c = database(tmp_path, wal=True)
    message(c)
    run(c)
    binding(c)
    c.commit()
    with t3.connection(tmp_path) as con:
        assert con.execute(f"SELECT count(*) FROM {P}messages").fetchone()[0] == 1
        message(c, "new")
        c.commit()
        assert con.execute(f"SELECT count(*) FROM {P}messages").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute(f"DELETE FROM {P}messages")
    c.close()


def test_tool_metadata_and_local_context(tmp_path):
    from agent_usage_tracker.label.context import session_paths, t3_context
    from agent_usage_tracker.event import make_event_id

    c = database(tmp_path)
    message(c)
    run(c)
    binding(c)
    c.execute(
        f"INSERT INTO {P}turn_items VALUES (?,?,?,?)",
        ("r", "command_execution", "completed", json.dumps({"exitCode": 1, "input": "secret"})),
    )
    c.execute(
        f"INSERT INTO {P}messages VALUES (?,?,?,?,?,?,?)",
        (
            "reply",
            "app",
            "r",
            None,
            "assistant",
            T + "09Z",
            json.dumps(
                {"text": "private reply", "createdBy": "agent", "creationSource": "provider"}
            ),
        ),
    )
    c.commit()
    rows = collect(config(tmp_path))
    turn = next(r for r in rows if isinstance(r, ActivityEvent) and r.kind == "turn")
    assert turn.attrs["tool_calls"] == 1 and turn.attrs["tool_errors"] == 1
    assert "secret" not in repr(rows)
    eid = make_event_id("t3", "steer", "m")
    ctx = t3_context(tmp_path, {"t3:app"}, {eid}, None)[eid]
    assert ctx.text == "private prompt words" and ctx.after == "private reply"
    assert "t3:app" in session_paths(tmp_path, "t3")
    c.close()


def test_locked_database_is_degraded(tmp_path):
    c = database(tmp_path)
    c.commit()
    c.execute("BEGIN EXCLUSIVE")
    with pytest.raises(CollectorUnavailable):
        collect(config(tmp_path))
    c.rollback()
    c.close()


@pytest.mark.parametrize(
    "source,author,origin",
    [
        ("mcp", "agent", "automated"),
        ("provider", "agent", "automated"),
        ("server", "agent", "automated"),
        ("server", "user", "unknown"),
    ],
)
def test_native_software_prompts_do_not_become_human(tmp_path, source, author, origin):
    c = database(tmp_path)
    message(c, source=source, author=author)
    run(c)
    binding(c)
    c.commit()
    assert collect(config(tmp_path)) == []
    rows = [
        UsageEvent(
            "mirror",
            T + "05Z",
            "codex",
            None,
            None,
            kind="human",
            session=EventSession(NATIVE, None, "interactive", None),
        ),
        UsageEvent(
            "usage",
            T + "05Z",
            "codex",
            "model",
            "openai",
            total_tokens=7,
            session=EventSession(NATIVE, None, "interactive", None),
        ),
        ActivityEvent("steer", T + "05Z", "codex", "steer", NATIVE, None, {}),
        ActivityEvent("turn", T + "09Z", "codex", "turn", NATIVE, None, {}),
    ]
    result = t3.canonicalize(rows, t3.native_bindings(config(tmp_path)))
    assert [r.event_id for r in result] == ["usage"]
    assert result[0].total_tokens == 7 and result[0].session.run_origin == origin
    assert result[0].session.session_id == NATIVE
    c.close()


def test_native_scan_refreshes_proof_for_newer_rows(tmp_path, monkeypatch):
    c = database(tmp_path)
    message(c)
    run(c)
    binding(c)
    c.execute(f"UPDATE {P}messages SET created_at=?", ("2050-11-14T12:00:00Z",))
    c.execute(
        f"UPDATE {P}runs SET requested_at=?,completed_at=?",
        ("2050-11-14T12:00:01Z", "2050-11-14T12:00:10Z"),
    )
    c.commit()
    original = t3.native_bindings
    calls = []

    def delayed_proof(cfg):
        calls.append(1)
        return {} if len(calls) == 1 else original(cfg)

    monkeypatch.setattr(t3, "native_bindings", delayed_proof)

    def source(cfg, wm):
        yield (
            [
                UsageEvent(
                    "tokens",
                    "2050-11-14T12:00:05Z",
                    "codex",
                    "m",
                    "openai",
                    total_tokens=9,
                    session=EventSession(NATIVE, None, "interactive", None),
                )
            ],
            lambda: None,
        )

    rows = [
        r
        for batches in drain(source, config(tmp_path), {}, "codex")
        for batch, ack in batches
        for r in batch
    ]
    assert len(calls) == 2 and rows[0].session.session_id == "t3:app"
    c.close()


def test_bounded_reads_fail_degraded(tmp_path, monkeypatch):
    c = database(tmp_path)
    message(c)
    message(c, "second")
    c.commit()
    monkeypatch.setattr(t3, "_MAX_ROWS", 1)
    wm = {}
    with pytest.raises(CollectorUnavailable):
        collect(config(tmp_path), wm)
    assert wm == {}
    c.close()


@pytest.mark.parametrize("value", ["not-a-timestamp", "2026-11-14T12:00:00"])
def test_cutover_config_validation(tmp_path, value):
    from agent_usage_tracker.config import _config_from, AutError

    with pytest.raises(AutError, match="t3_activity_since"):
        _config_from({"t3_activity_since": value}, tmp_path / "config.toml")


def test_t3_native_client_quarantine_before_late_binding(tmp_path):
    from datetime import datetime, timezone

    native = tmp_path / ".codex/sessions/rollout-t3.jsonl"
    native.parent.mkdir(parents=True)
    native.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": NATIVE, "originator": "T3 Code", "source": "vscode"},
            }
        )
        + "\n"
    )
    cfg = config(tmp_path)
    clients = t3.native_clients(cfg)
    assert clients == {("codex", NATIVE)}
    since = datetime(2026, 11, 14, tzinfo=timezone.utc)
    rows = [
        UsageEvent(
            "human",
            T + "00Z",
            "codex",
            None,
            None,
            kind="human",
            session=EventSession(NATIVE, None, "interactive", None),
        ),
        UsageEvent(
            "usage",
            T + "05Z",
            "codex",
            "m",
            "openai",
            total_tokens=8,
            session=EventSession(NATIVE, None, "interactive", None),
        ),
        ActivityEvent("steer", T + "00Z", "codex", "steer", NATIVE, None, {}),
        ActivityEvent("turn", T + "09Z", "codex", "turn", NATIVE, None, {}),
    ]
    result = t3.canonicalize(rows, {}, clients, since)
    assert [r.event_id for r in result] == ["usage"]
    assert result[0].session.run_origin == "unknown" and result[0].total_tokens == 8
    c = database(tmp_path)
    message(c)
    run(c)
    c.commit()
    with pytest.raises(CollectorUnavailable, match="overlap is unproved"):
        collect(cfg)
    binding(c)
    c.commit()
    assert len([r for r in collect(cfg) if isinstance(r, UsageEvent)]) == 1
    c.close()


@pytest.mark.parametrize("origin", ["unknown", "interactive", "automated", "subagent"])
def test_human_app_proof_only_promotes_unknown_native_origin(tmp_path, origin):
    c = database(tmp_path)
    message(c)
    run(c)
    binding(c)
    c.commit()
    row = UsageEvent(
        "usage",
        T + "05Z",
        "codex",
        "m",
        "openai",
        total_tokens=8,
        session=EventSession(NATIVE, None, origin, None),
    )
    result = t3.canonicalize([row], t3.native_bindings(config(tmp_path)))[0]
    if origin in {"unknown", "interactive"}:
        assert result.session.session_id == "t3:app" and result.session.run_origin == "interactive"
    else:
        assert result.session.session_id == NATIVE and result.session.run_origin == origin
    assert result.event_id == "usage" and result.total_tokens == 8
    if origin in {"unknown", "interactive"}:
        link = ActivityEvent(
            "link", T + "05Z", "codex", "link", NATIVE, None, {"run_origin": origin}
        )
        mapped_link = t3.canonicalize([link], t3.native_bindings(config(tmp_path)))[0]
        assert mapped_link.session_id == "t3:app"
        assert mapped_link.attrs["run_origin"] == "interactive"
    c.close()


def test_absent_optional_tool_projection_stays_unknown(tmp_path):
    c = database(tmp_path)
    message(c)
    run(c)
    binding(c)
    c.execute(f"DROP TABLE {P}turn_items")
    c.commit()
    rows = collect(config(tmp_path))
    turn = next(row for row in rows if isinstance(row, ActivityEvent) and row.kind == "turn")
    assert "tool_calls" not in turn.attrs and "tool_errors" not in turn.attrs
    c.close()


def test_overlapping_human_and_automated_runs_keep_native_origin_unknown():
    from datetime import datetime, timezone

    start = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    end = datetime(2026, 11, 14, 13, tzinfo=timezone.utc)
    bindings = {
        ("codex", NATIVE): [
            t3.Binding("t3:app", start, end, "interactive"),
            t3.Binding("t3:app", start, end, "automated"),
        ]
    }
    row = UsageEvent(
        "usage",
        T + "05Z",
        "codex",
        "m",
        "openai",
        total_tokens=8,
        session=EventSession(NATIVE, None, "interactive", None),
    )
    result = t3.canonicalize([row], bindings)[0]
    assert result.session.session_id == NATIVE and result.session.run_origin == "unknown"
    assert result.event_id == "usage" and result.total_tokens == 8


@pytest.mark.parametrize('origin', ['automated', 'subagent'])
@pytest.mark.parametrize('linked', [True, False])
def test_positive_link_origin_survives_app_binding_and_client_quarantine(origin, linked):
    from datetime import datetime, timezone

    start = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    end = datetime(2026, 11, 14, 13, tzinfo=timezone.utc)
    spans = {('codex', NATIVE): [t3.Binding('t3:app', start, end)]} if linked else {}
    row = ActivityEvent('link', T+'05Z', 'codex', 'link', NATIVE, None,
                        {'run_origin': origin})
    result = t3.canonicalize([row], spans, {('codex', NATIVE)}, start)[0]
    assert result.session_id == NATIVE
    assert result.attrs['run_origin'] == origin


def test_current_oversized_message_fails_degraded_without_ack(tmp_path):
    c = database(tmp_path)
    message(c)
    run(c)
    binding(c)
    c.execute(
        f"UPDATE {P}messages SET payload_json=?",
        (
            json.dumps(
                {"createdBy": "user", "creationSource": "web", "text": "x" * (1024 * 1024 + 1)}
            ),
        ),
    )
    c.commit()
    wm = {}
    with pytest.raises(CollectorUnavailable):
        collect(config(tmp_path), wm)
    assert wm == {}
    # Native linking needs provenance metadata, not the oversized prompt body.
    assert ("codex", NATIVE) in t3.native_bindings(config(tmp_path))
    c.close()


def test_read_truncates_payload_bytes_inside_sql(tmp_path):
    c = database(tmp_path)
    message(c)
    oversized = "𐍈" * (1024 * 1024)
    c.execute(f"UPDATE {P}messages SET payload_json=?", (oversized,))
    c.commit()
    received = []

    def observe(cursor, row):
        received.extend(
            len(value.encode("utf-8")) if isinstance(value, str) else len(value)
            for value in row
            if isinstance(value, (str, bytes))
        )
        return row

    c.row_factory = observe
    with pytest.raises(ValueError, match="bounded"):
        t3._read(c, f"SELECT payload_json FROM {P}messages")
    assert received and max(received) <= 1024 * 1024 + 1
    c.close()


def test_read_total_query_budget_is_atomic(tmp_path, monkeypatch):
    c = database(tmp_path)
    message(c)
    message(c, "second")
    c.commit()
    first_size = c.execute(
        f"SELECT length(CAST(payload_json AS BLOB)) FROM {P}messages LIMIT 1"
    ).fetchone()[0]
    assert first_size < 100 < first_size * 2
    monkeypatch.setattr(t3, "_MAX_QUERY_BYTES", 100)
    with pytest.raises(ValueError, match="bounded"):
        t3._read(c, f"SELECT payload_json FROM {P}messages")
    c.close()


def test_historical_oversized_records_do_not_block_current_task(tmp_path):
    c = database(tmp_path)
    message(c)
    run(c)
    binding(c)
    huge = "x" * (1024 * 1024 + 1)
    old = "2026-11-14T00:30:00+02:00"
    c.execute("INSERT INTO projection_projects VALUES (?,?)", ("old-project", huge))
    c.execute(
        f"INSERT INTO {P}threads VALUES (?,?,?,?)",
        ("old-thread", "old-project", json.dumps({"worktreePath": huge}), old),
    )
    c.execute(
        f"INSERT INTO {P}messages VALUES (?,?,?,?,?,?,?)",
        (
            "old-message",
            "old-thread",
            "old-run",
            None,
            "user",
            old,
            json.dumps({"createdBy": "user", "creationSource": "web", "text": huge}),
        ),
    )
    c.execute(
        f"INSERT INTO {P}runs VALUES (?,?,?,?,?,?,?,?)",
        (
            "old-run",
            "old-thread",
            "codex",
            "old-provider",
            "completed",
            old,
            old,
            json.dumps({"userMessageId": huge}),
        ),
    )
    c.execute(
        f"INSERT INTO {P}provider_threads VALUES (?,?,?,?)",
        (
            "old-provider",
            "old-thread",
            "codex",
            json.dumps(
                {"nativeThreadRef": {"driver": "codex", "nativeId": huge, "strength": "strong"}}
            ),
        ),
    )
    c.execute(
        f"INSERT INTO {P}turn_items VALUES (?,?,?,?)",
        ("old-run", "command_execution", "completed", json.dumps({"exitCode": 0, "output": huge})),
    )
    c.commit()
    rows = collect(config(tmp_path))
    assert len([row for row in rows if isinstance(row, UsageEvent)]) == 1
    assert len([row for row in rows if isinstance(row, ActivityEvent) and row.kind == "turn"]) == 1
    c.close()


def test_human_steer_into_agent_started_run_is_counted(tmp_path):
    c = database(tmp_path)
    message(c, source="provider", author="agent")
    message(c, mid="steer", ts="05Z")
    run(c, provider="claudeAgent")
    binding(c, driver="claudeAgent")
    c.commit()
    wm = {}
    rows = collect(config(tmp_path), wm)
    humans = [r for r in rows if isinstance(r, UsageEvent) and r.kind == "human"]
    assert [h.session.session_id for h in humans] == ["t3:app"]
    assert sum(isinstance(r, ActivityEvent) and r.kind == "steer" for r in rows) == 1
    assert not any(isinstance(r, ActivityEvent) and r.kind == "turn" for r in rows)
    assert collect(config(tmp_path), wm) == []
    c.close()


def test_human_steer_into_unbound_agent_run_stays_unproved(tmp_path):
    c = database(tmp_path)
    message(c, source="provider", author="agent")
    message(c, mid="steer", ts="05Z")
    run(c, provider="claudeAgent")
    c.commit()
    with pytest.raises(CollectorUnavailable, match="overlap is unproved"):
        collect(config(tmp_path))
    c.close()
