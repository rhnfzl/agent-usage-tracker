"""Hermetic tests for the link rows of Claude Code sessions: sources, order, roles and idle splits.

Run: `python tests/test_links_claude.py`  (or `pytest`).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.event import ActivityEvent  # noqa: E402
from agent_usage_tracker.event import make_event_id  # noqa: E402
from test_activity import (_base, _collect, _key, _result, _tools, _turn_end, _use,  # noqa: E402
                           _user, _write_jsonl)
from test_links import K1, K2, OLD, SHA_A, SHA_B, _bk, _links, _repos, _sk  # noqa: E402


def test_claude_sources_and_order() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, two = _repos(home)
        c1 = str(one)
        _write_jsonl(home / ".claude/projects/-p/S1.jsonl", [
            # turn 1: the commit line and the commit's own directory beat the record's cwd
            _user("h1", f"look at {SHA_A}", cwd=c1, gitBranch="feat/ABC-1-a"),
            _tools("m1", {"type": "text", "text": "PR https://github.com/o/r/pull/5"},
                   _use("c1", "Bash", command=f"cd {two} && git commit -m x"), cwd=c1,
                   gitBranch="feat/ABC-1-a"),
            _result("r1", "c1", "[fix/QA-2-b 9f8e7d6] fix QA-3 thing\n 1 file changed", cwd=c1),
            _turn_end("td1", 5, 3),
            # turn 2: no commit, the record's cwd and gitBranch
            _user("h2", "next", cwd=c1, gitBranch="feat/ABC-1-a"),
            _turn_end("td2", 5, 1),
            # turn 3: a worktree beats the record; it holds for later turns until it is left
            _base(type="worktree-state", worktreeSession={
                "originalCwd": str(two), "worktreePath": "/gone/wt", "worktreeBranch": "wt/x",
                "worktreeName": "x", "sessionId": "S1"}),
            _user("h3", "in the worktree", cwd="/gone/wt", gitBranch="wt/x"),
            _turn_end("td3", 5, 1),
            _user("h4", "still there", cwd="/gone/wt", gitBranch="wt/x"),
            _turn_end("td4", 5, 1),
            _base(type="worktree-state", worktreeSession=None),
            # turn 5: a default branch gives no branch key
            _user("h5", "back", cwd=c1, gitBranch="main"),
            _base(type="ai-title", aiTitle="Fix ZZ-9 import"),
            _turn_end("td5", 5, 1),
        ])
        evs = _collect(home)
        links = {e.event_id: e for e in _links(evs)}
        l1, l2, l3, l4, l5 = (links[make_event_id("claude_code", "link", f"td{i}")]
                              for i in range(1, 6))
        assert l1.attrs == {
            "repo_key": K2, "branch_key": _bk(K2, "fix/QA-2-b"),
            "ticket_keys": [_key("ABC-1"), _key("QA-2"), _key("QA-3")],
            "sha_refs": [_sk(SHA_A), _sk("9f8e7d6")], "commit_keys": [_sk("9f8e7d6")],
            "pr_keys": [_key("github.com/o/r|5")],
            "role": "seed"}, l1.attrs
        assert (l1.ts, l1.session_id) == (_base()["timestamp"], "S1")
        [commit] = [e for e in evs if isinstance(e, ActivityEvent) and e.kind == "commit"]
        assert commit.attrs["repo_key"] == K2, commit.attrs        # the cd target, not the cwd
        assert (l2.attrs["repo_key"], l2.attrs["branch_key"]) == (K1, _bk(K1, "feat/ABC-1-a"))
        assert l2.attrs["sha_refs"] == [] and l2.attrs["pr_keys"] == []      # reset per turn
        for lw in (l3, l4):
            assert (lw.attrs["repo_key"], lw.attrs["branch_key"]) == (K2, _bk(K2, "wt/x")), \
                lw.attrs
        assert (l5.attrs["repo_key"], l5.attrs["branch_key"]) == (K1, None), l5.attrs
        assert l5.attrs["ticket_keys"] == [_key("ZZ-9")], l5.attrs          # from the title
        assert len(links) == 5
    print("ok claude_sources_and_order")


def test_claude_commit_repo_fallbacks() -> None:
    # cwd gone: the commit's -C target, else the worktree's original checkout, else NULL
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, two = _repos(home)
        gone = "/gone/dir"
        _write_jsonl(home / ".claude/projects/-p/S1.jsonl", [
            _user("h1", "go", cwd=gone),
            _tools("m1", _use("c1", "Bash", command=f"git -C {one} commit -m x"),
                   _use("c2", "Bash", command="git commit -m y"), cwd=gone),
            _result("r1", "c1", "[feat/x 1111aaa] x", cwd=gone),
            _result("r2", "c2", "[feat/x 2222bbb] y", cwd=gone),
            _base(type="worktree-state", worktreeSession={
                "originalCwd": str(two), "worktreePath": gone, "worktreeBranch": "feat/x"}),
            _tools("m2", _use("c3", "Bash", command="git commit -m z"), cwd=gone),
            _result("r3", "c3", "[feat/x 3333ccc] z", cwd=gone),
            _turn_end("td1", 5, 3),
        ])
        evs = _collect(home)
        got = {e.attrs["sha"]: e.attrs["repo_key"] for e in evs
               if isinstance(e, ActivityEvent) and e.kind == "commit"}
        assert got == {"1111aaa": K1, "2222bbb": None, "3333ccc": K2}, got
    print("ok claude_commit_repo_fallbacks")


def test_claude_roles_and_idle_close() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, _two = _repos(home)
        c1 = str(one)
        proj = home / ".claude/projects/-p"
        sub = proj / "S1/subagents/agent-A1.jsonl"
        sdk = proj / "S9.jsonl"
        _write_jsonl(sub, [
            _user("s1", "work", agentId="A1", isSidechain=True, cwd=c1, gitBranch="feat/y"),
            _tools("sm1", {"type": "text", "text": f"done {SHA_B}"}, agentId="A1", cwd=c1,
                   gitBranch="feat/y"),
        ])
        _write_jsonl(sdk, [_user("k1", "run", sessionId="S9", entrypoint="sdk-py", cwd=c1),
                           _tools("km1", {"type": "text", "text": "ok"}, sessionId="S9",
                                  entrypoint="sdk-py", cwd=c1)])
        wm: dict = {}
        assert not _links(_collect(home, wm)), "an active file keeps its turn open"
        assert wm["files"][str(sub)]["act"]["lo"]
        os.utime(sub, (OLD, OLD))
        os.utime(sdk, (OLD, OLD))
        later = _links(_collect(home, wm))                 # no new lines, but now idle
        assert {(e.session_id, e.attrs["role"]) for e in later} == {("A1", "child"),
                                                                    ("S9", "child")}, later
        [la] = [e for e in later if e.session_id == "A1"]
        assert (la.attrs["repo_key"], la.attrs["branch_key"], la.attrs["sha_refs"]) == \
            (K1, _bk(K1, "feat/y"), [_sk(SHA_B)]), la.attrs
        assert (la.event_id, la.turn_id) == (make_event_id("claude_code", "link", "a-sm1"), "P-s1")
        assert "act" not in wm["files"][str(sub)], "closed: nothing left to carry"
        assert not _links(_collect(home, wm)), "closed once"
    print("ok claude_roles_and_idle_close")


def test_claude_backfill_determinism() -> None:
    # A live pass that sees the file in two scans gives exactly the rows of one full read.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, two = _repos(home)
        c1 = str(one)
        main = home / ".claude/projects/-p/S1.jsonl"
        sub = home / ".claude/projects/-p/S1/subagents/agent-A1.jsonl"
        recs = [
            _user("h1", "go https://bitbucket.org/t/r/pull-requests/3", cwd=c1, gitBranch="f/a"),
            _tools("m1", _use("c1", "Bash", command=f"git -C {two} commit -m x"), cwd=c1),
            _result("r1", "c1", f"[f/b {SHA_A}] AB-1 x", cwd=c1),
            _tools("m2", _use("x1", "Bash", command="git log"), cwd=c1),
            _result("r2", "x1", f"{SHA_B} older", cwd=c1),
            _turn_end("td1", 5, 3),
            _user("h2", "and then", cwd=c1, gitBranch="f/a"),
            _tools("m3", {"type": "text", "text": "fine"}, cwd=c1),
        ]
        srecs = [_user("s1", "work", agentId="A1", isSidechain=True, cwd=c1),
                 _tools("sm1", {"type": "text", "text": SHA_A}, agentId="A1", cwd=c1),
                 _tools("sm2", {"type": "text", "text": "done"}, agentId="A1", cwd=c1)]
        for cut in range(1, len(recs)):
            _write_jsonl(main, recs[:cut])
            _write_jsonl(sub, srecs[:min(cut, len(srecs))])
            wm: dict = {}
            live = _collect(home, wm)
            with main.open("a") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in recs[cut:])
            with sub.open("a") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in srecs[cut:])
            for f in (main, sub):
                os.utime(f, (OLD, OLD))
            live += _collect(home, wm)
            full = _collect(home)

            def view(evs):
                return sorted((e.event_id, e.ts, e.session_id, e.turn_id,
                               json.dumps(e.attrs, sort_keys=True)) for e in _links(evs))
            assert view(live) == view(full), (cut, view(live), view(full))
            assert len(view(full)) == 3, view(full)       # td1, the idle tail of S1, the subagent
    print("ok claude_backfill_determinism")


def test_claude_idle_gap_split_is_deterministic() -> None:
    # A turn left open for an hour or more closes at its last record whether a live scan saw the
    # file idle or a full read only sees the gap between record timestamps.
    t0, t1 = "2026-07-18T10:00:00Z", "2026-07-18T12:00:00Z"
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        one, _two = _repos(home)
        c1 = str(one)
        main = home / ".claude/projects/-p/S1.jsonl"
        recs = [
            _user("h1", f"go {SHA_A}", cwd=c1, timestamp=t0),
            _tools("m1", {"type": "text", "text": "waiting"}, cwd=c1, timestamp=t0),
            _result("r1", "x", f"back {SHA_B}", cwd=c1, timestamp=t1),
            _tools("m2", {"type": "text", "text": "done"}, cwd=c1, timestamp=t1),
            _turn_end("td1", 5, 3) | {"timestamp": t1},
        ]

        def view(evs):
            return sorted((e.event_id, e.ts, json.dumps(e.attrs, sort_keys=True))
                          for e in _links(evs))
        _write_jsonl(main, recs)
        full = view(_collect(home))
        assert [r[0] for r in full] == sorted([make_event_id("claude_code", "link", "a-m1"),
                                               make_event_id("claude_code", "link", "td1")]), full
        for idle in (True, False):
            _write_jsonl(main, recs[:2])
            if idle:
                os.utime(main, (OLD, OLD))          # a scan ran while the file sat idle
            wm: dict = {}
            live = _collect(home, wm)
            with main.open("a") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in recs[2:])
            live += _collect(home, wm)
            assert view(live) == full, (idle, view(live), full)
    print("ok claude_idle_gap_split_is_deterministic")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
