"""Hermetic collector tests on synthetic fixtures (no real logs, no PII): Claude Code, and the
rules every collector shares. Each other collector has its own test file.

Run: `python tests/test_collectors.py`  (or `pytest`).
Locks the normalization rules that are easy to get wrong:
  - Claude Code captures all four token classes (the "undercount" trap).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import UsageEvent  # noqa: E402
from agent_usage_tracker.collectors import claude_code, codex, cursor, pi  # noqa: E402


def _cfg(home: Path) -> Config:
    return Config(machine="test", agents=(), webhook_url=None,
                  webhook_secret=None, pg_dsn=None, home=home)


def _drain(collect, cfg) -> list:
    events = []
    for batch, _commit in collect(cfg, {}):
        events.extend(batch)
    return events


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


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
    print("ok claude_code")


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
        written, _skipped = run_export(cfg, ["claude_code"], buf)
        assert written == 2, written                  # 1 human + 1 assistant, each once
        ids = [ln.split(",")[0] for ln in buf.getvalue().strip().splitlines()[1:]]
        assert len(ids) == len(set(ids)), ids         # a COPY load has no ON CONFLICT to save it
    print("ok duplicate_session_paths_counted_once")


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
    print("ok claude_code_human_turn_classification")


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
    print("ok claude_code_old_loop_repeats")


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
    print("ok claude_code_multiblock_dedup")


def test_human_turns() -> None:
    # Human prompts are emitted as kind='human' with zero tokens; the load-bearing filters
    # (tool_result / meta / sidechain for claude_code) keep injected non-prompts out.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".claude/projects/p/s.jsonl", [
            {"type": "user", "uuid": "h1", "timestamp": "2026-07-18T10:00:00Z", "cwd": "/x/proj",
             "message": {"role": "user", "content": "fix the bug"}},                    # real prompt
            {"type": "user", "uuid": "h2", "timestamp": "2026-07-18T10:00:01Z",
             "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},  # excluded
            {"type": "user", "uuid": "h3", "isMeta": True,
             "message": {"role": "user", "content": "meta"}},                           # excluded
            {"type": "user", "uuid": "h4", "isSidechain": True,
             "message": {"role": "user", "content": "subagent"}},                       # excluded
            {"type": "assistant", "uuid": "a1", "timestamp": "2026-07-18T10:00:05Z",
             "message": {"model": "m", "usage": {"input_tokens": 5, "output_tokens": 5,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        ])
        ev = _drain(claude_code.collect, _cfg(home))
        human = [e for e in ev if e.kind == "human"]
        assert len(human) == 1, human                                  # tool_result/meta/sidechain out
        assert human[0].ts == "2026-07-18T10:00:00Z" and human[0].project == "proj"
        assert human[0].model is None and human[0].total_tokens == 0
        assert sum(e.kind == "agent" for e in ev) == 1

    # codex: an event_msg user_message becomes a human turn (distinct id from token_count turns)
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".codex/sessions/2026/07/18/rollout-a.jsonl", [
            {"type": "session_meta", "payload": {"session_id": "s1", "cwd": "/x/repo"}},
            {"type": "event_msg", "timestamp": "2026-07-18T10:00:00Z",
             "payload": {"type": "user_message", "message": "hello"}},
            {"type": "event_msg", "timestamp": "2026-07-18T10:00:02Z",
             "payload": {"type": "token_count", "info": {"last_token_usage": {
                 "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5,
                 "reasoning_output_tokens": 0, "total_tokens": 15}}}},
        ])
        ev = _drain(codex.collect, _cfg(home))
        human = [e for e in ev if e.kind == "human"]
        assert len(human) == 1 and human[0].ts == "2026-07-18T10:00:00Z", human
        assert human[0].agent == "codex" and human[0].project == "repo"
        assert sum(e.kind == "agent" for e in ev) == 1

    # pi: a role==user message becomes a human turn
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".pi/agent/sessions/dir/s.jsonl", [
            {"type": "message", "id": "u1", "timestamp": "2026-07-18T10:00:00Z",
             "message": {"role": "user", "content": "hi"}},
            {"type": "message", "id": "m1", "timestamp": "2026-07-18T10:00:02Z",
             "message": {"role": "assistant", "usage": {"input": 5, "output": 5, "totalTokens": 10}}},
        ])
        ev = _drain(pi.collect, _cfg(home))
        human = [e for e in ev if e.kind == "human"]
        assert len(human) == 1 and human[0].ts == "2026-07-18T10:00:00Z", human
        assert sum(e.kind == "agent" for e in ev) == 1

    # cursor: a type==1 (user) bubble becomes a human turn with the coarse composer timestamp
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        db = home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("composerData:c1", json.dumps({"createdAt": 1700000000000})))
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b1", json.dumps({"bubbleId": "b1", "type": 1})))       # user
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b2", json.dumps({"bubbleId": "b2", "type": 2,
                        "tokenCount": {"inputTokens": 10, "outputTokens": 5}})))          # assistant
        con.commit()
        con.close()
        cfg = _cfg(home)
        cfg.cursor_db_path = str(db)
        ev = _drain(cursor.collect, cfg)
        human = [e for e in ev if e.kind == "human"]
        assert len(human) == 1 and human[0].ts is not None, human      # coarse composer ts
        assert human[0].total_tokens == 0
        assert sum(e.kind == "agent" for e in ev) == 1
    print("ok human_turns")


def test_export_drains_past_cap() -> None:
    # aut export is one-shot ("from scratch"); it must return EVERY event even when a single file
    # exceeds the per-scan _MAX_LINES cap (regression guard for the drain loop).
    import io

    from agent_usage_tracker import scan
    from agent_usage_tracker.collectors import _util
    orig = _util._MAX_LINES
    _util._MAX_LINES = 2                      # tiny cap: 5 turns -> multiple passes
    try:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _write_jsonl(home / ".claude/projects/p/s.jsonl", [
                {"type": "assistant", "uuid": f"u{i}", "timestamp": f"2026-07-18T10:00:0{i}Z",
                 "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
                for i in range(5)])
            n, skipped = scan.run_export(_cfg(home), ["claude_code"], io.StringIO())
            assert n == 5, (n, skipped)       # all 5 drained despite cap=2
    finally:
        _util._MAX_LINES = orig
    print("ok export_drains_past_cap")


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
    print("ok claude_truncated_in_place_drops_its_carried_state")


def test_file_identity_reset() -> None:
    # scan_file records (dev, ino) and re-reads from offset 0 when the file behind the path is
    # replaced (rotation / inode reuse), so a replacement's opening lines are never skipped.
    from agent_usage_tracker.collectors._util import scan_file
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "f.jsonl")
        Path(p).write_bytes(b"a\nb\n")   # bytes, not text: Windows would translate \n to \r\n
        lines, s1 = scan_file(p, {})
        assert [ln.strip() for ln in lines] == [b"a", b"b"]
        assert "dev" in s1 and "ino" in s1 and s1["off"] == 4
        lines, s2 = scan_file(p, s1)                 # unchanged -> nothing new
        assert lines == [] and s2["off"] == 4
        stale = dict(s1)
        stale["ino"] = s1["ino"] + 1                 # pretend a different file now sits here
        lines, s3 = scan_file(p, stale)
        assert [ln.strip() for ln in lines] == [b"a", b"b"]   # reset to 0, full re-read
        assert s3["ino"] == s1["ino"]                          # identity corrected to the real file

        # Regression: st_dev is a mount-time number, not a property of the file. macOS renumbers
        # it on reboot, which used to invalidate every watermark and re-push all history. A
        # changed dev with the same inode is the SAME file -> keep the offset, read nothing.
        rebooted = dict(s1)
        rebooted["dev"] = s1["dev"] + 4              # what a restart does to every tracked path
        lines, s4 = scan_file(p, rebooted)
        assert lines == [] and s4["off"] == 4        # no re-read
        assert s4["dev"] == s1["dev"]                # dev still refreshed for diagnostics
    print("ok file_identity_reset")


def test_status_line_comes_from_each_collector() -> None:
    from agent_usage_tracker.collectors import REGISTRY
    cfg = _cfg(Path("/tmp"))
    for name, wm, want in (("claude_code", {"files": {"a": {}}}, "1 files tracked"),
                           ("codex", {}, "never scanned"),
                           ("cursor", {}, "full-scan each run (no watermark)"),
                           ("cursor_api", {"since_ms": 5}, "since_ms=5"),
                           ("cursor_cli", {"chats": {"a": 1}}, "1 chats, 0 SDK stores tracked"),
                           ("cursor_cli", {"files": {}}, "0 files tracked"),
                           ("activitywatch", {"end": "x"}, "read until x"),
                           ("activitywatch", {}, "never scanned")):
        assert REGISTRY[name].describe(cfg, wm) == want, name
    print("ok status_line_comes_from_each_collector")


def test_one_shot_commands_warn_at_the_drain_cap() -> None:
    # a collector that never settles used to stop export and backfill silently, unlike scan
    import io

    from agent_usage_tracker import collectors, scan

    def endless(cfg, wm):
        yield [], lambda: wm.update(n=wm.get("n", 0) + 1)

    saved = (collectors.MAX_DRAIN_PASSES, scan.REGISTRY)
    collectors.MAX_DRAIN_PASSES, scan.REGISTRY = 3, {"endless": collectors.Collector(endless)}
    try:
        for run in (lambda c: scan.run_export(c, ["endless"], io.StringIO()),
                    lambda c: scan.run_history_backfill(c, ["endless"], dry_run=True)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                run(_cfg(Path("/tmp")))
            assert "drain-pass cap (3)" in err.getvalue(), err.getvalue()
    finally:
        collectors.MAX_DRAIN_PASSES, scan.REGISTRY = saved
    print("ok one_shot_commands_warn_at_the_drain_cap")


def test_hardening_edges() -> None:
    # project_name: cross-platform + non-str guard (Windows path reduced on POSIX, containers -> None)
    from agent_usage_tracker.collectors._util import project_name
    assert project_name("/home/u/repo") == "repo"
    assert project_name("C:\\Users\\a\\private\\proj") == "proj"     # windows path on posix
    assert project_name("repo/") == "repo" and project_name("plain-name") == "plain-name"
    assert project_name(None) is None and project_name(["x"]) is None and project_name({"a": 1}) is None

    # A git worktree is the SAME repo: collapse it onto the main checkout, or one repo worked on
    # across N branches reports as N projects and no per-project total is real.
    assert project_name("/w/repo/.claude/worktrees/feat-x") == "repo"     # Claude Code's own layout
    assert project_name("/w/repo/.worktrees/feat-x") == "repo"            # hand-rolled
    assert project_name("/w/repo/.claude-worktrees/feat-x") == "repo"     # any .*worktrees container
    assert project_name("C:\\w\\repo\\.worktrees\\feat-x") == "repo"      # same on a windows path
    # A monorepo package inside a worktree stays attributed to the PACKAGE, not the branch.
    assert project_name("/w/mono/.worktrees/R8-1/pkg") == "pkg"
    assert project_name("/w/mono/.claude/worktrees/R8-1/pkg") == "pkg"
    # Must not fire on ordinary paths, incl. dot-dirs that are not worktree containers.
    assert project_name("/home/u/.claude/projects") == "projects"
    assert project_name("/home/u/.config/repo") == "repo"
    assert project_name("/home/u/worktrees/repo") == "repo"     # no dot -> not a container
    # Degenerate: the container itself, with no branch under it, is left exactly as it was.
    assert project_name("/w/repo/.worktrees") == ".worktrees"
    assert project_name("/.worktrees/feat-x") == "feat-x"       # nothing left to attribute to

    # _as_bool: a quoted "false" in TOML must not silently enable a collector
    from agent_usage_tracker.config import _as_bool
    assert _as_bool(True) is True and _as_bool(False) is False
    assert _as_bool("false") is False and _as_bool("0") is False and _as_bool("no") is False
    assert _as_bool("true") is True and _as_bool("yes") is True and _as_bool("on") is True

    # cursor_api pagination: a full page of zero-token (dropped) rows that are still RECENT must
    # NOT halt paging - the next page's real events must still be reached.
    from agent_usage_tracker.collectors import cursor_api
    UID = 7
    zero_recent = [{"owningUser": UID, "timestamp": str(1000 + i), "model": "m",
                    "conversationId": f"z{i}", "tokenUsage": {"inputTokens": 0, "outputTokens": 0}}
                   for i in range(cursor_api._PAGE)]                 # a full page, all zero-token
    real = {"owningUser": UID, "timestamp": "2000", "model": "m", "conversationId": "r",
            "tokenUsage": {"inputTokens": 5, "outputTokens": 5}}
    pages = {1: {"usageEventsDisplay": zero_recent}, 2: {"usageEventsDisplay": [real]}}

    def fake_post(cookie, name, body):
        return {"userId": UID} if name == "get-me" else pages.get(body.get("page"), {"usageEventsDisplay": []})

    cfg = _cfg(Path("/tmp"))
    cfg.cursor_api = True
    oa, op = cursor_api._auth, cursor_api._post
    cursor_api._auth = lambda c: ("ck", 1)
    cursor_api._post = fake_post
    try:
        evs = [e for batch, _ in cursor_api.collect(cfg, {}) for e in batch]
    finally:
        cursor_api._auth, cursor_api._post = oa, op
    assert len(evs) == 1 and evs[0].input_tokens == 5, evs   # reached page 2 past the all-zero page
    print("ok hardening_edges")


def test_non_object_lines() -> None:
    # valid-but-non-object JSON lines (a scalar, an array, a string) must be skipped, not crash
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        good = {"type": "assistant", "uuid": "u1", "timestamp": "2026-07-18T10:00:00Z",
                "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
        path = home / ".claude/projects/p/s.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # 1e400 parses to inf -> int(inf) raises OverflowError; must also be skipped, not wedge
        overflow = ('{"type":"assistant","uuid":"u3","timestamp":"2026-07-18T10:00:00Z",'
                    '"message":{"model":"m","usage":{"input_tokens":1e400,"output_tokens":1,'
                    '"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}')
        path.write_text('42\n[1,2,3]\n"a string"\n' + overflow + "\n" + json.dumps(good) + "\n")
        ev = _drain(claude_code.collect, _cfg(home))
        assert len(ev) == 1 and ev[0].total_tokens == 2
    print("ok non_object_lines")


def test_valid_ts() -> None:
    from agent_usage_tracker.event import valid_ts
    assert valid_ts("2026-07-18T10:00:00Z")
    assert valid_ts("2026-07-18T10:00:00+00:00")
    for bad in (None, "", "   ", 12345, 3.14, "not-a-date", []):
        assert not valid_ts(bad), bad
    print("ok valid_ts")


def test_is_interactive_survives_a_line_that_is_no_record():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "s.jsonl"
        p.write_text('["entrypoint"]\n')
        assert claude_code.is_interactive(p) is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
