"""Labels after a long gap: day tasks wait for the episode rebuild, and steers whose text is not on
this machine are counted, not dropped without a word."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_usage_tracker.label import run, tasks
from label_helpers import ById, FakeSource, _cfg, _claude_log, _env, _steer
from test_label import sink  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 3, 10, 0).astimezone()          # the Mac is back, a month later
GAP = NOW - timedelta(days=20)                          # a day inside the gap


def _midnight(t: datetime) -> datetime:
    return run.local_midnight(t.astimezone().date())


def _member(sid: str) -> dict:
    return {"session_id": sid, "role": "seed", "repo_keys": []}


def _gap_day(tmp_path):
    _claude_log(tmp_path, "s1", [("u1", "plan the export")])
    steers = [_steer("u1", "s1", GAP)]
    eps = [{"id": "e1", "first_ts": GAP, "last_ts": GAP, "n_sessions": 1, "n_steers": 1,
            "n_commits": 0, "outcome": "no_code", "members": [_member("s1")]}]
    return steers, eps


def test_day_window_stops_at_the_rebuilt_episodes():
    wm = {"work_category": _midnight(NOW - timedelta(days=31)).isoformat()}
    start, until = run._day_window("work_category", wm, None, NOW, ready=NOW - timedelta(days=31))
    assert until == start                               # nothing past the last rebuilt day
    _s, until = run._day_window("work_category", wm, None, NOW, ready=NOW)
    assert until == _midnight(NOW)
    _s, until = run._day_window("work_category", wm, None, NOW, ready=None)
    assert until == start                              # no rebuild: preserve the pending days
    # titles come from this machine's aw-server, not from the episodes: never held back
    assert run._window("aw_title_relevance", wm, None, NOW, ready=GAP)[1] == _midnight(NOW)


@pytest.mark.parametrize("ready", [None, NOW - timedelta(days=31)])
def test_gap_days_are_labelled_after_the_rebuild_not_before(tmp_path, sink, ready):  # noqa: F811
    steers, eps = _gap_day(tmp_path)
    cfg = _cfg(tmp_path)
    wm0 = _midnight(NOW - timedelta(days=31)).isoformat()
    with _env(tmp_path):
        (tmp_path / "state.json").write_text(json.dumps(
            {"collectors": {"label": {"work_category": wm0}}}))
        # the scan just delivered the month; the last rebuild saw only the days before the gap
        rep = run.run_label(cfg, ["work_category"], backend_for=lambda t, c: ById("planning"),
                            src=FakeSource(cfg, steers, (), ready=ready),
                            now=NOW)
        assert rep["work_category"]["rows"] == 0
        assert rep["work_category"]["unavailable"] == 1
        wm = json.loads((tmp_path / "state.json").read_text())["collectors"]["label"]
        assert wm["work_category"] == wm0               # the gap days are not skipped for good
        # the next night: the rebuild has the gap's episodes
        rep = run.run_label(cfg, ["work_category"], backend_for=lambda t, c: ById("planning"),
                            src=FakeSource(cfg, steers, eps, ready=NOW + timedelta(hours=17)),
                            now=NOW + timedelta(days=1))
        assert rep["work_category"]["rows"] == 1
        assert [e.attrs["unit_id"] for b in sink.rows for e in b] == \
            [f"e1:{GAP.astimezone().date().isoformat()}"]


def test_episodes_ready_reads_the_rebuild_then_the_newest_episode(tmp_path, monkeypatch):
    seen: list[tuple[str, tuple]] = []
    answers: dict[str, object] = {"collector_scan": None, "agents.episode": None}

    def fake_sql(dsn, sql, params):
        seen.append((sql, params))
        key = "collector_scan" if "collector_scan" in sql else "agents.episode"
        return [(answers[key],)]

    monkeypatch.setattr(tasks, "_sql", fake_sql)
    src = tasks.Source(_cfg(tmp_path))
    assert src.episodes_ready() is None                 # an empty sink: day tasks wait
    assert seen[0][1] == ("test",)                      # this machine's scans only
    t1, t2 = datetime(2026, 10, 3, 12, tzinfo=timezone.utc), datetime(2026, 10, 5, tzinfo=timezone.utc)
    answers["agents.episode"] = t1
    assert src.episodes_ready() == t1                   # no rebuild record: the newest episode
    answers["collector_scan"] = t2
    assert src.episodes_ready() == t2                   # the rebuild record wins


def test_steers_without_local_text_are_counted(tmp_path, sink, capsys):  # noqa: F811
    day = NOW - timedelta(days=1)
    _claude_log(tmp_path, "s1", [("u1", "has text")])
    steers = [_steer("u1", "s1", day), _steer("u2", "s1", day + timedelta(minutes=1)),
              _steer("u3", "s2", day + timedelta(minutes=2)),
              _steer("c1", "s3", day, agent="codex")]   # Codex text is never read: not counted
    cfg = _cfg(tmp_path)
    with _env(tmp_path):
        rep = run.run_label(cfg, ["steer_intent"], src=FakeSource(cfg, steers),
                            backend_for=lambda t, c: ById(), now=NOW, since=day - timedelta(hours=1))
    assert rep["steer_intent"]["rows"] == 1 and rep["steer_intent"]["skipped_no_text"] == 2
    run.print_report(rep, dry_run=False)
    assert "2 steers skipped: their text is not on this machine" in capsys.readouterr().err
