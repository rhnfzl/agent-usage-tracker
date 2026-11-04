"""Hermetic tests for the Codex collector: usage, sessions and activity from rollout logs.

Locks the rules:
  - Codex subtracts cached from input and uses the per-turn delta, keeps reasoning;
  - identity from the FIRST session_meta only (a fork's second meta is the parent's), run_origin
    from source / thread_source / originator, turn_id from task_started carried across scans,
    usage event_ids unchanged, UserMessage items count only in a file with no user_message;
  - activity: steer, interrupt, turn (tool aggregates, errors, duration), commit, compaction.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path

from agent_usage_tracker.collectors import codex
from agent_usage_tracker.event import UsageEvent
from agent_usage_tracker.event import make_event_id
from codex_pi_helpers import (T, _acts, _append, _call, _cfg, _collect, _ev, _item,
                              _meta, _run, _tok, _usage, _write_jsonl)
from test_activity import _key
from test_collectors import _drain


def test_codex_human_turn_classification() -> None:
    # Same question for codex, but the signal is structural: session_meta records who is driving.
    def _session(meta: dict, messages: list[str]) -> list[dict]:
        recs: list[dict] = [{"type": "session_meta", "payload": {
            "session_id": meta.get("id", "s"), "model_provider": "openai",
            "cwd": "/x/repo", **meta}}]
        for i, msg in enumerate(messages):
            recs.append({"type": "event_msg", "timestamp": f"2026-07-18T10:0{i}:00Z",
                         "payload": {"type": "user_message", "message": msg}})
        return recs

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        root = home / ".codex/sessions/2026/07/18"
        # a person in the interactive TUI -> both count
        _write_jsonl(root / "rollout-tui.jsonl", _session(
            {"id": "s1", "originator": "codex-tui", "source": "cli"},
            ["why is this failing?", "now fix it"]))
        # a thread another agent spawned
        _write_jsonl(root / "rollout-sub.jsonl", _session(
            {"id": "s2", "originator": "codex-tui",
             "source": {"subagent": {"thread_spawn": {"depth": 1}}}}, ["go review this"]))
        # scripted `codex exec`, and a programmatic MCP call
        _write_jsonl(root / "rollout-exec.jsonl", _session(
            {"id": "s3", "originator": "codex_exec", "source": "exec"}, ["orchestrate a review"]))
        _write_jsonl(root / "rollout-mcp.jsonl", _session(
            {"id": "s4", "originator": "codex_cli_rs", "source": "mcp"}, ["answer this"]))
        # another coding agent driving codex through its integration
        _write_jsonl(root / "rollout-cc.jsonl", _session(
            {"id": "s5", "originator": "Claude Code", "source": "vscode"}, ["<task>review</task>"]))
        # Codex Desktop: a scheduled automation, then a real message in the same session
        _write_jsonl(root / "rollout-desk.jsonl", _session(
            {"id": "s6", "originator": "Codex Desktop", "source": "vscode"},
            ["Automation: Standup summary\nAutomation ID: standup-summary\nmemory: ...",
             "Automation is the topic I want to discuss"]))   # NOT an automation preamble

        human = [e for e in _drain(codex.collect, _cfg(home)) if e.kind == "human"]
        assert len(human) == 3, [(e.event_id, e.ts) for e in human]


def test_codex() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".codex/sessions/2026/07/18/rollout-a.jsonl", [
            {"type": "session_meta", "payload": {"session_id": "sess1",
             "model_provider": "openai", "cwd": "/x/repo"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            {"type": "event_msg", "timestamp": "2026-07-18T10:00:01Z",
             "payload": {"type": "token_count", "info": {"last_token_usage": {
                 "input_tokens": 1000, "cached_input_tokens": 900,
                 "output_tokens": 40, "reasoning_output_tokens": 15, "total_tokens": 1040}}}},
            # second turn: its OWN delta, not a cumulative running total
            {"type": "event_msg", "timestamp": "2026-07-18T10:00:05Z",
             "payload": {"type": "token_count", "info": {"last_token_usage": {
                 "input_tokens": 500, "cached_input_tokens": 300,
                 "output_tokens": 30, "reasoning_output_tokens": 5, "total_tokens": 530}}}},
            # zero-token snapshot -> filtered
            {"type": "event_msg", "timestamp": "2026-07-18T10:00:09Z",
             "payload": {"type": "token_count", "info": {"last_token_usage": {
                 "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                 "reasoning_output_tokens": 0, "total_tokens": 0}}}},
        ])
        ev = _drain(codex.collect, _cfg(home))
        assert len(ev) == 2, ev                                 # 2 non-zero turns, zero filtered
        first = next(e for e in ev if e.output_tokens == 40)
        second = next(e for e in ev if e.output_tokens == 30)
        assert first.input_tokens == 100 and first.cache_read_tokens == 900   # 1000 - 900
        assert first.reasoning_tokens == 15 and first.total_tokens == 1040
        assert second.input_tokens == 200 and second.cache_read_tokens == 300  # 500 - 300, its own delta
        assert second.total_tokens == 530                       # per-turn, not 1040+530 cumulative
        assert all(e.model == "gpt-5.6-sol" and e.provider == "openai" for e in ev)
        assert all(e.project == "repo" for e in ev)
        assert first.event_id != second.event_id                # content-hash ids are distinct


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
        assert [m(p) for m, p in [(codex.run_origin, {"source": "cli"}),
                                  (codex.run_origin, {"source": "mcp"}),
                                  (codex.run_origin, {"source": "vscode",
                                                       "originator": "Claude Code"}),
                                  (codex.run_origin, {})]] == \
            ["interactive", "automated", "automated", "unknown"]
    _run(check)


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
        del wm["state_v"]           # state from before identity was kept has no version either
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


def test_codex_activity_errors_never_block_usage(monkeypatch) -> None:
    def check(home, repo):
        monkeypatch.setattr(codex, "commit_call", lambda cmd: 1 / 0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            evs = _collect(codex, home)
        assert not _acts(evs, "commit") and _acts(evs, "turn")
        assert len(_usage(evs)) == 5 and len(_usage(evs, "human")) == 4
        assert "activity record(s) skipped" in err.getvalue(), err.getvalue()
    _run(check)
