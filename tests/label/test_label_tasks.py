"""Hermetic tests for label/tasks.py: which units each task asks about, and what they hold.

Locks the rule: steer text is read from the local Claude Code log and redacted; a steer without
local text is not labelled.
"""

from __future__ import annotations

import json
from datetime import timedelta

from agent_usage_tracker.event import make_event_id
from agent_usage_tracker.label import core
from agent_usage_tracker import config
from agent_usage_tracker.label import run
from agent_usage_tracker.label import tasks
from label_helpers import DAY1, FakeSource, NOW, _cfg, _claude_log, _steer, _win


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


def test_soft_outcome_stops_at_until(tmp_path):
    _claude_log(tmp_path, "s1", [("u1", "before the end"), ("u2", "after the end")])
    _claude_log(tmp_path, "s2", [("u3", "started late")])
    late = NOW + timedelta(hours=2)
    steers = [_steer("u1", "s1", DAY1), _steer("u2", "s1", late), _steer("u3", "s2", late)]
    ep = {"n_sessions": 1, "n_steers": 1, "n_commits": 0, "outcome": "no_code", "last_ts": late}
    eps = [{**ep, "id": "active", "first_ts": DAY1, "members": [_member("s1")]},
           {**ep, "id": "late", "first_ts": late, "members": [_member("s2")]}]
    reqs = tasks.units("soft_outcome", FakeSource(_cfg(tmp_path), steers, eps),
                       NOW - timedelta(days=14), NOW)
    assert [r.unit_id for r in reqs] == ["active"]
    assert reqs[0].facts["last_steers"] == ["before the end"] and reqs[0].facts["days_idle"] == 0


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


def test_work_category_bounds_shared_session_prompts_activity_and_reply(tmp_path, monkeypatch):
    t0 = DAY1.replace(minute=0)
    _claude_log(tmp_path, "s1", [("u1", "plan the export"), ("u2", "fix the crash")])
    steers = [_steer("u1", "s1", t0), _steer("u2", "s1", t0 + timedelta(hours=1))]
    eps = [{"id": name, "first_ts": start, "last_ts": start + timedelta(minutes=10),
            "members": [_member("s1")]} for name, start in
           [("early", t0), ("late", t0 + timedelta(hours=1))]]
    acts = [{"session_id": "s1", "ts": start + timedelta(minutes=1), "kind": "turn",
             "attrs": {"by_tool": {tool: 1}}} for start, tool in
            [(t0, "Plan"), (t0 + timedelta(hours=1), "Edit")]]
    src = FakeSource(_cfg(tmp_path), steers, eps, acts)
    cutoffs = []

    def replies(rows, until=None):
        cutoffs.append(until)
        return {row["event_id"]: ("", "later unrelated reply") for row in rows}

    monkeypatch.setattr(src, "replies", replies)
    local_day = t0.astimezone().date()
    reqs = tasks.units("work_category", src, run.local_midnight(local_day),
                       run.local_midnight(local_day + timedelta(days=1)))
    assert len(reqs) == 2
    assert [r.facts["first_steer"] for r in reqs] == ["plan the export", "fix the crash"]
    assert [r.facts["steers"] for r in reqs] == [1, 1]
    assert [r.facts["tool_calls_by_tool"] for r in reqs] == [{"Plan": 1}, {"Edit": 1}]
    assert cutoffs == [e["last_ts"] + timedelta(microseconds=1) for e in eps]


def test_work_category_reply_stops_at_local_midnight_and_inclusive_episode_end(tmp_path):
    next_day = DAY1.astimezone().date() + timedelta(days=1)
    midnight = run.local_midnight(next_day)
    first, second, end = (midnight + timedelta(minutes=m) for m in (-2, 2, 5))
    _claude_log(tmp_path, "s1", [])
    log = tmp_path / ".claude/projects/-p/s1.jsonl"
    log.write_text("\n".join(json.dumps({"type": role, "uuid": uid, "sessionId": "s1",
        "timestamp": ts.isoformat(), "message": {"role": role, "content": text}})
        for role, uid, ts, text in [
            ("user", "u1", first, "plan"),
            ("assistant", "a1", midnight - timedelta(minutes=1), "first day reply"),
            ("assistant", "a2", midnight + timedelta(minutes=1), "next day reply"),
            ("user", "u2", second, "build"),
            ("assistant", "a3", end, "episode end reply"),
            ("assistant", "a4", end + timedelta(minutes=1), "outside episode"),
        ]))
    src = FakeSource(_cfg(tmp_path), [_steer("u1", "s1", first), _steer("u2", "s1", second)],
                     [{"id": "e", "first_ts": first, "last_ts": end,
                       "members": [_member("s1")]}])
    reqs = tasks.units("work_category", src, run.local_midnight(next_day - timedelta(days=1)),
                       run.local_midnight(next_day + timedelta(days=1)))
    assert [r.facts["last_agent_reply"] for r in reqs] == ["first day reply", "episode end reply"]


def test_task_names_match_the_tasks():
    assert config.LABEL_DECISION_TASKS == tuple(tasks.TASKS)
    assert config.LABEL_NOTE not in tasks.TASKS


def test_agent_replies_are_text_only_and_stop_at_until(tmp_path):
    from datetime import datetime, timezone

    from agent_usage_tracker.collectors import claude_code
    from agent_usage_tracker.event import make_event_id

    d = tmp_path / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    def rec(kind, uid, minute, content):
        return json.dumps({"type": kind, "uuid": uid, "message": {"content": content},
                           "timestamp": f"2026-11-01T10:{minute:02d}:00Z"})
    (d / "s1.jsonl").write_text("\n".join([
        rec("assistant", "x0", 0, [{"type": "text", "text": "hello"}]),
        rec("user", "u1", 1, "first"),
        rec("assistant", "x1", 2, [{"type": "thinking", "thinking": "secret"},
                                   {"type": "tool_use", "name": "Bash", "input": {}}]),
        rec("user", "tr", 3, [{"type": "tool_result", "content": "tool output"}]),
        rec("assistant", "x2", 4, [{"type": "text", "text": "done one"}]),
        rec("user", "u2", 5, "second"),
        rec("assistant", "x3", 6, [{"type": "text", "text": "done two"}]),
        "not json"]))
    e1, e2 = (make_event_id("claude_code", "steer", u) for u in ("u1", "u2"))
    got = claude_code.agent_replies(tmp_path, {"s1"}, {e1, e2})
    assert got == {e1: ("hello", "done one"), e2: ("done one", "done two")}
    until = datetime(2026, 11, 1, 10, 6, tzinfo=timezone.utc)
    assert claude_code.agent_replies(tmp_path, {"s1"}, {e1, e2}, until)[e2] == ("done one", "")
    assert claude_code.agent_replies(tmp_path, {"other"}, {e1, e2}) == {}


def test_a_steer_unit_carries_the_reply_it_answers_cut_to_its_end(tmp_path, monkeypatch):
    src = tasks.Source(_cfg(tmp_path))
    long = "some words " * tasks.REPLY_MAX + "the end"
    monkeypatch.setattr(tasks.claude_code, "agent_replies",
                        lambda home, sessions, want, until=None, **kw:
                        {e: (long, "after") for e in want})
    rows = [{"event_id": "e", "agent": "claude_code", "session_id": "s"}]
    assert src.replies(rows)["e"][0].endswith("the end")
    assert len(src.replies(rows)["e"][0]) == tasks.REPLY_MAX
    assert src.replies(rows)["e"][1] == "after"
