"""Hermetic tests for label/tasks.py: which units each task asks about, and what they hold.

Run: `python tests/test_label_tasks.py`  (or `pytest`).
Locks the rule: steer text is read from the local Claude Code log and redacted; a steer without
local text is not labelled.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.event import make_event_id  # noqa: E402
from agent_usage_tracker.label import core  # noqa: E402
from agent_usage_tracker import config  # noqa: E402
from agent_usage_tracker.label import run  # noqa: E402
from agent_usage_tracker.label import tasks  # noqa: E402
from label_helpers import DAY1, FakeSource, NOW, _cfg, _claude_log, _steer, _win  # noqa: E402


def test_steer_without_uuid_finds_its_text_under_the_collectors_id(tmp_path):
    # the id the collector gives a record with no uuid: path:timestamp
    proj = tmp_path / ".claude" / "projects" / "-p"
    proj.mkdir(parents=True)
    f = proj / "s1.jsonl"
    ts = "2001-01-05T09:00:00Z"
    f.write_text(json.dumps({"type": "user", "timestamp": ts, "sessionId": "s1",
                             "message": {"role": "user", "content": "old log prompt"}}) + "\n")
    src = FakeSource(_cfg(tmp_path))
    steer = _steer(f"{f}:{ts}", "s1", DAY1)
    assert src.texts([steer]) == {steer["event_id"]: "old log prompt"}


def test_steer_units_read_local_redacted_text(tmp_path):
    t0 = DAY1
    _claude_log(tmp_path, "s1", [
        ("u1", "build the export, key sk-abcdefghijklmnopqrstuvwx"),
        ("u2", "no, mail bob@example.com first"),
        ("u3", "x" * 5000)])
    src = FakeSource(_cfg(tmp_path), steers=[
        _steer("u1", "s1", t0), _steer("u2", "s1", t0 + timedelta(seconds=90),
                                       after_interrupt=True),
        _steer("u3", "s1", t0 + timedelta(seconds=120)),
        _steer("gone", "s1", t0 + timedelta(seconds=150)),          # not in the local log
        _steer("u1", "s1", t0 + timedelta(seconds=160), agent="codex")])
    reqs = tasks.units("steer_intent", src, t0 + timedelta(seconds=1), None)
    assert [r.unit_id for r in reqs] == [make_event_id("claude_code", "steer", u)
                                         for u in ("u2", "u3")]
    st = reqs[0].facts
    assert st["text"] == "no, mail [redacted] first"
    assert "sk-" not in st["previous_text"] and "[redacted]" in st["previous_text"]
    assert st["index_in_session"] == 1 and st["seconds_since_previous"] == 90
    assert st["after_interrupt"] is True
    assert len(reqs[1].facts["text"]) <= tasks.TEXT_MAX


def _member(session_id):
    return {"session_id": session_id, "role": "seed", "repo_keys": []}


def test_work_category_units(tmp_path):
    t0 = DAY1.replace(minute=0)
    _claude_log(tmp_path, "s1", [("u1", "plan the export"), ("u2", "now write it")])
    steers = [_steer("u1", "s1", t0), _steer("u2", "s1", t0 + timedelta(minutes=5))]
    eps = [{"id": "e1", "first_ts": t0, "last_ts": t0 + timedelta(minutes=5),
            "n_sessions": 1, "n_steers": 2, "n_commits": 1, "outcome": "open",
            "members": [_member("s1")]}]
    acts = [{"session_id": "s1", "ts": t0 + timedelta(minutes=1), "kind": "turn",
             "attrs": {"by_tool": {"Edit": 3, "Bash": 1}, "tool_errors": 1}},
            {"session_id": "s1", "ts": t0 + timedelta(minutes=2), "kind": "commit", "attrs": {}}]
    wins = [_win(t0 - timedelta(minutes=2), 4, app="Slack", category="comms", rule="default_app"),
            _win(t0 + timedelta(minutes=30), 4, app="Slack", category="comms",
                 rule="default_app"),
            _win(t0 + timedelta(minutes=34, seconds=30), 3, app="Slack", category="comms",
                 rule="default_app"),
            _win(t0 + timedelta(minutes=50), 2, app="Notes", category="docs")]
    # a Codex session in the same episode: its steer and turns stay out of the episode facts
    steers.append(_steer("c1", "s2", t0 + timedelta(minutes=6), agent="codex"))
    eps[0]["members"].append(_member("s2"))
    acts.append({"session_id": "s2", "ts": t0 + timedelta(minutes=7), "kind": "turn",
                 "attrs": {"by_tool": {"shell": 9}, "tool_errors": 4}})
    cfg = _cfg(tmp_path)
    reqs = tasks.units("work_category", FakeSource(cfg, steers, eps, acts, wins),
                       run.local_midnight(DAY1.astimezone().date()), run.local_midnight(
                           NOW.astimezone().date()))
    by = {r.unit: r for r in reqs}
    assert set(by) == {"episode_day", "aw_block"} and len(reqs) == 2
    st = by["episode_day"].facts
    assert st["first_steer"] == "plan the export" and st["last_steers"] == ["now write it"]
    assert st["tool_calls_by_tool"] == {"Edit": 3, "Bash": 1} and st["commits"] == 1
    assert st["steers"] == 2 and st["sessions"] == 1 and st["tool_errors"] == 1
    assert by["episode_day"].unit_id == f"e1:{t0.astimezone().date().isoformat()}"
    # the 7-minute Slack block counts; the one with a steer inside and the 2-minute one do not
    assert by["aw_block"].facts["minutes"] == 7 and by["aw_block"].facts["apps"] == ["Slack"]


def test_soft_outcome_only_no_code(tmp_path):
    _claude_log(tmp_path, "s1", [("u1", "look into the queue"), ("u2", "park it")])
    start = DAY1 - timedelta(days=3)
    steers = [_steer("u1", "s1", DAY1), _steer("u2", "s2", DAY1),
              _steer("x1", "s3", DAY1, agent="codex")]
    ep = {"first_ts": start, "last_ts": DAY1, "n_sessions": 2, "n_steers": 2, "n_commits": 0}
    eps = [{**ep, "id": "e1", "outcome": "no_code", "members": [_member("s1"), _member("s3")]},
           {**ep, "id": "e2", "outcome": "shipped", "members": [_member("s1")]}]
    reqs = tasks.units("soft_outcome", FakeSource(_cfg(tmp_path), steers, eps),
                       NOW - timedelta(days=14), NOW)
    assert [r.unit_id for r in reqs] == ["e1"]
    assert reqs[0].facts["last_steers"] == ["look into the queue"]
    assert reqs[0].none_fits is None and reqs[0].facts["days_idle"] == 1
    assert reqs[0].facts["steers"] == 1 and reqs[0].facts["sessions"] == 1     # Claude Code only
    # the row's ts is the episode start (stable); new activity re-asks with a new id
    assert reqs[0].ts == start.isoformat()
    res = core.decide(reqs[0], {"resolved": 1.0}, core.Choice(), "m", "r")
    first = core.label_event(reqs[0], res).event_id
    eps[0]["last_ts"] = NOW
    again = tasks.units("soft_outcome", FakeSource(_cfg(tmp_path), steers, eps),
                        NOW - timedelta(days=14), NOW)[0]
    assert again.ts == reqs[0].ts and core.label_event(again, res).event_id != first


def test_task_names_match_the_tasks():
    assert config.LABEL_DECISION_TASKS == tuple(tasks.TASKS)
    assert config.LABEL_NOTE not in tasks.TASKS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
