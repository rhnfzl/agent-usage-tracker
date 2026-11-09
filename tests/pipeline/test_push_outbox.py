"""Webhook-only scans keep every unsupported sink row until Postgres accepts it."""

import os
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_usage_tracker import push
from agent_usage_tracker.event import TimeInterval
from fake_psycopg import _fake_psycopg
from test_sink import _act, _cfg, _tables, _usage


def _queued(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(push, "post_webhook", lambda *args: None)
    cfg = _cfg(dsn=None)
    rows = [_usage(), _act(), TimeInterval(
        "t1", "2026-07-18T09:00:00Z", "2026-07-18T10:00:00Z", "aw", "Editor",
        "private title", "editor", True, {}, "test")]
    push.make_sender(cfg)(rows)
    return cfg, rows


def test_webhook_only_rows_survive_restart_and_replay(tmp_path, monkeypatch, capsys):
    cfg, rows = _queued(tmp_path, monkeypatch)
    push.make_sender(cfg)(rows)
    assert push.outbox_status() == {"session": 1, "activity": 1, "time": 1}
    assert "queued" in capsys.readouterr().err
    if os.name != "nt":
        assert push.outbox_path().stat().st_mode & 0o777 == 0o600
        assert push.outbox_path().parent.stat().st_mode & 0o777 == 0o700
    log = []
    with _fake_psycopg(log):
        push.make_sender(replace(cfg, pg_dsn="postgresql://x"))([])
    assert _tables(log) == ["agents.event_session", "agents.activity_event",
                            "agents.time_interval", "commit"]
    assert log[0][1][0][0] == rows[0].event_id
    assert log[2][1][0][6] == "private title"
    assert push.outbox_status() == {}


def test_failed_replay_keeps_the_whole_transaction(tmp_path, monkeypatch):
    cfg, _ = _queued(tmp_path, monkeypatch)
    with _fake_psycopg([], fail_on="activity_event"), pytest.raises(RuntimeError):
        push.replay_outbox(replace(cfg, pg_dsn="postgresql://x"))
    assert sum(push.outbox_status().values()) == 3
    with _fake_psycopg([]):
        assert push.replay_outbox(replace(cfg, pg_dsn="postgresql://x")) == 3
    assert push.outbox_status() == {}


def test_queue_failure_prevents_ack(tmp_path, monkeypatch):
    cfg, _ = _queued(tmp_path, monkeypatch)
    push.outbox_path().write_bytes(b"not a sqlite database")
    with pytest.raises(push.DeliveryError, match="outbox"):
        push.make_sender(cfg)([_act()])


def test_concurrent_writers_keep_all_rows_and_deduplicate(tmp_path, monkeypatch):
    cfg, _ = _queued(tmp_path, monkeypatch)

    def send(i):
        push.make_sender(cfg)([replace(_act(), event_id=f"concurrent{i}")])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(send, list(range(40)) * 2))
    assert push.outbox_status() == {"session": 1, "activity": 41, "time": 1}


def test_replay_uses_bounded_batches(tmp_path, monkeypatch):
    cfg, _ = _queued(tmp_path, monkeypatch)
    push.make_sender(cfg)([replace(_act(), event_id=f"many{i}") for i in range(1200)])
    log = []
    with _fake_psycopg(log):
        assert push.replay_outbox(replace(cfg, pg_dsn="postgresql://x")) == 1203
    assert max(len(rows) for entry in log if entry != "commit" for rows in [entry[1]]) <= 500
    assert _tables(log).count("commit") == 3
    with sqlite3.connect(push.outbox_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


def test_stale_queued_snapshot_cannot_replace_full_coverage(tmp_path, monkeypatch):
    cfg, _ = _queued(tmp_path, monkeypatch)
    full = replace(_act(), event_id="turn1", kind="turn", attrs={"tool_calls": 2, "tool_errors": 1,
                                               "revision_sources": ["a", "b"]})
    stale = replace(full, attrs={"tool_calls": 1, "tool_errors": 0,
                                "revision_sources": ["a"]})
    push.make_sender(cfg)([full])
    push.make_sender(cfg)([stale])
    push.make_sender(cfg)([replace(stale, attrs={"tool_calls": 1})])
    with sqlite3.connect(push.outbox_path()) as conn:
        row = conn.execute("SELECT payload FROM pending WHERE kind='activity' AND event_id=?",
                           (full.event_id,)).fetchone()
    assert json.loads(row[0])["attrs"] == full.attrs


def test_pg_commit_then_local_ack_failure_can_retry(tmp_path, monkeypatch):
    cfg, rows = _queued(tmp_path, monkeypatch)
    cfg = replace(cfg, pg_dsn="postgresql://x")
    with sqlite3.connect(push.outbox_path()) as conn:
        conn.execute("CREATE TRIGGER refuse_ack BEFORE DELETE ON pending "
                     "BEGIN SELECT RAISE(ABORT, 'simulated crash before local ack'); END")
    first = []
    with _fake_psycopg(first), pytest.raises(push.DeliveryError):
        push.replay_outbox(cfg)
    assert first[-1] == "commit"
    assert sum(push.outbox_status().values()) == 3
    with sqlite3.connect(push.outbox_path()) as conn:
        conn.execute("DROP TRIGGER refuse_ack")
    again = []
    with _fake_psycopg(again):
        assert push.replay_outbox(cfg) == 3
    assert again == first
    assert again[0][1][0][0] == rows[0].event_id


def test_sender_replays_once_per_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(push, "replay_outbox", lambda cfg: calls.append(cfg) or 0)
    send = push.make_sender(_cfg())
    send([])
    send([])
    assert len(calls) == 1
