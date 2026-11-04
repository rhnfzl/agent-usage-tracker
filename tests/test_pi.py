"""Hermetic tests for the Pi collector: usage, sessions, activity and its small readers.

Run: `python tests/test_pi.py`  (or `pytest`).
Locks the rules:
  - usage comes from message.usage; the header identity is carried in the watermark, with
    reasoning tokens, modelId, project from the header cwd and compaction usage; fork copies are
    skipped for session and activity rows;
  - activity: steer via, interrupt, commit, derived turn (retry is not an end), compaction, invoke.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_usage_tracker.collectors import pi  # noqa: E402
from agent_usage_tracker.event import UsageEvent  # noqa: E402
from agent_usage_tracker.event import make_event_id  # noqa: E402
from codex_pi_helpers import (FORK, SID, T, _acts, _append, _cfg, _collect, _hdr,  # noqa: E402
                              _pa, _pi_file, _pr, _pu, _run, _usage, _write_jsonl)
from test_activity import _git_repo, _key  # noqa: E402
from test_collectors import _drain  # noqa: E402


def test_pi() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".pi/agent/sessions/dir/s.jsonl", [
            {"type": "model_change", "model": "claude-opus-4-8"},
            {"type": "message", "id": "m1", "timestamp": "2026-07-18T10:00:02Z",
             "message": {"provider": "anthropic", "usage": {
                 "input": 200, "output": 80, "cacheRead": 50, "cacheWrite": 10,
                 "totalTokens": 340, "cost": {"total": 0.0021}}}},
            # zero-token message -> filtered
            {"type": "message", "id": "m2", "timestamp": "2026-07-18T10:00:05Z",
             "message": {"usage": {"input": 0, "output": 0, "totalTokens": 0}}},
        ])
        ev = _drain(pi.collect, _cfg(home))
        assert len(ev) == 1, ev
        e = ev[0]
        assert e.input_tokens == 200 and e.output_tokens == 80
        assert e.cache_read_tokens == 50 and e.cache_write_tokens == 10
        assert e.total_tokens == 340 and e.est_cost_usd == 0.0021
        assert e.model == "claude-opus-4-8"                    # streamed from model_change
        assert e.project is None                               # encoded dir is not reconstructable
    print("ok pi")


def test_pi_usage_and_sessions() -> None:
    def check(home, repo):
        evs = _collect(pi, home)
        agent = {e.event_id: e for e in _usage(evs)}
        a1 = agent[make_event_id("pi", "a1")]
        assert (a1.reasoning_tokens, a1.output_tokens, a1.total_tokens, a1.project) == \
            (5, 10, 20, "repo"), a1
        c1 = agent[make_event_id("pi", "c1")]
        assert (c1.model, c1.provider, c1.total_tokens, c1.reasoning_tokens, c1.est_cost_usd) \
            == ("model-a", "prov", 150, 10, 0.01), c1
        assert agent[make_event_id("pi", "a2")].reasoning_tokens is None
        main = [e for e in evs if isinstance(e, UsageEvent) and e.session
                and e.session.session_id == SID]
        assert {(e.session.parent_session_id, e.session.run_origin) for e in main} == \
            {(None, "unknown")}
        turn_of = {e.event_id: e.session.turn_id for e in main}
        assert turn_of[make_event_id("pi", "a1")] == "u1"
        assert turn_of[make_event_id("pi", "c1")] == "u4"
        assert turn_of[make_event_id("pi", "human", "u5")] == "u5"
        # the fork: copied entries keep their usage ids but get no session or activity rows
        fork = [e for e in evs if isinstance(e, UsageEvent)
                and e.event_id in {make_event_id("pi", "human", "u1"), make_event_id("pi", "a2"),
                                   make_event_id("pi", "human", "f1"), make_event_id("pi", "f2")}]
        assert len(fork) == 6, fork                   # u1 and a2 twice (main + fork), f1, f2
        assert sorted((e.session.session_id, e.session.parent_session_id)
                      for e in fork if e.session) == [
            (SID, None), (SID, None), (FORK, SID), (FORK, SID)], fork
        assert {e.event_id for e in _acts(evs, sid=FORK)} == {
            make_event_id("pi", k, f"{FORK}|f1") for k in ("steer", "turn", "link")}
        # parent and session fallbacks: the uuid in a file name, never the path
        assert pi.uuid_in(f"/gone/2026-01-01T00-00-00-000Z_{FORK}.jsonl") == FORK
        assert pi.uuid_in("/gone/not-a-session.jsonl") is None
    _run(check)
    print("ok pi_usage_and_sessions")


def test_pi_activity() -> None:
    def check(home, repo):
        evs = _acts(_collect(pi, home), sid=SID)
        st = {e.event_id: e.attrs for e in _acts(evs, "steer")}
        got = [(st[make_event_id("pi", "steer", f"{SID}|{u}")]["via"],
                st[make_event_id("pi", "steer", f"{SID}|{u}")]["after_interrupt"])
               for u in ("u1", "u2", "u3", "u4", "u5")]
        assert got == [("typed", False), ("skill", False), ("typed", True), ("queued", False),
                       ("typed", False)], got
        assert st[make_event_id("pi", "steer", f"{SID}|u1")]["words"] == 5
        [i] = _acts(evs, "interrupt")
        assert (i.turn_id, i.attrs) == ("u2", {"during_tool": True}), i
        [c] = _acts(evs, "commit")
        assert c.attrs == {"sha": "1a2b3c4", "repo_key": _key("example.com/owner/repo"),
                           "amend": False}, c.attrs
        assert c.event_id == make_event_id("pi", "commit", f"{SID}|k1|1a2b3c4")
        [inv] = _acts(evs, "invoke")
        assert (inv.turn_id, inv.attrs) == ("u2", {"skill": "my-skill"}), inv
        [cp] = _acts(evs, "compaction")
        assert cp.attrs == {"tokens_before": 5000}, cp.attrs
        turns = {e.turn_id: (e.ts, e.attrs) for e in _acts(evs, "turn")}
        assert set(turns) == {"u1", "u2", "u3", "u4"}, turns    # u5 is still open
        ts, t1 = turns["u1"]
        assert (ts, t1["duration_ms"], t1["message_count"], t1["tool_calls"], t1["tool_errors"],
                t1["by_tool"]) == (T(5), 4000, 5, 2, 1, {"bash": 1, "read": 1}), t1
        assert "hook_blocks" not in t1
        assert turns["u2"][0] == T(9)                          # closed by the abort
        assert (turns["u3"][0], turns["u3"][1]["duration_ms"]) == (T(12), 2000)   # by next user
        assert (turns["u4"][0], turns["u4"][1]["duration_ms"]) == (T(15), 2000)   # retry, stop
    _run(check)
    print("ok pi_activity")


def test_pi_carries_across_scans() -> None:
    # The open turn closes on a later scan; the watermark of the first scan is from before the
    # header was carried, so the header is re-read from line 1.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        repo = _git_repo(home, "https://Example.com/Owner/Repo.git")
        f = home / f".pi/agent/sessions/--x--/2026-07-18T10-00-00-000Z_{SID}.jsonl"
        _write_jsonl(f, [_hdr(SID, str(repo)), _pu("u1", T(1), "go"),
                         _pa("a1", T(2), "toolUse", ("k1", "bash", {"command": "git commit -m x"}),
                             tokens=1)])
        wm: dict = {}
        assert not _acts(_collect(pi, home, wm), "turn")
        next(iter(wm["files"].values())).pop("hdr")
        del wm["state_v"]           # state from before the header was kept has no version either
        _append(f, [_pr("r1", T(3), "k1", "ok 0a1b2c3"), _pa("a2", T(4), "stop", tokens=1)])
        second = _collect(pi, home, wm)
        [t] = _acts(second, "turn")
        assert (t.session_id, t.turn_id, t.attrs["tool_calls"], t.attrs["duration_ms"]) == \
            (SID, "u1", 1, 3000), t.attrs
        [c] = _acts(second, "commit")
        assert c.attrs["sha"] == "0a1b2c3" and c.attrs["repo_key"]
        [u] = _usage(second)
        assert (u.project, u.session.session_id, u.session.turn_id) == ("repo", SID, "u1")
    print("ok pi_carries_across_scans")


def test_pi_watermark_with_an_old_dev_is_not_read_again() -> None:
    """A reboot renumbers st_dev. State saved before Pi moved to inode-only identity holds the old
    number: it must resume where it was, with no history re-read or re-counted."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = _pi_file(home)
        _write_jsonl(f, [_hdr(SID, "/x/repo"), _pu("u1", T(1), "go"),
                         _pa("a1", T(2), "stop", tokens=7)])
        wm: dict = {}
        assert len(_usage(_collect(pi, home, wm))) == 1
        entry = wm["files"][str(f)]
        off, ino = entry["off"], entry["ino"]
        entry["dev"] += 4                # what a restart does to every tracked path
        del wm["state_v"]                # and this state predates the version
        assert _collect(pi, home, wm) == [], "nothing new: nothing read again"
        assert (entry["off"], entry["ino"]) == (off, ino)
        _append(f, [_pu("u2", T(3), "more"), _pa("a2", T(4), "stop", tokens=9)])
        later = _collect(pi, home, wm)
        assert [u.event_id for u in _usage(later)] == [make_event_id("pi", "a2")], later  # new only
        assert [h.session.turn_id for h in _usage(later, "human")] == ["u2"]
    print("ok pi_watermark_with_an_old_dev_is_not_read_again")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
