"""Hermetic tests for Codex and Pi session + activity rows on synthetic fixtures.

Run: `python tests/test_codex_pi.py`  (or `pytest`).
Locks the rules:
  - codex: identity from the FIRST session_meta only (a fork's second meta is the parent's),
    run_origin from source / thread_source / originator, turn_id from task_started carried across
    scans, usage event_ids unchanged, UserMessage items count only in a file with no user_message;
  - codex activity: steer, interrupt, turn (tool aggregates, errors, duration), commit, compaction;
  - pi: header identity carried in the watermark, reasoning tokens, modelId, project from the
    header cwd, compaction usage, fork copies skipped for session and activity rows;
  - pi activity: steer via, interrupt, commit, derived turn (retry is not an end), compaction,
    invoke;
  - attrs never carry free text; backfill works for both and never touches the watermark.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_activity import _git_repo, _key, _plain_value  # noqa: E402

from agent_usage_tracker import cli  # noqa: E402
from agent_usage_tracker.collectors import codex, pi  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import ActivityEvent, UsageEvent, make_event_id  # noqa: E402

SALT = "test-salt"
TYPED = "please tidy the parser module"
AGAIN = "no, the other way"
LEAKS = (TYPED, AGAIN, "tidy", "clientname", "acmecorp", "ABC-1", "feature", "src/pkg",
         "hello there", "standup")


def T(i: int) -> str:
    return f"2026-07-18T10:00:{i:02d}Z"


def _cfg(home: Path, pg_dsn: str | None = None) -> Config:
    return Config(machine="test", agents=("codex", "pi"), webhook_url=None, webhook_secret=None,
                  pg_dsn=pg_dsn, home=home, repo_key_salt=SALT)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _append(path: Path, records: list[dict]) -> None:
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _collect(mod, home: Path, wm: dict | None = None) -> list:
    out = []
    for events, commit in mod.collect(_cfg(home), {} if wm is None else wm):
        out.extend(events)
        commit()
    return out


def _acts(evs: list, kind: str | None = None, sid: str | None = None) -> list:
    return [e for e in evs if isinstance(e, ActivityEvent) and (kind is None or e.kind == kind)
            and (sid is None or e.session_id == sid)]


def _usage(evs: list, kind: str = "agent", sid: str | None = None) -> list:
    return [e for e in evs if isinstance(e, UsageEvent) and e.kind == kind
            and (sid is None or (e.session and e.session.session_id == sid))]


# ---------------------------------------------------------------- codex fixtures

def _meta(id_: str, cwd: str = "/x/repo", **kw) -> dict:
    return {"type": "session_meta", "timestamp": T(0), "payload": {
        "id": id_, "session_id": kw.pop("session_id", id_), "cwd": cwd,
        "model_provider": "openai", **kw}}


def _ev(ts: str, **p) -> dict:
    return {"type": "event_msg", "timestamp": ts, "payload": p}


def _item(ts: str, turn: str, **item) -> dict:
    return _ev(ts, type="item_completed", turn_id=turn, item=item)


def _call(ts: str, name: str, args: str, **kw) -> dict:
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "function_call", "name": name, "arguments": args, "call_id": "c-" + ts, **kw}}


def _tok(ts: str, inp: int = 10, out: int = 5) -> dict:
    return _ev(ts, type="token_count", info={"last_token_usage": {
        "input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out,
        "reasoning_output_tokens": 0, "total_tokens": inp + out}})


def _um_item(ts: str, turn: str, text: str) -> dict:
    return _item(ts, turn, type="UserMessage", id="um-" + ts,
                 content=[{"type": "text", "text": text, "text_elements": []}])


def _codex_fixture(home: Path, repo: Path) -> None:
    root = home / ".codex/sessions/2026/07/18"
    cwd = str(repo)
    # older-format Codex Desktop: no thread_source; the UserMessage item copies the user_message
    _write_jsonl(root / "rollout-d1.jsonl", [
        _meta("D1", cwd=cwd, originator="Codex Desktop", source="vscode"),
        _ev(T(0), type="task_started", turn_id="T1"),
        _um_item(T(1), "T1", TYPED),
        _ev(T(1), type="user_message", message=TYPED, local_images=["/a.png"], local_audio=[]),
        *[_call(T(2 + i), "exec_command", '{"cmd": "ls"}') for i in range(3)],
        _call(T(5), "lookup", '{"q": "clientname"}', namespace="mcp__acmecorp_internal"),
        # Desktop also logs that call as an McpToolCall item with the same id: not a second call
        _item(T(5), "T1", type="McpToolCall", id="c-" + T(5), server="acmecorp", tool="lookup",
              arguments={"q": "clientname"}, status="completed"),
        # an older CLI MCP name, `<server>__<tool>` with no namespace: the label is free text
        _call(T(5), "acmecorp__lookup", '{"q": "x"}', call_id="c-old"),
        _item(T(6), "T1", type="CommandExecution", id="ce1", cwd=cwd, exit_code=0,
              status="completed",
              command=["/bin/zsh", "-lc", "git add -A && git commit -m 'tidy'"],
              aggregated_output="[feature/ABC-1-clientname 1a2b3c4] tidy\n 1 file changed"),
        _item(T(7), "T1", type="CommandExecution", id="ce2", cwd=cwd, exit_code=1,
              status="failed", command=["/bin/zsh", "-lc", "false"], aggregated_output=""),
        # a Desktop MCP call logged only as an item (no function_call): a call and an error
        _item(T(8), "T1", type="McpToolCall", id="mc1", server="acmecorp", tool="x",
              arguments={"q": "clientname"}, status="failed"),
        _tok(T(8)),
        _ev(T(9), type="task_complete", turn_id="T1"),           # no duration_ms: the gap
        _ev(T(10), type="task_started", turn_id="T2"),
        _ev(T(11), type="turn_aborted", turn_id="T2", reason="interrupted"),
        _ev(T(12), type="task_started", turn_id="T3"),
        _ev(T(13), type="user_message", message=AGAIN, local_images=[], local_audio=[]),
        _item(T(14), "T3", type="ContextCompaction", id="cc1"),
        _ev(T(15), type="task_complete", turn_id="T3", duration_ms=500),
    ])
    # newer Codex Desktop: prompts only as UserMessage items
    _write_jsonl(root / "rollout-d2.jsonl", [
        _meta("D2", originator="Codex Desktop", source="vscode", thread_source="user"),
        _ev(T(0), type="task_started", turn_id="U1"),
        _um_item(T(1), "U1", "hello there"),
        _tok(T(2)),
        _ev(T(3), type="task_started", turn_id="U2"),
        _um_item(T(4), "U2", "and again"),
    ])
    # a Desktop scheduled automation: structural flag, no text preamble
    _write_jsonl(root / "rollout-d3.jsonl", [
        _meta("D3", originator="Codex Desktop", source="vscode", thread_source="automation"),
        _ev(T(0), type="task_started", turn_id="A1"),
        _ev(T(1), type="user_message", message="Run the standup", local_images=[]),
        _tok(T(2)),
    ])
    # a spawned child, then (as in a fork) the parent's own meta on line 2
    _write_jsonl(root / "rollout-c1.jsonl", [
        _meta("C1", session_id="P1", parent_thread_id="P1", originator="codex_exec",
              source={"subagent": {"thread_spawn": {"parent_thread_id": "P1", "depth": 1}}},
              thread_source="subagent"),
        _meta("P1", cwd="/x/other", originator="codex_exec", source="exec"),
        _ev(T(1), type="task_started", turn_id="CT1"),
        _tok(T(3)),
    ])
    _write_jsonl(root / "rollout-e1.jsonl", [
        _meta("E1", originator="codex_exec", source="exec", thread_source="user"),
        _ev(T(0), type="task_started", turn_id="ET1"),
        _ev(T(1), type="user_message", message="orchestrate", local_images=[]),
        _tok(T(2)),
        _ev(T(3), type="task_complete", turn_id="ET1", duration_ms=42),
    ])


def _run(fn):
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        repo = _git_repo(home, "https://Example.com/Owner/Repo.git")
        _codex_fixture(home, repo)
        _pi_fixture(home, repo)
        return fn(home, repo)


def test_codex_sessions() -> None:
    def check(home, repo):
        evs = _collect(codex, home)
        hum = _usage(evs, "human")
        assert sorted((e.session.session_id, e.session.turn_id) for e in hum) == [
            ("D1", "T1"), ("D1", "T3"), ("D2", "U1"), ("D2", "U2")], hum
        rows = {e.session.session_id: e.session for e in _usage(evs)}
        assert {k: (s.parent_session_id, s.run_origin, s.turn_id) for k, s in rows.items()} == {
            "D1": (None, "interactive", "T1"), "D2": (None, "interactive", "U1"),
            "D3": (None, "automated", "A1"), "C1": ("P1", "subagent", "CT1"),
            "E1": (None, "automated", "ET1")}, rows
        assert all(e.session is not None for e in evs if isinstance(e, UsageEvent))
        # the child's usage row keeps its old event_id (seeded by the legacy session field)
        [child] = _usage(evs, sid="C1")
        assert child.event_id == make_event_id("codex", "P1", T(3), 10, 0, 5, 0, 15, ""), child
        assert child.project == "repo"                       # the parent meta's cwd is ignored
        assert [m(p) for m, p in [(codex._run_origin, {"source": "cli"}),
                                  (codex._run_origin, {"source": "mcp"}),
                                  (codex._run_origin, {"source": "vscode",
                                                       "originator": "Claude Code"}),
                                  (codex._run_origin, {})]] == \
            ["interactive", "automated", "automated", "unknown"]
    _run(check)
    print("ok codex_sessions")


def test_codex_activity() -> None:
    def check(home, repo):
        evs = _collect(codex, home)
        st = _acts(evs, "steer")
        assert sorted((e.session_id, e.attrs["after_interrupt"], e.attrs["images"],
                       e.attrs["words"]) for e in st) == [
            ("D1", False, 1, 5), ("D1", True, 0, 4), ("D2", False, 0, 2), ("D2", False, 0, 2)], st
        assert {e.attrs["via"] for e in st} == {"typed"}
        [i] = _acts(evs, "interrupt")
        assert (i.session_id, i.turn_id, i.attrs) == ("D1", "T2", {}), i
        turns = {e.turn_id: e.attrs for e in _acts(evs, "turn")}
        assert set(turns) == {"T1", "T3", "ET1"}, turns
        t1 = turns["T1"]
        assert (t1["duration_ms"], t1["tool_calls"], t1["tool_errors"], t1["by_tool"],
                t1["max_identical_run"], t1["retry_flag"], t1["runaway_flag"]) == \
            (9000, 6, 2, {"exec_command": 3, "mcp": 3}, 3, True, False), t1
        assert "hook_blocks" not in t1 and "message_count" not in t1
        assert (turns["T3"]["duration_ms"], turns["T3"]["tool_calls"]) == (500, 0)
        assert turns["ET1"]["duration_ms"] == 42
        [c] = _acts(evs, "commit")
        assert c.attrs == {"sha": "1a2b3c4", "repo_key": _key("example.com/owner/repo"),
                           "amend": False}, c.attrs
        assert c.event_id == make_event_id("codex", "commit", "D1|ce1|1a2b3c4")
        [cc] = _acts(evs, "compaction")
        assert (cc.session_id, cc.turn_id, cc.attrs) == ("D1", "T3", {}), cc
    _run(check)
    print("ok codex_activity")


def test_codex_carries_across_scans() -> None:
    # The scan boundary falls mid-turn, and the second scan resumes from a watermark saved before
    # identity was carried (no "meta"): the first line is re-read for it.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = home / ".codex/sessions/2026/07/18/rollout-s.jsonl"
        _write_jsonl(f, [
            _meta("S1", originator="codex-tui", source="cli"),
            _ev(T(0), type="task_started", turn_id="T1"),
            _ev(T(1), type="user_message", message="go", local_images=[]),
            _call(T(2), "exec_command", '{"cmd": "ls"}'),
        ])
        wm: dict = {}
        first = _collect(codex, home, wm)
        assert not _acts(first, "turn")
        fstate = next(iter(wm["files"].values()))
        assert fstate["turn"] == "T1" and fstate["act"]["agg"]["calls"] == 1, fstate
        fstate.pop("meta")
        _append(f, [_item(T(3), "T1", type="CommandExecution", id="x", cwd="/gone", exit_code=2,
                          command=["sh", "-c", "false"], aggregated_output=""),
                    _tok(T(4)), _ev(T(5), type="task_complete", turn_id="T1")])
        second = _collect(codex, home, wm)
        [t] = _acts(second, "turn")
        assert (t.turn_id, t.attrs["tool_calls"], t.attrs["tool_errors"], t.attrs["duration_ms"]) \
            == ("T1", 1, 1, 5000), t.attrs
        [u] = _usage(second)
        assert (u.session.session_id, u.session.run_origin, u.session.turn_id) == \
            ("S1", "interactive", "T1"), u.session
        assert "act" not in next(iter(wm["files"].values())), "nothing left to carry"
    print("ok codex_carries_across_scans")


def test_codex_fork_copy_is_not_activity() -> None:
    # A forked child starts with a copy of its parent's history: lines before
    # subagent_history_start_ordinal (0-based) are the parent's turn, including a turn_aborted
    # codex writes at the fork point. None of it is the child's activity or turn. The copy is
    # split across two scans to check the count carries. A spawned (not forked) child also has an
    # ordinal, equal to its line count, and must not be skipped.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        root = home / ".codex/sessions/2026/07/18"
        f = root / "rollout-f.jsonl"
        copied = [
            _meta("F1", parent_thread_id="P9", forked_from_id="P9",
                  subagent_history_start_ordinal=7,
                  source={"subagent": {"thread_spawn": {"parent_thread_id": "P9"}}}),
            _meta("P9", originator="Codex Desktop", source="vscode"),
            _ev(T(0), type="task_started", turn_id="PT"),
            _call(T(0), "exec_command", '{"cmd": "ls"}'),
            _item(T(0), "PT", type="CommandExecution", id="pc", cwd="/x", exit_code=0,
                  command=["sh", "-c", "git commit -m x"], aggregated_output="[main 9f9f9f9] x"),
            _ev(T(0), type="task_complete", turn_id="PT"),
            _ev(T(0), type="turn_aborted", turn_id="PT", reason="interrupted"),
        ]
        own = [
            _ev(T(1), type="thread_settings_applied"),
            _ev(T(1), type="task_started", turn_id="FT1"),
            _call(T(2), "exec_command", '{"cmd": "pwd"}'),
            _tok(T(3)),
            _ev(T(4), type="task_complete", turn_id="FT1"),
        ]
        _write_jsonl(f, copied[:4])
        _write_jsonl(root / "rollout-s.jsonl", [
            _meta("S2", parent_thread_id="P9", subagent_history_start_ordinal=4,
                  source={"subagent": {"thread_spawn": {"parent_thread_id": "P9"}}}),
            _ev(T(0), type="task_started", turn_id="ST1"),
            _tok(T(1)),
            _ev(T(2), type="task_complete", turn_id="ST1"),
        ])
        wm: dict = {}
        evs = _collect(codex, home, wm)
        _append(f, copied[4:] + own)
        evs += _collect(codex, home, wm)
        assert not _acts(evs, "interrupt") and not _acts(evs, "commit"), _acts(evs)
        assert sorted((e.session_id, e.turn_id, e.attrs["tool_calls"])
                      for e in _acts(evs, "turn")) == [("F1", "FT1", 1), ("S2", "ST1", 0)]
        assert sorted((u.session.session_id, u.session.turn_id) for u in _usage(evs)) == [
            ("F1", "FT1"), ("S2", "ST1")]
        assert all("skip" not in s for s in wm["files"].values()), "nothing left to carry"
    print("ok codex_fork_copy_is_not_activity")


def test_codex_activity_errors_never_block_usage() -> None:
    def check(home, repo):
        saved = codex.commit_call
        codex.commit_call = lambda cmd: 1 / 0
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                evs = _collect(codex, home)
        finally:
            codex.commit_call = saved
        assert not _acts(evs, "commit") and _acts(evs, "turn")
        assert len(_usage(evs)) == 5 and len(_usage(evs, "human")) == 4
        assert "activity record(s) skipped" in err.getvalue(), err.getvalue()
    _run(check)
    print("ok codex_activity_errors_never_block_usage")


# ---------------------------------------------------------------- pi fixtures

SID = "0f0e0d0c-0b0a-4909-8807-060504030201"
FORK = "1a1b1c1d-1e1f-4a2b-8c3d-4e5f60718293"


def _hdr(id_: str | None, cwd: str, ts: str = T(0), **kw) -> dict:
    h = {"type": "session", "version": 3, "timestamp": ts, "cwd": cwd, **kw}
    if id_:
        h["id"] = id_
    return h


def _msg(eid: str, ts: str, role: str, content, **m) -> dict:
    return {"type": "message", "id": eid, "parentId": None, "timestamp": ts,
            "message": {"role": role, "content": content, "timestamp": 0, **m}}


def _pu(eid: str, ts: str, text: str) -> dict:
    return _msg(eid, ts, "user", [{"type": "text", "text": text}])


def _pa(eid: str, ts: str, stop: str, *calls: tuple, tokens: int = 0, reasoning=None) -> dict:
    usage = {"input": tokens, "output": tokens, "cacheRead": 0, "cacheWrite": 0,
             "totalTokens": 2 * tokens, "cost": {"total": 0.0}}
    if reasoning is not None:
        usage["reasoning"] = reasoning
    content = [{"type": "toolCall", "id": k, "name": n, "arguments": a} for k, n, a in calls]
    return _msg(eid, ts, "assistant", content, api="x", provider="prov", model="model-a",
                usage=usage, stopReason=stop)


def _pr(eid: str, ts: str, call: str, text: str, err: bool = False) -> dict:
    return _msg(eid, ts, "toolResult", [{"type": "text", "text": text}], toolCallId=call,
                toolName="bash", isError=err)


def _pi_main(cwd: str) -> list[dict]:
    return [
        _hdr(SID, cwd),
        {"type": "model_change", "id": "e0", "parentId": None, "timestamp": T(0),
         "provider": "prov", "modelId": "model-a"},
        _pu("u1", T(1), TYPED),
        _pa("a1", T(2), "toolUse", ("k1", "bash", {"command": "git commit -m 'tidy'"}),
            ("k2", "read", {"path": "src/pkg/clientname.py"}), tokens=10, reasoning=5),
        _pr("r1", T(3), "k1", "[feature/ABC-1-clientname 1a2b3c4] tidy"),
        _pr("r2", T(4), "k2", "no such file", err=True),
        _pa("a2", T(5), "stop", tokens=3),
        _pu("u2", T(6), '<skill name="my-skill" location="/a/b">\nbody\n</skill>'),
        _pa("a3", T(7), "toolUse", ("k3", "bash", {"command": "ls"})),
        _pr("r3", T(8), "k3", "x"),
        _pa("a4", T(9), "aborted"),
        _pu("u3", T(10), AGAIN),
        _pa("a5", T(11), "toolUse", ("k4", "bash", {"command": "ls"})),
        _pr("r4", T(12), "k4", "x"),
        _pu("u4", T(13), "and run the checks"),
        _pa("a6", T(14), "error"),
        _pa("a7", T(15), "stop", tokens=4),
        {"type": "compaction", "id": "c1", "parentId": None, "timestamp": T(16),
         "summary": "s", "tokensBefore": 5000, "fromHook": False, "usage": {
             "input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0, "reasoning": 10,
             "totalTokens": 150, "cost": {"total": 0.01}}},
        _pu("u5", T(17), "last"),
        _pa("a8", T(18), "toolUse", ("k5", "read", {"path": "x"})),
    ]


def _pi_fixture(home: Path, repo: Path) -> None:
    d = home / ".pi/agent/sessions/--x-repo--"
    main = d / f"2026-07-18T10-00-00-000Z_{SID}.jsonl"
    _write_jsonl(main, _pi_main(str(repo)))
    # a fork: entries copied from the parent keep their ids and timestamps, all before the header
    _write_jsonl(d / f"2026-07-18T11-00-00-000Z_{FORK}.jsonl", [
        _hdr(FORK, str(repo), ts="2026-07-18T11:00:00Z", parentSession=str(main)),
        _pu("u1", T(1), TYPED),
        _pa("a2", T(5), "stop", tokens=3),
        _pu("f1", "2026-07-18T11:00:01Z", "fork prompt"),
        _pa("f2", "2026-07-18T11:00:02Z", "stop", tokens=2),
    ])


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
            make_event_id("pi", "steer", f"{FORK}|f1"), make_event_id("pi", "turn", f"{FORK}|f1")}
        # parent and session fallbacks: the uuid in a file name, never the path
        assert pi._uuid_in(f"/gone/2026-01-01T00-00-00-000Z_{FORK}.jsonl") == FORK
        assert pi._uuid_in("/gone/not-a-session.jsonl") is None
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


# ---------------------------------------------------------------- both

def test_attrs_carry_no_text() -> None:
    def check(home, repo):
        kinds = {"codex": {"steer", "interrupt", "turn", "commit", "compaction"},
                 "pi": {"steer", "interrupt", "turn", "commit", "compaction", "invoke"}}
        for mod in (codex, pi):
            evs = _acts(_collect(mod, home))
            assert {e.kind for e in evs} == kinds[mod.NAME], (mod.NAME, {e.kind for e in evs})
            for e in evs:
                for k, v in e.attrs.items():
                    assert _plain_value(k, v), (mod.NAME, e.kind, k, v)
                blob = json.dumps(e.attrs)
                for leak in (*LEAKS, str(repo)):
                    assert leak not in blob, (mod.NAME, e.kind, leak)
    _run(check)
    print("ok attrs_carry_no_text (codex, pi)")


def test_backfill_codex_pi() -> None:
    def check(home, repo):
        prev = os.environ.get("AUT_STATE")
        with tempfile.TemporaryDirectory() as sd:
            state = Path(sd) / "state.json"
            state.write_text('{"collectors": {"codex": {"files": {"keep": {"off": 7}}}}}')
            os.environ["AUT_STATE"] = str(state)
            before = (state.read_text(), state.stat().st_mtime_ns)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    c = cli.run_session_backfill(_cfg(home), ["codex", "pi"], dry_run=True,
                                                 sessions=True, activity=True)
            finally:
                if prev is None:
                    os.environ.pop("AUT_STATE", None)
                else:
                    os.environ["AUT_STATE"] = prev
            assert (state.read_text(), state.stat().st_mtime_ns) == before, "state untouched"
        assert c["by_origin"] == {"interactive": 6, "automated": 2, "subagent": 1,
                                  "unknown": 11}, c
        assert c["subagent_no_parent"] == 0, c
        assert c["by_kind"] == {"steer": 10, "interrupt": 2, "turn": 8, "commit": 2,
                                "compaction": 2, "invoke": 1}, c
    _run(check)
    print("ok backfill_codex_pi")


def main() -> int:
    test_codex_sessions()
    test_codex_activity()
    test_codex_carries_across_scans()
    test_codex_fork_copy_is_not_activity()
    test_codex_activity_errors_never_block_usage()
    test_pi_usage_and_sessions()
    test_pi_activity()
    test_pi_carries_across_scans()
    test_attrs_carry_no_text()
    test_backfill_codex_pi()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
