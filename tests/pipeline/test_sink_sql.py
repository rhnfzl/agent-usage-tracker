"""The sink layer's SQL, run against a real Postgres. Skipped unless AUT_TEST_PG_DSN is set.

Run: `AUT_TEST_PG_DSN=postgresql://postgres:pw@127.0.0.1:5432/scratch pytest tests/pipeline/test_sink_sql.py`
(one literal IP address and port, no PG* variables set). The DSN must be a
superuser on a scratch database of a TimescaleDB server, with `psql` on PATH: the fixture applies
the shipped schema (sink_stack/apply.sh, the same script `docker compose up` runs), and refuses to
touch a database that already has an `agents` schema. It creates the agents_writer and agents_ro
roles on that server if they are missing, and drops the ones it created at the end.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess  # nosec B404 # fixed argv, the shipped apply script
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import agent_usage_tracker
from agent_usage_tracker import (episode_store, git_days, improve_publish, inspector, outcomes,
                                 pricing, push, summary)
from agent_usage_tracker.config import DEFAULT_LEDGER_TZ, AutError, Config
from agent_usage_tracker.event import (ActivityEvent, EventSession, TimeInterval,
                                       UsageEvent)
from agent_usage_tracker.label import run as label_run
from agent_usage_tracker.label.tasks import Source
from agent_usage_tracker.sink import check_ledger_zone, connect, write_many
from price_fixture import CATALOG, PRICE_DAY, PRICE_MODEL, price_config, price_rows

DSN = os.environ.get("AUT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="AUT_TEST_PG_DSN is not set")

SINK_STACK = Path(agent_usage_tracker.__file__).parent / "sink_stack"

T0 = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
DAY = T0.astimezone().date().isoformat()
EPISODE = uuid.UUID("0f0f0f0f-0000-4000-8000-000000000001")


def _psql_env(dsn: str) -> dict[str, str]:
    """The environment that sends psql to exactly the server and database the scratch check reads.

    Checked before the first connection: anything libpq could resolve differently for psycopg and
    for psql (an inherited PG* variable, an unknown key, a second host to fail over to) stops here.
    """
    from psycopg.conninfo import conninfo_to_dict
    if not shutil.which("psql"):
        pytest.fail("AUT_TEST_PG_DSN is set but psql is not on PATH: the schema is applied with psql")
    if inherited := sorted(k for k in os.environ if k.startswith("PG")):
        pytest.fail(f"unset {inherited}: put the whole connection in AUT_TEST_PG_DSN")
    info = conninfo_to_dict(dsn)
    names = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
             "dbname": "PGDATABASE", "sslmode": "PGSSLMODE"}
    if unknown := sorted(set(info) - set(names)):
        pytest.fail(f"AUT_TEST_PG_DSN uses {unknown}: the test passes only {sorted(names)} to psql")
    # A hostname can resolve to several servers, and a missing host or port falls back to client
    # build defaults, so both clients could reach different servers: one literal endpoint only.
    host, port = str(info.get("host", "")), str(info.get("port", ""))
    try:
        literal = host.startswith("/") or bool(ipaddress.ip_address(host))
    except ValueError:
        literal = False
    if not literal or "," in host or not port.isdigit():
        pytest.fail("AUT_TEST_PG_DSN must name one IP address (or socket directory) and one port, "
                    "for example postgresql://postgres:pw@127.0.0.1:5432/scratch")
    # No role passwords: apply.sh then leaves existing roles alone, so a test against a server
    # that also holds a real sink cannot lock its collectors and Grafana out.
    env = {k: v for k, v in os.environ.items() if k not in ("AUT_WRITER_PASSWORD", "AUT_RO_PASSWORD")}
    return env | {names[k]: str(v) for k, v in info.items()} | {"AUT_TZ": DEFAULT_LEDGER_TZ}


@pytest.fixture(scope="module")
def dsn():
    env = _psql_env(DSN)
    with connect(DSN) as conn:
        if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'agents'").fetchone():
            pytest.fail("AUT_TEST_PG_DSN already has an agents schema: use a scratch database")
        roles = ("agents_writer", "agents_ro")
        had = {r for (r,) in conn.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                                          (list(roles),))}
    try:
        run = subprocess.run(["sh", str(SINK_STACK / "apply.sh")], env=env, capture_output=True,
                             text=True, check=False)  # nosec B603 B607
        assert run.returncode == 0, run.stderr[-4000:]
        yield DSN
    finally:
        with connect(DSN) as conn:
            conn.execute("DROP SCHEMA IF EXISTS agents CASCADE")
            for role in (r for r in roles if r not in had):  # roles are server-wide: drop ours only
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    conn.execute(f'DROP OWNED BY "{role}"')
                    conn.execute(f'DROP ROLE "{role}"')
            conn.commit()


def test_the_sink_records_its_zone_and_a_collector_in_another_zone_stops(dsn):
    check_ledger_zone(dsn, DEFAULT_LEDGER_TZ)
    with pytest.raises(AutError, match="set ledger_timezone"):
        check_ledger_zone(dsn, "Pacific/Auckland")


def test_agents_ro_reads_hours_in_the_sink_zone(dsn):
    with connect(dsn) as conn:
        # per database: a test database must not change the zone of a real sink on the server
        assert conn.execute("SELECT setconfig FROM pg_db_role_setting s JOIN pg_roles r "
                            "ON r.oid = s.setrole JOIN pg_database d ON d.oid = s.setdatabase "
                            "WHERE r.rolname = 'agents_ro' AND d.datname = current_database()"
                            ).fetchone()[0] == [f"TimeZone={DEFAULT_LEDGER_TZ}"]


def _rows(dsn: str, table: str) -> int:
    with connect(dsn) as conn:
        return conn.execute(f"SELECT count(*) FROM agents.{table}").fetchone()[0]


def _activity(eid: str, kind: str, session: str = "s1", **attrs) -> ActivityEvent:
    return ActivityEvent(event_id=eid, ts=T0.isoformat(), agent="claude_code", kind=kind,
                         session_id=session, turn_id=None, attrs=attrs, machine="t")


def test_covered_snapshots_update_legacy_rows_without_duplicate_timestamps(dsn):
    from dataclasses import replace
    legacy = replace(_activity("legacy-pi-turn", "turn", tool_calls=1, tool_errors=0), agent="pi")
    push.send_activity_rows(dsn, [legacy])
    corrected = replace(legacy, ts=(T0 - timedelta(minutes=1)).isoformat(),
                        attrs={"tool_calls": 2, "tool_errors": 1,
                               "revision_sources": ["a", "b"]})
    push.send_activity_rows(dsn, [corrected])
    stale = replace(corrected, attrs={"tool_calls": 1, "tool_errors": 0,
                                     "revision_sources": ["a"]})
    push.send_activity_rows(dsn, [stale, corrected])
    with connect(dsn) as conn:
        rows = conn.execute("SELECT ts, attrs FROM agents.activity_event WHERE event_id=%s",
                            (legacy.event_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == T0
    assert rows[0][1] == corrected.attrs


def test_insert_only_writer_defers_corrections_until_column_grant(dsn):
    from dataclasses import replace
    role = "snapshot_writer_" + uuid.uuid4().hex
    password = uuid.uuid4().hex
    # The scratch DSN uses a role in its URI; preserve the endpoint and database.
    from urllib.parse import urlsplit, urlunsplit
    uri = urlsplit(dsn)
    writer_dsn = urlunsplit(uri._replace(
        netloc=f"{role}:{password}@" + uri.netloc.rsplit("@", 1)[-1]))
    with connect(dsn) as conn:
        conn.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\'')
        conn.execute(f'GRANT USAGE ON SCHEMA agents TO "{role}"')
        conn.execute(f'GRANT INSERT, SELECT ON agents.activity_event TO "{role}"')
        conn.commit()
    try:
        first = replace(_activity("restricted-turn", "turn", tool_calls=1, tool_errors=0,
                                  revision_sources=["a"]), agent="cursor_cli")
        push.send_activity_rows(writer_dsn, [first])
        corrected = replace(first, attrs={"tool_calls": 2, "tool_errors": 0,
                                          "revision_sources": ["a", "b"]})
        push.send_activity_rows(writer_dsn, [corrected, _activity("restricted-ordinary", "commit")])
        cfg = Config(machine="t", agents=(), pg_dsn=writer_dsn, webhook_url=None,
                     webhook_secret=None, home=Path("."))
        assert push.outbox_status() == {"activity": 1}
        assert push.replay_outbox(cfg) == 0
        assert push.outbox_status() == {"activity": 1}
        with connect(dsn) as conn:
            assert conn.execute("SELECT attrs->>'tool_calls' FROM agents.activity_event "
                                "WHERE event_id='restricted-turn'").fetchone()[0] == "1"
            assert conn.execute("SELECT count(*) FROM agents.activity_event "
                                "WHERE event_id='restricted-ordinary'").fetchone()[0] == 1
            conn.execute(f'GRANT UPDATE(attrs) ON agents.activity_event TO "{role}"')
            conn.commit()
        assert push.replay_outbox(cfg) == 1
        assert push.outbox_status() == {}
        with connect(dsn) as conn:
            assert conn.execute("SELECT attrs->>'tool_calls' FROM agents.activity_event "
                                "WHERE event_id='restricted-turn'").fetchone()[0] == "2"
    finally:
        with connect(dsn) as conn:
            conn.execute(f'DROP OWNED BY "{role}"')
            conn.execute(f'DROP ROLE "{role}"')
            conn.commit()


def test_estimates_from_another_zone_are_found_and_the_named_delete_removes_only_them(dsn):
    from agent_usage_tracker import history_steers as hs
    zone = ZoneInfo(DEFAULT_LEDGER_TZ)
    noon = datetime(2026, 4, 8, 12, tzinfo=zone)               # written in the ledger zone
    other = datetime(2026, 4, 9, 12, tzinfo=timezone.utc)       # written on a UTC machine
    rows = [ActivityEvent(event_id=f"est-{t.date()}", ts=t.isoformat(), agent="codex",
                          kind="steer_estimate", session_id=None, turn_id=None,
                          attrs={"count": 1, "source": "history"}, machine="t") for t in (noon, other)]
    push.send_activity_rows(dsn, rows[:1])
    assert hs.foreign_estimates(dsn, DEFAULT_LEDGER_TZ) == 0
    push.send_activity_rows(dsn, rows[1:])
    assert hs.foreign_estimates(dsn, DEFAULT_LEDGER_TZ) == 1
    with connect(dsn) as conn:
        conn.execute(hs.drop_foreign_sql(DEFAULT_LEDGER_TZ))
        left = conn.execute("SELECT event_id FROM agents.activity_event "
                            "WHERE kind = 'steer_estimate'").fetchall()
        conn.execute("DELETE FROM agents.activity_event WHERE kind = 'steer_estimate'")
        conn.commit()
    assert left == [("est-2026-04-08",)]


def test_scan_rows_insert_once(dsn):
    session = EventSession("s1", None, "interactive", "t1")
    batch = [UsageEvent(event_id="u1", ts=T0.isoformat(), agent="claude_code", model="m",
                        provider="p", input_tokens=1, total_tokens=1, machine="t", session=session),
             _activity("steer1", "steer", via="typed"),
             TimeInterval(event_id="i1", ts=T0.isoformat(),
                          end_ts=(T0 + timedelta(minutes=5)).isoformat(), source="manual", app=None,
                          title=None, category=None, work_relevant=True, attrs={}, machine="t")]
    for _ in range(2):                 # the second pass is a re-delivery: ON CONFLICT swallows it
        push.insert_pg(dsn, batch)
    counts = {t: _rows(dsn, t) for t in ("usage_event", "event_session", "time_interval")}
    assert counts == {"usage_event": 1, "event_session": 1, "time_interval": 1}
    push.send_activity_rows(dsn, [batch[1]])
    push.send_session_rows(dsn, [batch[0]])


def test_heartbeat_inserts(dsn):
    cfg = Config(machine="t", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=dsn,
                 home=Path("."))
    row = push.scan_row(cfg, "claude_code", T0, newest_ts=T0)
    for _ in range(2):
        push.send_scan_rows(dsn, [row])
    assert _rows(dsn, "collector_scan") == 1


def test_git_daily_coverage_round_trip(dsn):
    day = T0.date()
    for n, complete, seconds in ((3, False, 0), (3, True, 1), (2, False, 2)):
        write_many(dsn, git_days.INSERT_SQL, [(day, n, T0 + timedelta(seconds=seconds), complete)])
    assert git_days.load_stored(dsn) == {day: (3, True)}
    write_many(dsn, git_days.INSERT_SQL, [(day, 5, T0 + timedelta(seconds=3), False)])
    assert git_days.load_stored(dsn) == {day: (5, False)}
    rows, drops = git_days.coverage_rows({day: 5}, {day: True}, git_days.load_stored(dsn))
    assert rows == [(day, 5, True)] and not drops
    write_many(dsn, git_days.INSERT_SQL, [(d, n, T0 + timedelta(seconds=4), c) for d, n, c in rows])
    assert git_days.load_stored(dsn) == {day: (5, True)}


def _scan_status_check(dsn: str, statuses: str) -> None:
    with connect(dsn) as conn:
        conn.execute("ALTER TABLE agents.collector_scan DROP CONSTRAINT collector_scan_status_check")
        conn.execute("ALTER TABLE agents.collector_scan ADD CONSTRAINT collector_scan_status_check "
                     f"CHECK (status IN ({statuses})) NOT VALID")  # new rows only
        conn.commit()


def test_a_degraded_heartbeat_is_stored_as_degraded(dsn):
    cfg = Config(machine="deg-new", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=dsn,
                 home=Path("."))
    push.send_scan_rows(dsn, [push.scan_row(cfg, "aw", T0, status="degraded", error="OSError")])
    with connect(dsn) as conn:
        assert conn.execute("SELECT status FROM agents.collector_scan WHERE machine = 'deg-new'"
                            ).fetchall() == [("degraded",)]


def test_a_degraded_heartbeat_does_not_lose_the_scan_on_an_older_ok_error_check(dsn):
    # a sink deployed before 'degraded' existed refuses it; the scan must still land, as error
    cfg = Config(machine="deg", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=dsn,
                 home=Path("."))
    _scan_status_check(dsn, "'ok', 'error'")
    try:
        push.send_scan_rows(dsn, [push.scan_row(cfg, "aw", T0, status="degraded", error="OSError"),
                                  push.scan_row(cfg, "pi", T0)])
    finally:
        _scan_status_check(dsn, "'ok', 'error', 'degraded'")
    with connect(dsn) as conn:
        assert conn.execute("SELECT collector, status, error FROM agents.collector_scan WHERE "
                            "machine = 'deg' ORDER BY 1").fetchall() \
            == [("aw", "error", "OSError"), ("pi", "ok", None)]


def test_outcome_reads_and_writes(dsn):
    push.send_activity_rows(dsn, [_activity("c1", "commit", sha="abcdef1234", repo_key="k1",
                                            amend=False)])
    rows = outcomes.load_rows(dsn)
    assert ("abcdef1234", "k1", False) in [(r["sha"], r["repo_key"], r["amend"]) for r in rows]
    for state, at in (("open", T0), ("merged", T0 + timedelta(days=1))):
        outcomes.insert_outcomes(dsn, [{"sha": "abcdef1234", "repo_key": "k1", "state": state,
                                "survival": 0.5, "decided_at": at}])
    assert outcomes.load_stored(dsn) == {("abcdef1234", "k1"): ("merged", 0.5)}
    outcomes.insert_manual_outcome(dsn, {"session_id": "s1", "outcome": "shipped", "ts": T0,
                                         "repo_key": "k1"})
    assert _rows(dsn, "manual_outcome") == 1


@pytest.fixture(scope="module")
def episode(dsn):
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.episode VALUES (%s, %s, %s, 1, 1, 1, 1, 0, 'shipped', "
                     "'git', false)", (EPISODE, T0, T0 + timedelta(hours=1)))
        conn.execute("INSERT INTO agents.episode_member VALUES (%s, 's1', 'seed', %s)",
                     (EPISODE, ["k1"]))
        conn.commit()
    return EPISODE


def test_episode_store(dsn, episode):
    eps = episode_store.load_month(dsn, T0 - timedelta(days=1), T0 + timedelta(days=1))
    assert [m["session_id"] for m in eps[0]["members"]] == ["s1"]
    episode_store.insert_corrections(dsn, [{
        "id": str(uuid.uuid4()), "ts": T0, "action": "confirm", "a_session": "s1", "b_session": None,
        "attrs": {"repo_keys": ["k1"]}}])
    assert episode_store.answered_sessions(dsn, ["s1", "s2"]) == {"s1"}


@pytest.mark.parametrize("selector", [("episode", "0f0f"), ("day", DAY),
                                      ("label", f"{EPISODE}:2026-06-15")])
def test_inspector_fetch(dsn, episode, selector):
    push.send_activity_rows(dsn, [_activity("steer2", "steer", via="typed"),
                                  _activity("c2", "commit", sha="abcdef1234", repo_key="k1")])
    raw = inspector.fetch(dsn, *selector)
    assert [ep["id"] for ep in raw["episodes"]] == [str(EPISODE)]
    assert inspector.build(raw)["episodes"], "the rows the sink returns fit what build reads"


def test_label_source_reads_and_note(dsn, episode):
    cfg = Config(machine="t", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=dsn,
                 home=Path("."))
    push.send_activity_rows(dsn, [_activity("l1", "label", unit_id="steer2", task="steer_intent"),
                                  _activity("t1", "turn", duration_ms=5),
                                  _activity("steer3", "steer", via="typed")])
    src = Source(cfg)
    since, until = T0 - timedelta(days=1), T0 + timedelta(days=1)
    assert "steer3" in {r["event_id"] for r in src.steers(since)}
    assert [m["session_id"] for m in src.episodes(since)[0]["members"]] == ["s1"]
    assert "turn" in {a["kind"] for a in src.activity(["s1"], since, until)}
    assert {"unit_id": "steer2", "task": "steer_intent"} in [a for _e, _t, a in
                                                            src.labels(since, until)]
    assert src.episodes_ready() == T0 + timedelta(hours=1)     # no rebuild row: the newest episode
    with connect(dsn) as conn:
        for machine, collector, at in (("t", "claude_code", T0 + timedelta(hours=2)),
                                       ("sink", "episode_link", T0 + timedelta(hours=3)),
                                       ("t", "claude_code", T0 + timedelta(hours=4))):
            conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                         "finished_at, status, events, human) VALUES (%s, %s, %s, %s, 'ok', 0, 0)",
                         (machine, collector, at, at + timedelta(minutes=1)))
        conn.commit()
    assert src.episodes_ready() == T0 + timedelta(hours=2)     # the last scan the rebuild saw
    for text in ("first", "second"):   # the first draft is kept
        label_run._insert_note(dsn, date(2026, 6, 15), text, "m")
    with connect(dsn) as conn:
        assert conn.execute("SELECT text FROM agents.daily_note").fetchall() == [("first",)]


def test_episode_readiness_excludes_scans_completed_during_the_rebuild(dsn):
    cfg = Config(machine="snapshot-test", agents=(), webhook_url=None, webhook_secret=None,
                 pg_dsn=dsn, home=Path("."))
    before, during = T0 + timedelta(hours=3), T0 + timedelta(hours=4, minutes=10)
    rows = [
        ("snapshot-test", "claude_code", before, before + timedelta(minutes=1), "ok"),
        ("snapshot-test", "claude_code", during, during + timedelta(minutes=1), "ok"),
        ("other-machine", "claude_code", during, during + timedelta(minutes=1), "ok"),
        ("sink", "episode_link", T0 + timedelta(hours=4), T0 + timedelta(hours=5), "ok"),
        ("sink", "episode_link", T0 + timedelta(hours=6), T0 + timedelta(hours=7), "error"),
    ]
    with connect(dsn) as conn:
        for row in rows:
            conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                         "finished_at, status, events, human) VALUES (%s, %s, %s, %s, %s, 0, 0)", row)
        conn.commit()
    assert Source(cfg).episodes_ready() == before
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES ('sink', 'episode_link', "
                     "%s, %s, 'ok', 0, 0)", (T0 + timedelta(hours=8), T0 + timedelta(hours=9)))
        conn.commit()
    assert Source(cfg).episodes_ready() == during


def test_a_rebuild_without_a_covered_machine_scan_does_not_use_another_episode(dsn):
    cfg = Config(machine="uncovered-test", agents=(), webhook_url=None, webhook_secret=None,
                 pg_dsn=dsn, home=Path("."))
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES ('sink', 'episode_link', "
                     "%s, %s, 'ok', 0, 0)", (T0 + timedelta(hours=20), T0 + timedelta(hours=21)))
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES ('uncovered-test', 'claude_code', "
                     "%s, %s, 'ok', 0, 0)",
                     (T0 + timedelta(hours=20, minutes=10), T0 + timedelta(hours=20, minutes=11)))
        conn.execute("INSERT INTO agents.episode VALUES (%s, %s, %s, 1, 1, 1, 0, 0, 'no_code', "
                     "'git', false)", (uuid.UUID("0f0f0f0f-0000-4000-8000-000000000004"),
                                       T0 + timedelta(hours=22), T0 + timedelta(hours=23)))
        conn.commit()
    assert Source(cfg).episodes_ready() is None


@pytest.mark.parametrize("agent", ["codex", "pi", "cursor_cli"])
def test_local_episode_readiness_uses_supported_collectors_without_expanding_the_teacher(dsn, agent):
    cfg = Config(machine=f"local-only-{agent}", agents=(agent,), webhook_url=None,
                 webhook_secret=None, pg_dsn=dsn, home=Path("."))
    scanned = T0 + timedelta(hours=29)
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES (%s, %s, %s, %s, 'ok', 0, 0)",
                     (cfg.machine, agent, scanned, scanned + timedelta(minutes=1)))
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES (%s, 'episode_link', "
                     "%s, %s, 'ok', 0, 0)",
                     (f"sink-{agent}", T0 + timedelta(hours=30), T0 + timedelta(hours=31)))
        conn.commit()
    assert Source(cfg, local_context=True).episodes_ready() == scanned
    assert Source(cfg).episodes_ready() is None


def test_local_episode_readiness_waits_for_stale_and_uncovered_mixed_collectors(dsn):
    cfg = Config(machine="mixed-collectors", agents=("claude_code", "codex", "pi"),
                 webhook_url=None, webhook_secret=None, pg_dsn=dsn, home=Path("."))
    claude, codex, pi = (T0 + timedelta(days=n) for n in (3, 4, 2))
    with connect(dsn) as conn:
        for collector, at, status in (("claude_code", claude, "ok"), ("codex", codex, "ok"),
                                      ("pi", codex, "error")):
            conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                         "finished_at, status, events, human) VALUES (%s, %s, %s, %s, %s, 0, 0)",
                         (cfg.machine, collector, at, at + timedelta(minutes=1), status))
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES ('sink-mixed', 'episode_link', "
                     "%s, %s, 'ok', 0, 0)", (T0 + timedelta(days=10), T0 + timedelta(days=10, hours=1)))
        conn.commit()
    assert Source(cfg, local_context=True).episodes_ready() is None
    assert Source(cfg).episodes_ready() == claude
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES (%s, 'pi', %s, %s, 'ok', 0, 0)",
                     (cfg.machine, pi, pi + timedelta(minutes=1)))
        conn.commit()
    assert Source(cfg, local_context=True).episodes_ready() == pi


def test_local_episode_readiness_waits_for_a_session_without_scan_receipts(dsn):
    cfg = Config(machine="missing-scan", agents=("claude_code", "pi"), webhook_url=None,
                 webhook_secret=None, pg_dsn=dsn, home=Path("."))
    scanned = T0 + timedelta(days=2)
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES (%s, 'claude_code', "
                     "%s, %s, 'ok', 0, 0)", (cfg.machine, scanned, scanned + timedelta(minutes=1)))
        conn.execute("INSERT INTO agents.collector_scan (machine, collector, started_at, "
                     "finished_at, status, events, human) VALUES ('sink-missing', 'episode_link', "
                     "%s, %s, 'ok', 0, 0)", (T0 + timedelta(days=12), T0 + timedelta(days=12, hours=1)))
        conn.execute("INSERT INTO agents.event_session (event_id,machine,agent,session_id,run_origin) "
                     "VALUES ('missing-scan-pi',%s,'pi','unscanned-session','interactive')", (cfg.machine,))
        conn.commit()
    assert Source(cfg, local_context=True).episodes_ready() is None
    assert Source(cfg).episodes_ready() == scanned


def test_inspect_day_means_a_member_row_that_day_not_span_overlap(dsn, episode):
    wide = uuid.UUID("0f0f0f0f-0000-4000-8000-000000000002")
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.episode VALUES (%s, %s, %s, 1, 1, 0, 0, 0, 'open', 'git', "
                     "false)", (wide, T0 - timedelta(days=5), T0 + timedelta(days=5)))
        conn.execute("INSERT INTO agents.episode_member VALUES (%s, 's9', 'seed', %s)", (wide, []))
        conn.commit()
    push.send_activity_rows(dsn, [_activity("s9a", "steer", "s9", via="typed")])
    a_quiet_day = (T0.astimezone() - timedelta(days=2)).date().isoformat()
    assert inspector.fetch(dsn, "day", a_quiet_day)["episodes"] == []
    assert str(wide) in [ep["id"] for ep in inspector.fetch(dsn, "day", DAY)["episodes"]]


@pytest.mark.parametrize("reopen_during_scoring", [False, True])
def test_mature_outcome_runner_reads_and_rechecks_real_episode_rows(
        dsn, tmp_path, monkeypatch, reopen_during_scoring):
    import json
    from dataclasses import replace
    from agent_usage_tracker.event import make_event_id
    from agent_usage_tracker.label import local
    from label_helpers import _env

    eid = uuid.uuid4()
    sid = f"sql-{eid}"
    at, end = T0, T0+timedelta(minutes=1)
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.episode VALUES (%s,%s,%s,1,1,1,0,0,'no_code','git',false)",
                     (eid, at, end))
        conn.execute("INSERT INTO agents.episode_member VALUES (%s,%s,'seed',%s)", (eid, sid, []))
        conn.commit()
    push.send_activity_rows(dsn, [ActivityEvent(event_id=make_event_id("claude_code", "steer", sid),
        ts=at.isoformat(), agent="claude_code", kind="steer", session_id=sid, turn_id=None,
        attrs={"via": "typed"}, machine="t")])
    directory = tmp_path / ".claude/projects/p"
    directory.mkdir(parents=True)
    (directory/f"{sid}.jsonl").write_text("\n".join(json.dumps(row) for row in [
        {"type": "user", "uuid": sid, "timestamp": at.isoformat(), "sessionId": sid,
         "message": {"content": "answer the synthetic question"}},
        {"type": "assistant", "uuid": sid+"-reply", "timestamp": end.isoformat(),
         "sessionId": sid, "message": {"content": "synthetic answer complete"}}]))
    cfg = Config(machine="t", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=dsn,
                 home=tmp_path, repo_key_salt="synthetic-sql-salt", label_mature_outcomes=True)
    row = {"minimum_score": .5, "source_eval_sha256": "a"*64,
           "recipe": {"id": "synthetic", "members": []}}
    monkeypatch.setattr(label_run, "load_policy", lambda: ({}, "b"*64))
    scored = []

    class Model(local.LocalBackend):
        def __init__(self):
            super().__init__("soft_outcome", row, "b"*64, cfg)

        def prepare_requests(self, requests, deadline):
            pass

        def distribution(self, request):
            scored.append(request.unit_id)
            if reopen_during_scoring:
                with connect(dsn) as conn:
                    conn.execute("UPDATE agents.episode SET outcome='shipped' WHERE episode_id=%s", (eid,))
                    conn.commit()
            return {candidate["id"]: float(candidate["id"] == "resolved")
                    for candidate in request.candidates}

    with _env(tmp_path):
        now = end+timedelta(days=40)
        disabled = replace(cfg, label_mature_outcomes=False)
        assert label_run.run_label(disabled, ["soft_outcome"], local_estimates=True, now=now,
            backend_for=lambda task, choice: Model())["soft_outcome"]["rows"] == 0
        result = label_run.run_label(cfg, ["soft_outcome"], local_estimates=True, now=now,
            backend_for=lambda task, choice: Model())["soft_outcome"]
        assert scored == [str(eid)]
        with connect(dsn) as conn:
            labels = conn.execute("SELECT attrs->>'label' FROM agents.activity_event WHERE "
                "kind='label' AND attrs->>'task'='working.local.soft_outcome' "
                "AND attrs->>'unit_id'=%s", (str(eid),)).fetchall()
        assert labels == ([] if reopen_during_scoring else [("resolved",)])
        assert result["rows"] == (0 if reopen_during_scoring else 1)
        if not reopen_during_scoring:
            result = label_run.run_label(cfg, ["soft_outcome"], local_estimates=True,
                now=now+timedelta(days=1), backend_for=lambda task, choice: Model())["soft_outcome"]
            assert result["rows"] == 0 and scored == [str(eid)]


# Last in the file: the refresh job rebuilds every derived view and records a sink scan.
def test_refresh_deletes_claude_code_relogs_and_keeps_other_duplicates(dsn):
    session = EventSession("s-relog", None, "interactive", "t1")

    def usage(eid: str, ts: datetime, agent: str, tokens: int) -> UsageEvent:
        return UsageEvent(event_id=eid, ts=ts.isoformat(), agent=agent, model="m", provider="p",
                          input_tokens=tokens, total_tokens=tokens, machine="t", session=session)

    later = T0 + timedelta(seconds=3, microseconds=123456)
    old = T0 - timedelta(days=60)      # plain history in another chunk
    push.insert_pg(dsn, [usage(f"old{i}", old + timedelta(seconds=i), "claude_code", 1)
                         for i in range(400)]
                   + [usage("relog", T0 + timedelta(microseconds=654321), "claude_code", 10), usage("relog", later, "claude_code", 12),
                      usage("odd", T0, "codex", 5), usage("odd", later, "codex", 5)])
    job = (SINK_STACK / "sql" / "refresh.sql").read_text(encoding="utf-8").splitlines()
    with connect(dsn) as conn:
        conn.execute("SELECT compress_chunk(c, if_not_compressed => true) "
                     "FROM show_chunks('agents.usage_event') c")
        conn.commit()
        # A delete that decompresses the old chunk's 400 rows fails past this limit.
        conn.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 200")
        # Kolkata prints as IST, which Postgres reads back as UTC+2: the keys must not round-trip.
        conn.execute("SET DateStyle = 'SQL, DMY'")
        conn.execute("SET TimeZone = 'Asia/Kolkata'")
        for _ in range(2):             # the second run finds nothing left to delete
            conn.execute("\n".join(line for line in job if not line.startswith("\\")))
            conn.commit()
        rows = conn.execute("SELECT event_id, total_tokens FROM agents.usage_event "
                            "WHERE event_id IN ('relog', 'odd') ORDER BY 1, 2").fetchall()
        assert conn.execute("SELECT count(*) FROM agents.usage_event "
                            "WHERE event_id LIKE 'old%'").fetchone()[0] == 400
    # the re-log keeps the copy usage_event_dedup keeps; a Codex duplicate stays for the audit
    assert rows == [("odd", 5), ("odd", 5), ("relog", 12)]


def test_improve_publish_sql_retries_latest_zeros_grants_and_dashboard(dsn):
    from psycopg.errors import InsufficientPrivilege
    reapplied = subprocess.run(["sh", str(SINK_STACK / "apply.sh")], env=_psql_env(dsn),
                               capture_output=True, text=True)
    assert reapplied.returncode == 0, reapplied.stderr[-4000:]
    row = ("2026-11-20", "avoidable_recovery", 1, "codex", "control", 1, 2, 3, False, T0)
    adoption = ("0123456789abcdef", T0, "Use the CI test entrypoint", "instruction",
                ["avoidable_recovery"], 53)
    with connect(dsn) as conn:
        conn.execute("SET ROLE agents_writer")
        for _ in range(2):
            conn.execute(improve_publish.SCORE_SQL, row)
            conn.execute(improve_publish.ADOPT_SQL, adoption)
        conn.execute(improve_publish.SCORE_SQL, (*row[:5], 0, 0, 0, False, T0 + timedelta(seconds=1)))
        conn.execute("RESET ROLE")
        conn.execute("SET ROLE agents_ro")
        assert conn.execute("SELECT passed, failed, unknown FROM agents.improve_score_latest "
                            "WHERE rubric_id = 'avoidable_recovery'").fetchall() == [(0, 0, 0)]
        assert conn.execute("SELECT count(*) FROM agents.improve_adoption").fetchone() == (1,)
        for sql in ("SELECT * FROM agents.improve_score_daily",
                    "INSERT INTO agents.improve_adoption(adoption_id) VALUES ('forbidden')"):
            with pytest.raises(InsufficientPrivilege), conn.transaction():
                conn.execute(sql)
        dashboard = json.loads((SINK_STACK / "grafana/dashboards/agents-effort.json").read_text())
        queries = [t["rawSql"] for p in dashboard["panels"][-2:] for t in p["targets"]]
        queries += [dashboard["annotations"]["list"][-1]["target"]["rawSql"]]
        for sql in queries:
            sql = sql.replace("$__timeFrom()", "'2026-01-01'::timestamptz")
            sql = sql.replace("$__timeTo()", "'2027-01-01'::timestamptz")
            sql = sql.replace("$__timeFilter(adopted_at)", "adopted_at >= '2026-01-01'")
            conn.execute(sql).fetchall()


def test_shipped_price_panels_reconcile_with_local_summary(dsn, tmp_path, monkeypatch):
    rows = price_rows()
    push.insert_pg(dsn, rows)
    push.insert_pg(dsn, rows)
    path = tmp_path / "prices.json"
    assert pricing.sync_prices(path, pg_dsn=dsn, fetch=lambda _: CATALOG) == 3
    rates, _ = pricing.read_price_snapshot(path)
    with connect(dsn) as conn:
        synced_at = conn.execute(
            "SELECT synced_at FROM agents.model_price WHERE model_key = 'model-a'"
        ).fetchone()[0]
    pricing.load_sink_prices(dsn, rates, synced_at)
    pricing.load_sink_prices(dsn, {"model-a": {"output_cost": 99}}, synced_at - timedelta(days=1))
    before = path.read_bytes()
    with pytest.raises(AutError, match="keeping the existing"):
        pricing.sync_prices(path, pg_dsn=dsn, fetch=lambda _: None)
    assert path.read_bytes() == before
    cfg = price_config(tmp_path, monkeypatch, rows + rows)
    report = summary.build_summary(cfg, since=PRICE_DAY, until=PRICE_DAY + timedelta(days=1),
                                   prices_path=path)
    totals = report["totals"]["pricing"]
    assert report["totals"]["events"] == 3
    assert totals["estimated_api_cost_usd"] is None

    dashboard = json.loads((SINK_STACK / "grafana/dashboards/agents-usage.json").read_text())
    panel_sql = {panel["id"]: panel["targets"][0]["rawSql"] for panel in dashboard["panels"]
                 if 24 <= panel["id"] <= 29}

    def run_panel(conn, panel_id):
        # Substitute Grafana's macros; execute the query from the shipped dashboard.
        sql = panel_sql[panel_id].replace(
            "$__timeFilter(ts)",
            "ts BETWEEN '2026-06-15 00:00:00+00' AND '2026-06-15 23:59:59+00'",
        )
        for field, values in (
            ("machine", "'pricing-fixture'"),
            ("agent", "'claude_code'"),
            ("model", f"'{PRICE_MODEL}','model-b','codex-auto-review'"),
        ):
            sql = sql.replace("${" + field + ":sqlstring}", values)
        return conn.execute(sql).fetchall()

    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT count(*), min(synced_at), max(synced_at) FROM agents.model_price"
        ).fetchone() == (3, synced_at, synced_at)
        assert conn.execute(
            "SELECT has_table_privilege('agents_writer', 'agents.model_price', 'INSERT'), "
            "has_table_privilege('agents_ro', 'agents.model_price', 'UPDATE')"
        ).fetchone() == (False, False)
        conn.execute("SET ROLE agents_ro")
        assert conn.execute(
            "SELECT agents.price_model_key(%s), agents.price_model_key(%s), "
            "agents.price_model_key(%s)", (PRICE_MODEL, "abc@", "@")
        ).fetchone() == tuple(pricing.normalize_model_key(key) for key in (PRICE_MODEL, "abc@", "@"))
        projected = conn.execute("""
            SELECT model, coverage_tokens, priced_tokens, unpriced_tokens, unattributed_tokens,
                   known_api_cost_usd, estimated_api_cost_usd, logged_cost_usd
            FROM agents.usage_price
            WHERE machine = 'pricing-fixture' ORDER BY model
        """).fetchall()
        local = {m["model"]: m for m in report["prices_by_model"]}
        assert len(projected) == len(local) == 3
        for model, denom, priced, unpriced, unattributed, known, complete, logged in projected:
            rec = local[model]
            assert (denom, priced, unpriced, unattributed) == (
                rec["coverage_tokens"], rec["priced_tokens"], rec["unpriced_tokens"],
                rec["unattributed_tokens"])
            assert known == (Decimal(rec["known_api_cost_usd"])
                             if rec["known_api_cost_usd"] is not None else None)
            assert complete == (Decimal(rec["estimated_api_cost_usd"])
                                if rec["estimated_api_cost_usd"] is not None else None)
            assert logged == (Decimal("0.125") if model == PRICE_MODEL else None)
        assert run_panel(conn, 24) == [(Decimal(totals["known_api_cost_usd"]),)]
        assert float(run_panel(conn, 25)[0][0]) == pytest.approx(80 / 97)
        assert run_panel(conn, 26) == [(17,)]
        assert run_panel(conn, 27)[0][-1] == Decimal("1.01")
        table_model = {row[0]: row for row in run_panel(conn, 28)}
        assert table_model["model-b"][1:8] == (1, 22, 15, 7, Decimal("0.4"), None, None)
        assert table_model["codex-auto-review"][1:8] == (1, 10, 0, 10, None, None, None)
        aggregate, = run_panel(conn, 29)
        assert aggregate[:8] == ("claude_code", 3, 97, 80, 17, Decimal("1.01"), None,
                                Decimal("0.125"))
        assert aggregate[8] is not None
        conn.execute("RESET ROLE")
        assert conn.execute(
            "SELECT count(*) FILTER (WHERE est_cost_usd IS NULL), sum(est_cost_usd) "
            "FROM agents.usage_cost WHERE event_id LIKE 'price-%'"
        ).fetchone() == (2, Decimal("0.61"))
