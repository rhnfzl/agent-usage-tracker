"""The sink layer's SQL, run against a real Postgres. Skipped unless AUT_TEST_PG_DSN is set.

Run: `AUT_TEST_PG_DSN=postgresql://... pytest tests/pipeline/test_sink_sql.py`. The DSN must be a scratch
database: the tests create the `agents` schema, and refuse to touch a database that already has one.

STAND_IN_SCHEMA is this file's own, written from CONTRACT.md and the columns the code names: the
deployed DDL lives in the infrastructure repo (DEPLOY.md). So this catches invalid SQL, a wrong
column and an ON CONFLICT target with no matching key against the stand-in. It proves nothing about
the deployed schema; tests/pipeline/test_sink.py only checks the stand-in names what CONTRACT.md names.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_usage_tracker import episode_store, inspector, outcomes, push
from agent_usage_tracker.config import Config
from agent_usage_tracker.event import (ActivityEvent, EventSession, TimeInterval,
                                       UsageEvent)
from agent_usage_tracker.label import run as label_run
from agent_usage_tracker.label.tasks import Source
from agent_usage_tracker.sink import connect

DSN = os.environ.get("AUT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="AUT_TEST_PG_DSN is not set")

STAND_IN_SCHEMA = """
CREATE SCHEMA agents;
CREATE TABLE agents.usage_event (
    event_id text, ts timestamptz, machine text, agent text, kind text, model text, provider text,
    input_tokens bigint, output_tokens bigint, cache_read_tokens bigint, cache_write_tokens bigint,
    reasoning_tokens bigint, total_tokens bigint, est_cost_usd numeric, project text,
    PRIMARY KEY (event_id, ts));
CREATE TABLE agents.event_session (
    event_id text PRIMARY KEY, machine text, agent text, session_id text, parent_session_id text,
    run_origin text, turn_id text);
CREATE TABLE agents.activity_event (
    event_id text, ts timestamptz, machine text, agent text, kind text, session_id text,
    turn_id text, attrs jsonb, PRIMARY KEY (event_id, ts));
CREATE TABLE agents.time_interval (
    event_id text, ts timestamptz, end_ts timestamptz, machine text, source text, app text,
    title text, category text, work_relevant boolean, attrs jsonb, PRIMARY KEY (event_id, ts));
CREATE TABLE agents.collector_scan (
    machine text, collector text, started_at timestamptz, finished_at timestamptz, status text,
    events integer, human integer, newest_ts timestamptz, error text, aut_version text,
    PRIMARY KEY (machine, collector, finished_at));
CREATE TABLE agents.commit_outcome (
    sha text, repo_key text, state text, survival numeric, decided_at timestamptz,
    PRIMARY KEY (sha, repo_key, decided_at));
CREATE TABLE agents.manual_outcome (session_id text, outcome text, ts timestamptz, repo_key text);
CREATE TABLE agents.episode (
    episode_id uuid PRIMARY KEY, first_ts timestamptz, last_ts timestamptz, n_sessions integer,
    n_seed_sessions integer, n_steers integer, n_commits integer, n_prs integer, outcome text,
    outcome_source text, confirmed boolean);
CREATE TABLE agents.episode_member (
    episode_id uuid, session_id text, role text, repo_keys text[]);
CREATE TABLE agents.episode_correction (
    id uuid PRIMARY KEY, ts timestamptz, action text, a_session text, b_session text, attrs jsonb);
CREATE TABLE agents.daily_note (
    day date PRIMARY KEY, text text, model_id text, created_at timestamptz);
"""

T0 = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
DAY = T0.astimezone().date().isoformat()
EPISODE = uuid.UUID("0f0f0f0f-0000-4000-8000-000000000001")


@pytest.fixture(scope="module")
def dsn():
    with connect(DSN) as conn:
        if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'agents'").fetchone():
            pytest.fail("AUT_TEST_PG_DSN already has an agents schema: use a scratch database")
        conn.execute(STAND_IN_SCHEMA)
        conn.commit()
    try:
        yield DSN
    finally:
        with connect(DSN) as conn:
            conn.execute("DROP SCHEMA agents CASCADE")
            conn.commit()


def _rows(dsn: str, table: str) -> int:
    with connect(dsn) as conn:
        return conn.execute(f"SELECT count(*) FROM agents.{table}").fetchone()[0]


def _activity(eid: str, kind: str, session: str = "s1", **attrs) -> ActivityEvent:
    return ActivityEvent(event_id=eid, ts=T0.isoformat(), agent="claude_code", kind=kind,
                         session_id=session, turn_id=None, attrs=attrs, machine="t")


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
        conn.execute("INSERT INTO agents.episode VALUES (%s, %s, %s, 1, 1, 1, 1, 0, 'merged', "
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
    for text in ("first", "second"):   # the first draft is kept
        label_run._insert_note(dsn, date(2026, 6, 15), text, "m")
    with connect(dsn) as conn:
        assert conn.execute("SELECT text FROM agents.daily_note").fetchall() == [("first",)]


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
