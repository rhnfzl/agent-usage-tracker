"""Documented Claude statusline payload, private cached reads and local notice transitions."""

from __future__ import annotations

import argparse
import io
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

from agent_usage_tracker import coverage, observations, operational_status, quota, quota_cli
from agent_usage_tracker.config import AutError
from agent_usage_tracker.observation_report import build_report, public_draft

T0 = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
SCOPE = "269861f9-62d4-4db0-befb-85130ba5ead2"
OTHER = "caaa6ef7-9ba3-4571-8309-f3d4a353236a"
RESET = T0 + timedelta(hours=3)
FIXTURE = Path(__file__).parents[1] / "fixtures/claude-statusline-quota.json"


def payload(percent, reset=RESET, window="five_hour"):
    return {"rate_limits": {window: {
        "used_percentage": percent, "resets_at": int(reset.timestamp()) if reset else None,
    }}}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    return tmp_path / "observe.sqlite3", tmp_path / "quota-cache.json"


def capture(store, data, at, scope=SCOPE):
    db, cache = store
    return quota.capture_claude_quota(data, scope=scope, db=db, cache=cache, observed_at=at)


def window_at(store, at, window="five_hour", scope=SCOPE):
    result = quota.quota_snapshot(store[1], now=at)
    assert result["cache_status"] == "available"
    return next(row for row in result["windows"] if row["scope"] == quota.quota_scope(scope, window))


def test_documented_payload_maps_to_private_observation_store_and_replays(store):
    raw = json.loads(FIXTURE.read_text())
    assert capture(store, raw, T0)["accepted"] == 2
    contents = store[1].read_bytes()
    assert capture(store, raw, T0)["duplicates"] == 2
    assert capture(store, raw, T0 + timedelta(minutes=5))["unchanged"] == 2
    assert store[1].read_bytes() == contents
    report = build_report(store[0], now=T0.isoformat())
    rows = {r["period"]: r for r in report["quotas"]}
    assert set(rows) == {"five_hour", "seven_day"}
    assert rows["five_hour"]["unit"] == "basis_points"
    assert rows["five_hour"]["remaining"] == 7650 and rows["five_hour"]["limit"] == 10000
    result = window_at(store, T0)
    assert result["used_percent"] == 23.5 and result["pace_reason"] == "single_sample"
    assert result["window"] == "five_hour" and result["observed_at"] == T0.isoformat()
    assert result["expires_at"] == (T0 + quota.FRESH_FOR).isoformat()
    for private in (SCOPE, "synthetic-session-to-discard", "/synthetic/private/project", "used_usd"):
        assert private not in json.dumps(report) and private.encode() not in contents
    assert "quota" not in public_draft(report)


@pytest.mark.parametrize("quota_first", (False, True))
def test_statusline_quota_coexists_with_primary_claude_usage(store, quota_first):
    def import_usage():
        return observations.ingest(store[0], [{
            "version": 1, "kind": "usage", "id": "claude-usage",
            "timestamp": T0.isoformat(), "output_tokens": 4,
        }], source="claude_code", stream="usage", role="primary")

    if not quota_first:
        assert import_usage()["accepted"] == 1
    assert capture(store, payload(50), T0)["accepted"] == 1
    if quota_first:
        assert import_usage()["accepted"] == 1
    report = build_report(store[0], now=T0.isoformat())
    assert report["coverage"]["additive_usage"] == 1
    [record] = report["quotas"]
    assert record["source"] == "claude_code" and record["remaining"] == 5000


@pytest.mark.parametrize("percent", (None, 0))
def test_missing_or_zero_fields_stay_distinct(store, percent):
    assert capture(store, payload(percent, RESET if percent is None else None), T0)["accepted"] == 1
    value = window_at(store, T0)
    assert value["used_percent"] == percent
    assert value["coverage"] == "partial"
    assert value["pace_reason"] == "missing_percent_or_reset"
    assert value["reset_at"] == (RESET.isoformat() if percent is None else None)
    assert capture(store, {}, T0 + timedelta(minutes=1))["missing_windows"] == 2
    assert window_at(store, T0 + timedelta(minutes=1)) == value


@pytest.mark.parametrize("bad", (True, -1, 101, float("nan"), "35"))
def test_bad_percentage_writes_nothing(store, bad):
    with pytest.raises(ValueError, match="percentage"):
        capture(store, payload(bad), T0)
    assert not any(path.exists() for path in store)


def test_pace_compares_only_fresh_samples_in_one_scope_and_reset(store):
    first, next_at = T0, T0 + timedelta(minutes=15)
    capture(store, payload(25), first)
    capture(store, payload(77), first, OTHER)
    capture(store, payload(35), next_at)
    observed = window_at(store, next_at)
    assert observed["pace_percent_per_hour"] == 40
    assert observed["reaches_limit_before_reset"] is True
    assert window_at(store, next_at, scope=OTHER)["pace_reason"] == "single_sample"
    after = T0 + timedelta(minutes=31)
    value = window_at(store, after)
    assert value["freshness"] == "current" and value["pace_reason"] == "stale_sample"
    assert value["reaches_limit_before_reset"] is None
    value = window_at(store, after + timedelta(minutes=15))
    assert value["freshness"] == "stale"
    assert window_at(store, RESET)["pace_reason"] == "reset_passed"
    new_reset = RESET + timedelta(hours=5)
    capture(store, payload(5, new_reset), RESET + timedelta(minutes=1))
    value = window_at(store, RESET + timedelta(minutes=1))
    assert value["pace_reason"] == "window_reset"
    assert value["reaches_limit_before_reset"] is None


def test_conflicting_and_out_of_order_counters(store):
    nxt = T0 + timedelta(minutes=10)
    capture(store, payload(30), T0)
    capture(store, payload(31), nxt)
    assert capture(store, payload(88), T0)["conflicts"] == 1
    assert window_at(store, nxt)["pace_reason"] == "conflicting_samples"
    assert capture(store, payload(20), T0 - timedelta(minutes=1))["out_of_order"] == 1
    capture(store, payload(29), nxt + timedelta(minutes=1))
    assert window_at(store, nxt + timedelta(minutes=1))["pace_reason"] == "single_sample"
    capture(store, payload(28), nxt + timedelta(minutes=2))
    value = window_at(store, nxt + timedelta(minutes=2))
    assert value["coverage"] == "conflicting" and value["pace_reason"] == "counter_decreased"
    assert value["pace_percent_per_hour"] is None
    assert capture(store, payload(49), nxt + timedelta(minutes=2))["conflicts"] == 1
    assert window_at(store, nxt + timedelta(minutes=2))["used_percent"] is None


def test_compact_statusline_names_conflicting_counters(store, monkeypatch, capsys):
    later = T0 + timedelta(minutes=1)
    capture(store, payload(51), T0)
    capture(store, payload(50), later)
    report = operational_status.statusline_snapshot(quota_path=store[1], now=later)
    monkeypatch.setattr(quota_cli, "statusline_snapshot", lambda: report)
    quota_cli.run_statusline(argparse.Namespace(json=False))
    assert "5h quota conflicting" in capsys.readouterr().out


def test_statusline_uses_only_bounded_cached_state(store, tmp_path, monkeypatch):
    assert quota.quota_snapshot(store[1], now=T0)["cache_status"] == "missing"
    capture(store, payload(70), T0)
    assessed = T0.isoformat()
    coverage.report_path().write_text(json.dumps({
        "version": 1, "generated_at": assessed,
        "collectors": [{"configured": True, "observation": {"finished_at": assessed}},
                       {"configured": False, "observation": {
                           "finished_at": (T0 + timedelta(hours=1)).isoformat()}}],
        "issues": [{"severity": "warning", "source_id": "private-registry-label"}],
        "alerts": {"private-alert-label": {"first_seen": T0.isoformat(), "last_seen": T0.isoformat()},
                   "old-label": {"first_seen": T0.isoformat(), "absent_since": T0.isoformat()}},
        "delivery": {"status": "available", "pending": {"activity": 3}},
        "private_path": "/synthetic/home",
    }))

    def fail(*args, **kwargs):
        pytest.fail("statusline invoked a database, scan or coverage refresh")

    monkeypatch.setattr(coverage, "build_report", fail)
    monkeypatch.setattr(observations, "_connect", fail)
    before = {f: (f.read_bytes(), f.stat().st_mtime_ns) for f in tmp_path.iterdir()}
    for _ in range(5):
        report = operational_status.statusline_snapshot(now=T0)
        assert report["operations"]["scan_freshness"] == "current"
        assert report["operations"]["coverage_warnings"] == 1
        assert report["operations"]["new_coverage_alerts"] == 1
        assert report["operations"]["pending_delivery"] == 3
        assert report["attention"]["state"] == "unknown"
        assert "/synthetic/home" not in json.dumps(report)
        assert "private-registry-label" not in json.dumps(report)
        assert "private-alert-label" not in json.dumps(report)
    assert before == {f: (f.read_bytes(), f.stat().st_mtime_ns) for f in tmp_path.iterdir()}
    report = operational_status.statusline_snapshot(now=T0 + timedelta(hours=13))
    assert report["operations"]["assessment_freshness"] == "stale"
    assert report["operations"]["scan_freshness"] == "stale"
    store[1].write_bytes(b" " * (quota.MAX_CACHE_BYTES + 1))
    assert quota.quota_snapshot(store[1], now=T0) == {
        "schema_version": 1, "cache_status": "limited", "windows": [],
    }


def test_quota_notices_persist_quiet_recovery_and_recurrence(store, tmp_path):
    path = tmp_path / "notices.json"
    check = operational_status.check_quota_notices
    quiet = (time(11), time(13))
    warning_at = T0 + timedelta(hours=1, minutes=2)
    capture(store, payload(91), T0)
    assert check([window_at(store, T0)], path=path, now=T0, quiet_hours=quiet) == []
    assert not path.exists()
    assert check([window_at(store, warning_at)], path=path, now=warning_at) == []
    capture(store, payload(92), warning_at)
    window = window_at(store, warning_at)
    assert [notice["kind"] for notice in check([window], path=path, now=warning_at)] == [
        "quota_warning"]
    assert check([window], path=path, now=warning_at + timedelta(seconds=1)) == []
    assert operational_status.mute_quota(window, path=path, now=warning_at) == RESET.isoformat()
    during_gap = RESET + timedelta(minutes=1)
    assert check([], path=path, now=during_gap) == []
    assert check([window_at(store, during_gap)], path=path, now=during_gap) == []
    assert len(operational_status.read_quota_notices(path)) == 1
    new_reset = RESET + timedelta(hours=5)
    capture(store, payload(7, new_reset), during_gap)
    fresh = window_at(store, during_gap)
    assert [notice["kind"] for notice in check([fresh], path=path, now=during_gap)] == [
        "quota_recovered"]
    assert check([fresh], path=path, now=during_gap) == []
    later = during_gap + timedelta(minutes=20)
    capture(store, payload(94, new_reset), later)
    assert [notice["kind"] for notice in check([window_at(store, later)], path=path, now=later)] == [
        "quota_warning"]
    assert [notice["kind"] for notice in operational_status.read_quota_notices(path)] == [
        "quota_warning", "quota_recovered", "quota_warning"]


def test_mute_suppresses_initial_notice_until_reset(store, tmp_path):
    path = tmp_path / "notices.json"
    capture(store, payload(96), T0)
    operational_status.mute_quota(window_at(store, T0), path=path, now=T0)
    assert not operational_status.check_quota_notices([window_at(store, T0)], path=path, now=T0)
    now = RESET + timedelta(seconds=1)
    capture(store, payload(96, RESET + timedelta(hours=5)), now)
    assert operational_status.check_quota_notices([window_at(store, now)], path=path, now=now)


def test_registered_commands_are_explicit_and_statusline_is_read_only(store, capsys, monkeypatch):
    parser = argparse.ArgumentParser()
    quota_cli.register(parser.add_subparsers(dest="cmd", required=True))
    monkeypatch.setattr(sys, "stdin", io.StringIO(FIXTURE.read_text()))
    command = ["quota", "capture", "--db", str(store[0]), "--scope", SCOPE,
               "--observed-at", T0.isoformat()]
    quota_cli.run(parser.parse_args(command))
    assert json.loads(capsys.readouterr().out)["accepted"] == 2
    quota_cli.run_statusline(parser.parse_args(["statusline", "--json"]))
    assert json.loads(capsys.readouterr().out)["quota"]["cache_status"] == "available"
    quota_cli.run(parser.parse_args(["quota", "check"]))
    assert json.loads(capsys.readouterr().out)["cache_status"] == "available"
    assert not operational_status.quota_notices_path().exists()
    with pytest.raises(AutError, match="quiet hours apply only"):
        quota_cli.run(parser.parse_args(["quota", "check", "--quiet-hours", "22:00-07:00"]))
