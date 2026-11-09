"""Hermetic tests for the Pi collector: usage, sessions, activity and its small readers.

Locks the rules:
  - usage comes from message.usage; the header identity is carried in the watermark, with
    reasoning tokens, modelId, project from the header cwd and compaction usage; fork copies are
    skipped for session and activity rows;
  - activity: steer via, interrupt, commit, derived turn (retry is not an end), compaction, invoke.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from agent_usage_tracker.collectors import pi
from agent_usage_tracker.event import UsageEvent
from agent_usage_tracker.event import make_event_id
from codex_pi_helpers import (FORK, SID, T, _acts, _append, _cfg, _collect, _hdr,
                              _pa, _pi_file, _pr, _pu, _run, _usage, _write_jsonl)
from test_activity import _git_repo, _key
from test_collectors import _drain


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
                t1["by_tool"]) == (T(1), 4000, 5, 2, 1, {"bash": 1, "read": 1}), t1
        assert "hook_blocks" not in t1
        assert turns["u2"][0] == T(6)                          # stable turn start
        assert (turns["u3"][0], turns["u3"][1]["duration_ms"]) == (T(10), 2000)
        assert (turns["u4"][0], turns["u4"][1]["duration_ms"]) == (T(13), 2000)
    _run(check)


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


@pytest.mark.parametrize("stop", ["error", "toolUse"])
def test_pi_idle_turn_publishes_at_start_and_retries_transactionally(tmp_path, stop):
    f = _pi_file(tmp_path)
    _write_jsonl(f, [_hdr(SID, "/x/repo"), _pu("u1", T(1), "go"),
                     _pa("a1", T(2), stop, tokens=1)])
    wm = {}
    assert not _acts(_collect(pi, tmp_path, wm), "turn")
    os.utime(f, (1_600_000_000, 1_600_000_000))
    before = json.dumps(wm, sort_keys=True)
    failed = [e for events, commit in pi.collect(_cfg(tmp_path), wm) for e in events]
    assert json.dumps(wm, sort_keys=True) == before
    [turn] = _acts(failed, "turn")
    assert (turn.ts, turn.attrs["duration_ms"], turn.attrs["message_count"]) == (T(1), 1000, 2)
    assert _collect(pi, tmp_path, wm) == failed
    assert _collect(pi, tmp_path, wm) == []


def test_pi_idle_snapshot_keeps_pending_commit_and_matches_full_read(tmp_path):
    f = _pi_file(tmp_path)
    head = [_hdr(SID, "/x/repo"), _pu("u1", T(1), "go"),
            _pa("a1", T(2), "toolUse", ("k1", "bash", {"command": "git commit -m x"}), tokens=1)]
    tail = [_pr("r1", T(3), "k1", "[feat/x 1a2b3c4] x"), _pa("a2", T(4), "stop", tokens=1)]
    _write_jsonl(f, head)
    os.utime(f, (1_600_000_000, 1_600_000_000))
    wm = {}
    first = _collect(pi, tmp_path, wm)
    assert len(_acts(first, "turn")) == 1
    _append(f, tail)
    second = _collect(pi, tmp_path, wm)
    [commit] = _acts(second, "commit")
    assert commit.attrs["sha"] == "1a2b3c4"
    combined = {e.event_id: e for e in first + second}
    full = {e.event_id: e for e in _collect(pi, tmp_path)}
    assert combined == full


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
        entry["dev"] = 4                 # an old number, which a restart would have changed
        del wm["state_v"]                # and this state predates the version
        assert _collect(pi, home, wm) == [], "nothing new: nothing read again"
        assert (entry["off"], entry["ino"]) == (off, ino)
        _append(f, [_pu("u2", T(3), "more"), _pa("a2", T(4), "stop", tokens=9)])
        later = _collect(pi, home, wm)
        assert [u.event_id for u in _usage(later)] == [make_event_id("pi", "a2")], later  # new only
        assert [h.session.turn_id for h in _usage(later, "human")] == ["u2"]


UUID = "0f0e0d0c-0b0a-4909-8807-060504030201"


def test_cost_total_keeps_only_a_finite_number():
    assert pi._cost_total(0.5) == 0.5 and pi._cost_total({"total": 2}) == 2.0
    for bad in (None, "1.5", True, {"total": None}, {"input": 1}, 10 ** 400, float("nan"),
                float("inf"), {"total": -float("inf")}):
        assert pi._cost_total(bad) is None, bad


def test_uuid_in_reads_only_a_file_name_that_ends_in_one():
    assert pi.uuid_in(f"/x/2026_{UUID}.jsonl") == UUID
    assert pi.uuid_in(f"/x/{UUID}/session.jsonl") is None
    assert pi.uuid_in(f"/x/2026_{UUID}.txt") is None


def test_header_without_a_parent_falls_back_to_the_file_name():
    hdr = pi.header({"timestamp": "t", "cwd": "/w/repo"}, f"/x/2026_{UUID}.jsonl")
    assert hdr == {"id": UUID, "ts": "t", "cwd": "/w/repo", "parent": None, "fork": False}
    assert pi.header({"id": "own", "cwd": 5}, "/x/plain.jsonl")["id"] == "own"
    assert pi.header({"cwd": 5}, "/x/plain.jsonl") == {"id": "plain", "ts": None, "cwd": None,
                                                       "parent": None, "fork": False}


def test_header_finds_a_forks_parent_and_never_keeps_its_path(tmp_path):
    parent = tmp_path / f"2026_{UUID}.jsonl"
    parent.write_text(json.dumps({"type": "session", "id": "real-parent"}) + "\n")
    hdr = pi.header({"id": "f", "parentSession": str(parent)}, "/x/f.jsonl")
    assert hdr["parent"] == "real-parent" and hdr["fork"] is True
    assert str(tmp_path) not in json.dumps(hdr)
    # the parent's file is gone, or its first line is no header: the uuid in the name stands in
    assert pi.header({"parentSession": f"/gone/2026_{UUID}.jsonl"}, "/x/f.jsonl")["parent"] == UUID
    parent.write_text(json.dumps({"type": "message", "id": "not-a-header"}) + "\n")
    assert pi.header({"parentSession": str(parent)}, "/x/f.jsonl")["parent"] == UUID
    # no uuid to go on: still a fork, with no parent
    hdr = pi.header({"parentSession": "/gone/plain"}, "/x/f.jsonl")
    assert hdr["parent"] is None and hdr["fork"] is True


def test_is_fork_copy_means_older_than_a_forks_header():
    fork = {"fork": True, "ts": T(10)}
    assert pi.is_fork_copy({"timestamp": T(5)}, fork)
    assert not pi.is_fork_copy({"timestamp": T(10)}, fork)
    assert not pi.is_fork_copy({"timestamp": T(15)}, fork)
    assert not pi.is_fork_copy({}, fork)
    assert not pi.is_fork_copy({"timestamp": T(5)}, {"fork": False, "ts": T(10)})


def test_note_model_change_keeps_what_the_record_does_not_name():
    cur = {"model": "old", "provider": "p"}
    pi._note_model_change({"modelId": "new"}, cur)
    assert cur == {"model": "new", "provider": "p"}
    pi._note_model_change({"model": "other", "provider": "q"}, cur)
    assert cur == {"model": "other", "provider": "q"}
    pi._note_model_change({}, cur)
    assert cur == {"model": "other", "provider": "q"}


def test_record_id_falls_back_to_the_path_and_time():
    assert pi._record_id({"id": "e"}, "/p") == "e"
    assert pi._record_id({"timestamp": "t"}, "/p") == "/p:t"


def _usage_of(o, msg=None, cur=None):
    return pi._usage_event(o, msg or {}, cur or {"model": "fallback", "provider": "fb"}, "/p", "repo", None)


def test_usage_event_reads_a_message_and_uses_the_streamed_model_when_it_has_none():
    msg = {"usage": {"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4, "reasoning": 1,
                     "totalTokens": 10, "cost": {"total": 0.5}}, "model": "m", "provider": "p"}
    e = _usage_of({"type": "message", "id": "e", "timestamp": "t"}, msg)
    assert (e.input_tokens, e.output_tokens, e.cache_read_tokens, e.cache_write_tokens) == (1, 2, 3, 4)
    assert (e.model, e.provider, e.est_cost_usd, e.project, e.ts) == ("m", "p", 0.5, "repo", "t")
    assert (e.total_tokens, e.reasoning_tokens) == (10, 1)
    plain = _usage_of({"type": "message", "id": "e"}, {"usage": {"input": 1, "output": 1}})
    assert (plain.model, plain.provider, plain.total_tokens) == ("fallback", "fb", 2)
    assert plain.reasoning_tokens is None and plain.est_cost_usd is None


def test_usage_event_of_a_compaction_is_the_records_own_usage():
    e = _usage_of({"type": "compaction", "id": "c", "usage": {"input": 5}, "model": "ignored"})
    assert (e.input_tokens, e.total_tokens, e.model) == (5, 5, "fallback")


def test_usage_event_is_none_without_tokens_and_raises_on_a_bad_count():
    assert _usage_of({"type": "model_change", "usage": {"input": 1}}) is None
    assert _usage_of({"type": "message"}, {"usage": "x"}) is None
    assert _usage_of({"type": "message"}, {"usage": {"input": 0}}) is None
    with pytest.raises(ValueError):
        _usage_of({"type": "message"}, {"usage": {"input": "many"}})


def test_advance_session_opens_a_turn_on_a_prompt_and_skips_a_fork_copy():
    hdr = {"id": "s", "parent": "p", "fork": True, "ts": T(10)}
    cur = {}
    assert pi._advance_session({"timestamp": T(5), "id": "e0"}, hdr, cur, True) is None and cur == {}
    s = pi._advance_session({"timestamp": T(11), "id": "e1"}, hdr, cur, True)
    assert (s.session_id, s.parent_session_id, s.turn_id) == ("s", "p", "e1")
    assert pi._advance_session({"timestamp": T(12), "id": "e2"}, hdr, cur, False).turn_id == "e1"
    # a prompt with no usable id stays in the turn it is in
    assert pi._advance_session({"timestamp": T(13), "id": 5}, hdr, cur, True).turn_id == "e1"


def test_upgrade_state_adds_the_header_once_and_pads_pending_calls(tmp_path):
    f = tmp_path / f"2026_{UUID}.jsonl"
    f.write_text(json.dumps({"type": "session", "id": "hdr-id", "cwd": "/w"}) + "\n")
    wm = {"files": {str(f): {"off": 10, "act": {"pend": {"c": [False, None]}}},
                    "/gone.jsonl": {"off": 10},
                    "/fresh.jsonl": {"off": 0}}}
    pi._upgrade_state(wm)
    entry = wm["files"][str(f)]
    assert entry["hdr"]["id"] == "hdr-id" and entry["act"]["pend"]["c"] == [False, None, None]
    assert "hdr" not in wm["files"]["/gone.jsonl"] and "hdr" not in wm["files"]["/fresh.jsonl"]
    assert wm["state_v"] == pi._STATE_V
    f.unlink()
    entry["hdr"]["id"] = "kept"
    pi._upgrade_state(wm)                  # already this version: no step runs again
    assert entry["hdr"]["id"] == "kept"
    fresh = {}
    pi._upgrade_state(fresh)
    assert fresh == {"files": {}, "state_v": pi._STATE_V}


def test_trim_state_keeps_only_what_carries_over():
    cur = {"act": {"link": {}}}
    pi._trim_state(cur)
    assert "act" not in cur
    cur = {"act": {"link": {}, "open": {"id": "e"}}}
    pi._trim_state(cur)
    assert cur == {"act": {"open": {"id": "e"}}}
    cur = {"act": {"link": {"repo": 1}}}
    pi._trim_state(cur)
    assert cur == {"act": {"link": {"repo": 1}}}
