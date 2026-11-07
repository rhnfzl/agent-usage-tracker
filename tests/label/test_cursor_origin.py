"""Local source guards reject registered Cursor inputs, including old sink rows."""

import json
from datetime import datetime, timezone

import pytest

from agent_usage_tracker import cursor_run
from agent_usage_tracker.config import AutError, Config
from agent_usage_tracker.label import tasks
from agent_usage_tracker.state import atomic_write_text

SID = "aaaaaaaa-0000-4000-8000-000000000001"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    cfg = Config("test", (), None, None, "synthetic", tmp_path, repo_key_salt="test")
    return tasks.Source(cfg)


def test_steers_read_registry_after_sink_snapshot(source, monkeypatch):
    def sql(*args):
        atomic_write_text(cursor_run.registry_path(), json.dumps({"version": 1, "sessions": [SID]}))
        return [("auto", NOW, "cursor_cli", SID, {}), ("human", NOW, "claude_code", SID, {})]

    monkeypatch.setattr(tasks, "_sql", sql)
    assert [r["event_id"] for r in source.steers(NOW)] == ["human"]


def test_activity_omits_only_registered_cursor_rows(source, monkeypatch):
    atomic_write_text(cursor_run.registry_path(), json.dumps({"version": 1, "sessions": [SID]}))
    monkeypatch.setattr(tasks, "_sql", lambda *a: [(SID, NOW, "turn", {}, "cursor_cli"),
                                                  (SID, NOW, "turn", {}, "claude_code")])
    assert [r["agent"] for r in source.activity([SID], NOW, NOW)] == ["claude_code"]


def test_corrupt_origin_does_not_silently_enter_classification(source, monkeypatch):
    atomic_write_text(cursor_run.registry_path(), "invalid")
    monkeypatch.setattr(tasks, "_sql", lambda *a: [("auto", NOW, "cursor_cli", SID, {})])
    with pytest.raises(AutError):
        source.steers(NOW)
