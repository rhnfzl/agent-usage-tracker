"""The sink layer's SQL, run against a real Postgres. Skipped unless AUT_TEST_PG_DSN is set.

Run: `AUT_TEST_PG_DSN=postgresql://... pytest tests/pipeline/test_sink_sql.py`. The DSN must be a scratch
database: the tests create the `agents` schema, and refuse to touch a database that already has one.

STAND_IN_SCHEMA is this file's own copy of the tables the code touches, with the keys, NOT NULLs and
CHECKs of the deployed DDL in the infrastructure repo (DEPLOY.md), checked against it on 2026-11-05.
It leaves out Timescale, the views, column defaults, numeric precision and event_session.first_seen.
So this catches invalid SQL, a wrong column, a refused value and an ON CONFLICT target with no
matching key. It cannot notice the deployed DDL changing later; tests/pipeline/test_sink.py only
checks the stand-in names what CONTRACT.md names.
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
    event_id text, ts timestamptz, machine text NOT NULL, agent text NOT NULL, kind text NOT NULL,
    model text, provider text, input_tokens bigint NOT NULL, output_tokens bigint NOT NULL,
    cache_read_tokens bigint NOT NULL, cache_write_tokens bigint NOT NULL, reasoning_tokens bigint,
    total_tokens bigint NOT NULL, est_cost_usd numeric, project text, PRIMARY KEY (event_id, ts));
CREATE TABLE agents.event_session (
    event_id text PRIMARY KEY, machine text NOT NULL, agent text NOT NULL, session_id text NOT NULL,
    parent_session_id text, run_origin text NOT NULL, turn_id text);
CREATE TABLE agents.activity_event (
    event_id text, ts timestamptz, machine text NOT NULL, agent text NOT NULL, kind text NOT NULL,
    session_id text, turn_id text, attrs jsonb NOT NULL, PRIMARY KEY (event_id, ts));
CREATE TABLE agents.time_interval (
    event_id text, ts timestamptz, end_ts timestamptz NOT NULL, machine text NOT NULL,
    source text NOT NULL, app text, title text, category text, work_relevant boolean NOT NULL,
    attrs jsonb NOT NULL, PRIMARY KEY (event_id, ts));
CREATE TABLE agents.collector_scan (
    machine text, collector text, started_at timestamptz NOT NULL, finished_at timestamptz,
    status text NOT NULL, events integer NOT NULL, human integer NOT NULL, newest_ts timestamptz,
    error text, aut_version text, PRIMARY KEY (machine, collector, finished_at));
CREATE TABLE agents.commit_outcome (
    sha text, repo_key text, state text, survival numeric, decided_at timestamptz,
    PRIMARY KEY (sha, repo_key, state, decided_at));
CREATE TABLE agents.manual_outcome (
    session_id text, outcome text NOT NULL, ts timestamptz, repo_key text,
    PRIMARY KEY (session_id, ts));
CREATE TABLE agents.episode (
    episode_id uuid PRIMARY KEY, first_ts timestamptz NOT NULL, last_ts timestamptz NOT NULL,
    n_sessions integer NOT NULL, n_seed_sessions integer NOT NULL, n_steers integer NOT NULL,
    n_commits integer NOT NULL, n_prs integer NOT NULL, outcome text NOT NULL,
    outcome_source text NOT NULL, confirmed boolean NOT NULL);
CREATE TABLE agents.episode_member (
    episode_id uuid REFERENCES agents.episode (episode_id) ON DELETE CASCADE, session_id text,
    role text NOT NULL, repo_keys text[] NOT NULL, PRIMARY KEY (episode_id, session_id));
CREATE TABLE agents.episode_correction (
    id uuid PRIMARY KEY, ts timestamptz NOT NULL, action text NOT NULL, a_session text NOT NULL,
    b_session text, attrs jsonb NOT NULL);
CREATE TABLE agents.daily_note (
    day date PRIMARY KEY, text text NOT NULL, model_id text NOT NULL, created_at timestamptz NOT NULL);
ALTER TABLE agents.event_session ADD CHECK (run_origin IN ('interactive', 'automated', 'subagent', 'unknown'));
ALTER TABLE agents.time_interval ADD CHECK (source IN ('aw', 'manual')), ADD CHECK (end_ts > ts);
ALTER TABLE agents.collector_scan ADD CHECK (status IN ('ok', 'error'));
ALTER TABLE agents.commit_outcome ADD CHECK (survival BETWEEN 0 AND 1), ADD CHECK (state IN (
    'merged', 'reverted', 'reworked', 'open', 'abandoned', 'superseded', 'undecided'));
ALTER TABLE agents.manual_outcome ADD CHECK (outcome IN ('shipped', 'ruled_out', 'open'));
ALTER TABLE agents.episode ADD CHECK (outcome_source IN ('git', 'manual')), ADD CHECK (outcome IN (
    'shipped', 'reworked', 'open', 'ruled_out', 'no_code', 'undecided'));
ALTER TABLE agents.episode_member ADD CHECK (role IN ('seed', 'child'));
ALTER TABLE agents.episode_correction ADD CHECK (action IN ('merge', 'split', 'confirm', 'reject'));
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


def test_a_degraded_heartbeat_does_not_lose_the_scan_on_the_deployed_check(dsn):
    cfg = Config(machine="deg", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=dsn,
                 home=Path("."))
    push.send_scan_rows(dsn, [push.scan_row(cfg, "aw", T0, status="degraded", error="OSError"),
                              push.scan_row(cfg, "pi", T0)])
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
