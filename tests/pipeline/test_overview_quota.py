from argparse import ArgumentParser
from datetime import date, datetime, timedelta, timezone
import sqlite3

import pytest

from agent_usage_tracker import overview, overview_cli
from agent_usage_tracker.demo import fixture
from agent_usage_tracker.quota import capture_claude_quota, quota_cache_path
from agent_usage_tracker.summary import build_summary


def test_overview_shows_cached_quota_and_unknown_pace(tmp_path):
    config = fixture(tmp_path)
    report = build_summary(config, since=date(2026, 6, 15), until=date(2026, 6, 16),
                           prices_path=tmp_path / "no-prices")
    current = {
        "source": "claude_code", "scope": "0f" * 16, "window": "five_hour",
        "used_percent": 0.0, "observed_at": "2026-06-15T12:00:00+00:00",
        "expires_at": "2026-06-15T12:30:00+00:00", "reset_at": "2026-06-15T14:00:00+00:00",
        "freshness": "current", "coverage": "observed", "pace_percent_per_hour": 0.0,
        "reaches_limit_before_reset": False, "pace_reason": None,
    }
    stale = dict(current, window="seven_day", used_percent=40.0, freshness="stale",
                 pace_percent_per_hour=None, reaches_limit_before_reset=None,
                 pace_reason="stale_sample")
    quota = {"schema_version": 1, "cache_status": "available", "windows": [current, stale]}
    html = overview.render(report, {"collectors": [], "issues": []}, "/tok", "custom", False,
                           quota=quota)
    for value in (b"Claude Code", b"5 hours", b"7 days", b"0%", b"0 percentage points/hour",
                  b"2026-06-15T14:00:00+00:00", b"stale sample"):
        assert value in html


def test_overview_quota_cache_missing_does_not_write(tmp_path, monkeypatch):
    config = fixture(tmp_path / "home")
    state = tmp_path / "state" / "state.json"
    monkeypatch.setenv("AUT_STATE", str(state))
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("AUT_IMPROVE_ROOT", str(tmp_path / "empty-improvement"))
    parser = ArgumentParser()
    overview_cli.register(parser.add_subparsers(required=True))
    args = parser.parse_args(["overview", "--no-browser", "--since", "2026-06-15",
                              "--until", "2026-06-16"])

    def serve(token, on_get, *args, **kwargs):
        assert b"No cached quota observations" in on_get("")

    monkeypatch.setattr(overview_cli.local_page, "serve", serve)
    overview_cli.run(config, args)
    assert not state.parent.exists()


def test_overview_reads_new_quota_cache_without_opening_observation_store(tmp_path, monkeypatch):
    config = fixture(tmp_path / "home")
    state = tmp_path / "state" / "state.json"
    monkeypatch.setenv("AUT_STATE", str(state))
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("AUT_IMPROVE_ROOT", str(tmp_path / "empty-improvement"))
    parser = ArgumentParser()
    overview_cli.register(parser.add_subparsers(required=True))
    args = parser.parse_args(["overview", "--no-browser"])

    def serve(token, on_get, *args, **kwargs):
        assert b"No cached quota observations" in on_get("")
        observed = datetime.now(timezone.utc) - timedelta(seconds=1)
        reset = int((observed + timedelta(hours=2)).timestamp())
        capture_claude_quota(
            {"rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": reset}}},
            scope="12345678-1234-4123-8123-123456789abc",
            db=tmp_path / "observations.db", observed_at=observed,
        )
        snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        assert quota_cache_path() in snapshot
        with monkeypatch.context() as cached:
            cached.setattr(sqlite3, "connect", lambda *a, **kw: pytest.fail("opened SQLite"))
            page = on_get("")
        assert b"23.5%" in page and b"single sample" in page and b"current" in page
        assert snapshot == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    monkeypatch.setattr(overview_cli.local_page, "serve", serve)
    overview_cli.run(config, args)
