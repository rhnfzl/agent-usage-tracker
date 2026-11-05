"""Hermetic tests for the Claude Code collector on synthetic fixtures.

Locks the rule: Claude Code captures all four token classes (the "undercount" trap).
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path

from agent_usage_tracker.collectors import claude_code
from agent_usage_tracker.config import Config
from agent_usage_tracker.event import UsageEvent
from test_collectors import _cfg, _drain, _write_jsonl


def test_claude_code() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".claude/projects/p/s.jsonl", [
            {"type": "user", "message": {"role": "user"}},
            {"type": "assistant", "uuid": "u1", "timestamp": "2026-07-18T10:00:00Z",
             "cwd": "/x/myproj", "message": {"model": "claude-opus-4-8", "usage": {
                 "input_tokens": 2, "output_tokens": 500,
                 "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300}}},
            # zero-token assistant record -> must be filtered out
            {"type": "assistant", "uuid": "u2", "timestamp": "2026-07-18T10:01:00Z",
             "message": {"model": "claude-opus-4-8", "usage": {
                 "input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        ])
        ev = _drain(claude_code.collect, _cfg(home))
        assert len(ev) == 1, ev
        e = ev[0]
        assert e.input_tokens == 2 and e.output_tokens == 500
        assert e.cache_read_tokens == 1000 and e.cache_write_tokens == 300
        assert e.total_tokens == 1802, e.total_tokens          # all four, not just input
        assert e.model == "claude-opus-4-8" and e.provider == "anthropic"
        assert e.project == "myproj"


def test_duplicate_session_paths_counted_once() -> None:
    # Claude Code re-encodes a project directory, so one session file can sit at two paths and
    # be globbed twice. Both copies emit the same event_ids, so the sink stores one -- the scan
    # report and the CSV export have to agree with the sink, not with the raw emit count.
    import io

    from agent_usage_tracker.scan import run_export, run_scan
    records = [
        {"type": "user", "uuid": "h1", "timestamp": "2026-07-18T10:00:00Z",
         "entrypoint": "cli", "message": {"role": "user", "content": "do the thing"}},
        {"type": "assistant", "uuid": "a1", "requestId": "req_1",
         "timestamp": "2026-07-18T10:00:01Z", "cwd": "/x/proj",
         "message": {"id": "msg_1", "model": "claude-opus-5", "usage": {
             "input_tokens": 10, "output_tokens": 20,
             "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40}}},
    ]
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        # the same session id under two encodings of ONE project -- byte-identical, as on disk
        _write_jsonl(home / ".claude/projects/-Users-me-proj/sess-abc.jsonl", records)
        _write_jsonl(home / ".claude/projects/pre---Users-me-proj/sess-abc.jsonl", records)
        cfg = _cfg(home)

        report = run_scan(cfg, ["claude_code"], dry_run=True)["claude_code"]
        assert report["events"] == 1, report          # not 2
        assert report["human"] == 1, report           # not 2
        assert report["total"] == 100, report         # not 200
        assert report["duplicates"] == 3, report      # usage, human and its steer row, surfaced
        assert report["by_model"]["claude-opus-5"]["events"] == 1

        buf = io.StringIO()
        written, _skipped, _failed = run_export(cfg, ["claude_code"], buf)
        assert written == 2, written                  # 1 human + 1 assistant, each once
        ids = [ln.split(",")[0] for ln in buf.getvalue().strip().splitlines()[1:]]
        assert len(ids) == len(set(ids)), ids         # a COPY load has no ON CONFLICT to save it


def test_claude_code_human_turn_classification() -> None:
    # "human turns" is the denominator of agent_turns_per_human_turn, so anything the harness
    # writes as role=user but nobody typed has to stay out of it.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".claude/projects/p/s.jsonl", [
            {"type": "user", "uuid": "h1", "timestamp": "2026-07-18T10:00:00Z",
             "entrypoint": "cli", "message": {"role": "user", "content": "fix the parser"}},
            # a slash command IS you: kept
            {"type": "user", "uuid": "h2", "timestamp": "2026-07-18T10:00:01Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "<command-name>/review</command-name>"}},
            # harness-injected role=user turns: not typed by anyone
            {"type": "user", "uuid": "x1", "timestamp": "2026-07-18T10:00:02Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"}},
            {"type": "user", "uuid": "x2", "timestamp": "2026-07-18T10:00:03Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "<local-command-stdout>Goodbye!</local-command-stdout>"}},
            {"type": "user", "uuid": "x3", "timestamp": "2026-07-18T10:00:04Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "[Request interrupted by user for tool use]"}},
            # an SDK program driving the session, not a person
            {"type": "user", "uuid": "x4", "timestamp": "2026-07-18T10:00:05Z",
             "entrypoint": "sdk-py", "message": {"role": "user",
             "content": "Review this change for security vulnerabilities."}},
            {"type": "user", "uuid": "x5", "timestamp": "2026-07-18T10:00:06Z",
             "entrypoint": "sdk-cli", "message": {"role": "user", "content": "Analyse the repo."}},
            # unknown entrypoint stays counted: only drop what is positively a program
            {"type": "user", "uuid": "h3", "timestamp": "2026-07-18T10:00:07Z",
             "message": {"role": "user", "content": "and now ship it"}},
            # turnOrigin is authoritative when present: only "human" is a person
            {"type": "user", "uuid": "h4", "timestamp": "2026-07-18T10:00:08Z",
             "entrypoint": "cli", "turnOrigin": "human", "promptSource": "typed",
             "message": {"role": "user", "content": "looks good"}},
            {"type": "user", "uuid": "h5", "timestamp": "2026-07-18T10:00:09Z",
             "entrypoint": "cli", "turnOrigin": "human", "promptSource": "suggestion_accepted",
             "message": {"role": "user", "content": "run the tests"}},
            {"type": "user", "uuid": "x6", "timestamp": "2026-07-18T10:00:10Z",
             "entrypoint": "cli", "turnOrigin": "peer", "promptSource": "system",
             "message": {"role": "user", "content": "please rebase"}},
            {"type": "user", "uuid": "x7", "timestamp": "2026-07-18T10:00:11Z",
             "entrypoint": "cli", "turnOrigin": "scheduled",
             "message": {"role": "user", "content": "<command-message>loop</command-message>"}},
            {"type": "user", "uuid": "x8", "timestamp": "2026-07-18T10:00:12Z",
             "entrypoint": "cli", "turnOrigin": "auto_continuation",
             "message": {"role": "user", "content": "continue"}},
            {"type": "user", "uuid": "x9", "timestamp": "2026-07-18T10:00:13Z",
             "entrypoint": "cli", "promptSource": "system",
             "message": {"role": "user", "content": "status update"}},
            # peer and harness text with no origin fields at all (older and some current files)
            {"type": "user", "uuid": "x10", "timestamp": "2026-07-18T10:00:14Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "Another Claude session sent a message:\n<cross-session>hi</cross-session>"}},
            {"type": "user", "uuid": "x11", "timestamp": "2026-07-18T10:00:15Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "[Cross-session idle notice] worker went idle"}},
            {"type": "user", "uuid": "x12", "timestamp": "2026-07-18T10:00:16Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "<bash-stdout>ok</bash-stdout><bash-stderr></bash-stderr>"}},
            # the `!` command you typed stays a human turn; only its echoed output is dropped
            {"type": "user", "uuid": "h6", "timestamp": "2026-07-18T10:00:17Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "<bash-input>git status</bash-input>"}},
        ])
        human = [e for e in _drain(claude_code.collect, _cfg(home)) if e.kind == "human"]
        assert {e.ts for e in human} == {"2026-07-18T10:00:00Z",    # typed
                                         "2026-07-18T10:00:01Z",    # slash command
                                         "2026-07-18T10:00:07Z",    # unknown entrypoint
                                         "2026-07-18T10:00:08Z",    # turnOrigin human, typed
                                         "2026-07-18T10:00:09Z",    # accepted suggestion
                                         "2026-07-18T10:00:17Z"}, sorted(e.ts for e in human)


def test_claude_code_old_loop_repeats() -> None:
    # Before turnOrigin existed, each /loop firing was written exactly like the /loop you typed.
    # Newer records show every repeat is scheduled, so a file keeps only its first /loop.
    loop = "<command-message>loop</command-message>\n<command-name>/loop</command-name>"
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".claude/projects/p/old.jsonl", [
            {"type": "user", "uuid": f"l{i}", "timestamp": f"2026-05-01T10:0{i}:00Z",
             "entrypoint": "cli", "message": {"role": "user", "content": loop}}
            for i in range(3)
        ] + [
            # other repeated commands are mostly typed by hand: all kept
            {"type": "user", "uuid": f"b{i}", "timestamp": f"2026-05-01T11:0{i}:00Z",
             "entrypoint": "cli", "message": {"role": "user",
             "content": "<command-message>pr-babysit</command-message>"}}
            for i in range(2)
        ])
        # a new file: turnOrigin decides, the repeat rule does not apply
        _write_jsonl(home / ".claude/projects/p/new.jsonl", [
            {"type": "user", "uuid": "n1", "timestamp": "2026-11-01T10:00:00Z",
             "entrypoint": "cli", "turnOrigin": "human", "message": {"role": "user", "content": loop}},
            {"type": "user", "uuid": "n2", "timestamp": "2026-11-01T10:05:00Z",
             "entrypoint": "cli", "turnOrigin": "human", "message": {"role": "user", "content": loop}},
        ])
        human = [e for e in _drain(claude_code.collect, _cfg(home)) if e.kind == "human"]
        assert sorted(e.ts for e in human) == ["2026-05-01T10:00:00Z",   # first old /loop
                                               "2026-05-01T11:00:00Z",   # pr-babysit x2
                                               "2026-05-01T11:01:00Z",
                                               "2026-11-01T10:00:00Z",   # both new, typed
                                               "2026-11-01T10:05:00Z"], sorted(e.ts for e in human)


def test_claude_code_multiblock_dedup() -> None:
    # Claude Code writes one JSONL line per content block, each repeating the message's usage.
    # The collector must emit ONE event per (message.id, requestId) - not one per line (6x bug).
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        blocks = [{"type": "assistant", "uuid": f"u{i}", "requestId": "req_1",
                   "timestamp": "2026-07-18T10:00:00Z", "cwd": "/x/proj",
                   "message": {"id": "msg_1", "model": "claude-opus-4-8", "usage": {
                       "input_tokens": 10, "output_tokens": 20,
                       "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 5}}}
                  for i in range(6)]                   # 6 blocks, identical usage, distinct uuids
        _write_jsonl(home / ".claude/projects/p/s.jsonl", blocks)
        ev = _drain(claude_code.collect, _cfg(home))
        assert len(ev) == 1, ev                        # 6 lines -> ONE event
        assert ev[0].total_tokens == 1035              # counted once, not 6 * 1035
    # cumulative-growing repeats (same message.id) -> keep the max (final) total
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        recs = [{"type": "assistant", "uuid": f"g{i}", "requestId": "req_2",
                 "timestamp": "2026-07-18T10:01:00Z",
                 "message": {"id": "msg_2", "model": "m", "usage": {
                     "input_tokens": 0, "output_tokens": out,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
                for i, out in enumerate((100, 200, 300))]
        _write_jsonl(home / ".claude/projects/p/s.jsonl", recs)
        ev = _drain(claude_code.collect, _cfg(home))
        assert len(ev) == 1 and ev[0].output_tokens == 300, ev   # keeps the final cumulative


def test_claude_truncated_in_place_drops_its_carried_state() -> None:
    from agent_usage_tracker.collectors import claude_code
    user = {"type": "user", "uuid": "u1", "promptId": "p1", "sessionId": "s1", "cwd": "/x",
            "timestamp": "2026-07-18T10:00:00Z", "message": {"role": "user", "content": "go"}}

    def turn(uuid: str) -> dict:
        return {"type": "assistant", "uuid": uuid, "sessionId": "s1", "requestId": uuid,
                "timestamp": "2026-07-18T10:00:01Z", "cwd": "/x", "message": {
                    "id": uuid, "model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}}

    def scan(cfg: Config, wm: dict) -> list:
        rows = []
        for batch, advance in claude_code.collect(cfg, wm):
            rows += [e for e in batch if isinstance(e, UsageEvent) and e.kind == "agent"]
            advance()
        return rows

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        path = home / ".claude" / "projects" / "-p" / "s1.jsonl"
        _write_jsonl(path, [user, turn("a1")])
        wm: dict = {}
        scan(_cfg(home), wm)
        with open(path, "a") as fh:      # an append resumes, and keeps the turn
            fh.write(json.dumps(turn("a2")) + "\n")
        assert [e.session.turn_id for e in scan(_cfg(home), wm)] == ["p1"]
        with open(path, "w") as fh:      # the same inode, now shorter than the offset
            fh.write(json.dumps(turn("a3")) + "\n")
        assert [e.session.turn_id for e in scan(_cfg(home), wm)] == [None]


def test_is_interactive_survives_a_line_that_is_no_record():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "s.jsonl"
        p.write_text('["entrypoint"]\n')
        assert claude_code.is_interactive(p) is False


def test_an_unhashable_field_skips_one_line_and_does_not_wedge_the_file():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".claude/projects/-Users-x-myproj/s1.jsonl", [
            {"type": "system", "entrypoint": []},
            {"type": "assistant", "uuid": "u1", "timestamp": "2026-07-18T10:00:00Z",
             "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 2, "output_tokens": 5}}},
        ])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ev = _drain(claude_code.collect, _cfg(home))
        assert [e.input_tokens for e in ev if e.kind != "human"][:1] == [2], ev
        assert "malformed" in err.getvalue(), err.getvalue()
