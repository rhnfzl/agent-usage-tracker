"""Emit local outbox notices only from a fresh cached SQLite count."""

import argparse
from datetime import datetime, time, timedelta, timezone
import json
import sqlite3

from agent_usage_tracker import coverage, operational_status as status, push, quota_cli
from agent_usage_tracker.config import Config
from agent_usage_tracker.event import ActivityEvent

T0 = datetime(2026, 6, 15, 22, tzinfo=timezone.utc)


def _publish(cfg, at):
    report = coverage.build_report(cfg, now=at)
    coverage.save_report(report)
    return report


def _queue():
    push._queue_rest([ActivityEvent("synthetic-row", T0.isoformat(), "claude_code",
                                    "steer", None, None, {})])


def test_pending_delivery_alert_lifecycle_reads_saved_outbox_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    cfg = Config("fixture", (), None, None, None, tmp_path)
    inbox = status.delivery_notices_path()
    _queue()
    report = _publish(cfg, T0)
    assert any(issue["code"] == "delivery_backlog" for issue in report["issues"])
    # The existing watchdog-facing coverage alert omits this issue even after baseline.
    assert coverage.alert_state(report, {"alerts": {}}, T0) == ({}, None)
    assert status.delivery_evidence(now=T0)["pending_rows"] == 1
    before = push.outbox_path().read_bytes()
    assert status.check_delivery_notices(now=T0, quiet_hours=(time(22), time(7))) == []
    assert not inbox.exists()
    now = T0 + timedelta(hours=9)
    warning, = status.check_delivery_notices(now=now, quiet_hours=(time(22), time(7)))
    assert (warning["kind"], warning["pending_rows"]) == ("delivery_backlog", 1)
    initial = inbox.read_bytes()
    assert status.check_delivery_notices(now=now) == []
    assert initial == inbox.read_bytes()
    assert push.outbox_path().read_bytes() == before

    # A missing file can be reported as an empty queue in status, but proves no recovery.
    hidden = push.outbox_path().with_suffix(".fixture")
    push.outbox_path().rename(hidden)
    now += timedelta(minutes=1)
    absent = _publish(cfg, now)
    assert absent["delivery"] == {"status": "available", "pending": {}}
    assert status.delivery_evidence(now=now)["freshness"] == "unknown"
    assert status.check_delivery_notices(now=now) == []
    assert initial == inbox.read_bytes()

    # A crash or downtime can leave an old zero on disk. It also cannot close the episode.
    hidden.rename(push.outbox_path())
    with sqlite3.connect(push.outbox_path()) as conn:
        conn.execute("DELETE FROM pending")
    now += timedelta(minutes=1)
    report = _publish(cfg, now)
    assert report["delivery"]["evidence"] == "outbox_snapshot"
    late = now + timedelta(hours=coverage.FRESH_HOURS, seconds=1)
    assert status.delivery_evidence(now=late)["freshness"] == "stale"
    assert status.check_delivery_notices(now=late) == []
    assert initial == inbox.read_bytes()

    _publish(cfg, late)
    recovered, = status.check_delivery_notices(now=late)
    assert (recovered["kind"], recovered["pending_rows"]) == ("delivery_recovered", 0)
    assert status.check_delivery_notices(now=late) == []
    _queue()
    late += timedelta(minutes=1)
    _publish(cfg, late)
    recurring, = status.check_delivery_notices(now=late)
    assert recurring["kind"] == "delivery_backlog"
    assert status.read_delivery_notices() == [warning, recovered, recurring]


def test_older_or_unknown_evidence_cannot_recover_backlog(tmp_path):
    cache, inbox = tmp_path / "coverage.json", tmp_path / "inbox.json"

    def write(at, pending, evidence="outbox_snapshot", queue_status="available"):
        cache.write_text(json.dumps({"version": 1, "generated_at": at.isoformat(),
                                    "delivery": {"status": queue_status,
                                                 "pending": {"activity": pending},
                                                 "evidence": evidence}}))

    at = T0 + timedelta(minutes=1)
    write(at, 2)
    assert status.check_delivery_notices(path=inbox, coverage_path=cache, now=at)
    latest = at + timedelta(minutes=1)
    write(latest, 2)
    assert not status.check_delivery_notices(path=inbox, coverage_path=cache, now=latest)
    before = inbox.read_bytes()
    for stamp, pending, evidence, queue_status in (
        (at, 0, "outbox_snapshot", "available"),
        (latest, 0, "outbox_snapshot", "unknown"),
        (latest, False, "outbox_snapshot", "available"),
        (latest, 0, None, "available"),
        (latest + timedelta(minutes=1), 0, "outbox_snapshot", "available"),
    ):
        write(stamp, pending, evidence, queue_status)
        assert not status.check_delivery_notices(path=inbox, coverage_path=cache, now=latest)
        assert inbox.read_bytes() == before


def test_registered_delivery_notices_require_explicit_opt_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    _queue()
    now = datetime.now(timezone.utc)
    _publish(Config("fixture", (), None, None, None, tmp_path), now)
    parser = argparse.ArgumentParser()
    quota_cli.register(parser.add_subparsers())
    quota_cli.run_delivery_notices(parser.parse_args(["delivery-notices", "check"]))
    assert json.loads(capsys.readouterr().out)["pending_rows"] == 1
    assert not status.delivery_notices_path().exists()
    quota_cli.run_delivery_notices(parser.parse_args(["delivery-notices", "check", "--notify"]))
    assert json.loads(capsys.readouterr().out)[0]["kind"] == "delivery_backlog"
    quota_cli.run_delivery_notices(parser.parse_args(["delivery-notices", "inbox"]))
    assert len(json.loads(capsys.readouterr().out)) == 1
    quota_cli.run(parser.parse_args(["quota", "inbox"]))
    assert json.loads(capsys.readouterr().out) == []
