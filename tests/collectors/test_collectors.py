"""Hermetic collector tests on synthetic fixtures (no real logs, no PII): the rules every collector
shares. Each collector has its own test file.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

import pytest

from agent_usage_tracker.config import Config
from agent_usage_tracker.collectors import claude_code, codex, cursor, pi


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


def test_export_drains_past_cap(monkeypatch) -> None:
    # aut export is one-shot ("from scratch"); it must return EVERY event even when a single file
    # exceeds the per-scan _MAX_LINES cap (regression guard for the drain loop).
    import io

    from agent_usage_tracker import scan
    from agent_usage_tracker.collectors import _util
    monkeypatch.setattr(_util, "_MAX_LINES", 2)                      # tiny cap: 5 turns -> multiple passes
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_jsonl(home / ".claude/projects/p/s.jsonl", [
            {"type": "assistant", "uuid": f"u{i}", "timestamp": f"2026-07-18T10:00:0{i}Z",
             "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1,
                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
            for i in range(5)])
        n, skipped, _failed = scan.run_export(_cfg(home), ["claude_code"], io.StringIO())
        assert n == 5, (n, skipped)       # all 5 drained despite cap=2


def test_file_identity_reset() -> None:
    # changed_files keeps the inode and re-reads from offset 0 when the file behind the path is
    # replaced (rotation / inode reuse), so a replacement's opening lines are never skipped.
    from agent_usage_tracker.collectors._util import changed_files
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "f.jsonl")
        Path(p).write_bytes(b"a\nb\n")   # bytes, not text: Windows would translate \n to \r\n

        def scan(entry: dict) -> list:
            return list(changed_files([p], {p: entry} if entry else {},
                                      lambda st: {"off": 0, "ino": st.st_ino}, Counter()))
        [(_p, s1, _off, lines, end)] = scan({})
        assert [ln.strip() for ln in lines] == [b"a", b"b"]
        s1["off"] = end
        assert set(s1) == {"ino", "off"} and s1["off"] == 4
        assert scan(s1) == []                                   # unchanged -> nothing new
        stale = dict(s1)
        stale["ino"] = s1["ino"] + 1                 # pretend a different file now sits here
        [(_p, s3, off, lines, _end)] = scan(stale)
        assert off == 0 and [ln.strip() for ln in lines] == [b"a", b"b"]   # reset to 0, full re-read
        assert s3["ino"] == s1["ino"]                          # identity corrected to the real file

        # Regression: st_dev is a mount-time number, not a property of the file. macOS renumbers
        # it on reboot, which used to invalidate every watermark and re-push all history. A
        # changed dev with the same inode is the SAME file -> keep the offset, read nothing.
        assert scan({**s1, "dev": 4}) == []          # what an older version's entry still holds


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


def test_one_shot_commands_warn_at_the_drain_cap(monkeypatch) -> None:
    # a collector that never settles used to stop export and backfill silently, unlike scan
    import io

    from agent_usage_tracker import collectors, scan

    def endless(cfg, wm):
        yield [], lambda: wm.update(n=wm.get("n", 0) + 1)

    monkeypatch.setattr(collectors, "MAX_DRAIN_PASSES", 3)
    monkeypatch.setattr(scan, "REGISTRY", {"endless": collectors.Collector(endless)})
    for run in (lambda c: scan.run_export(c, ["endless"], io.StringIO()),
                lambda c: scan.run_history_backfill(c, ["endless"], dry_run=True)):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run(_cfg(Path("/tmp")))
        assert "drain-pass cap (3)" in err.getvalue(), err.getvalue()


def test_hardening_edges(monkeypatch) -> None:
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
    monkeypatch.setattr(cursor_api, "_auth", lambda c: ("ck", 1))
    monkeypatch.setattr(cursor_api, "_post", fake_post)
    evs = [e for batch, _ in cursor_api.collect(cfg, {}) for e in batch]
    assert len(evs) == 1 and evs[0].input_tokens == 5, evs   # reached page 2 past the all-zero page


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


def test_valid_ts() -> None:
    from agent_usage_tracker.event import valid_ts
    assert valid_ts("2026-07-18T10:00:00Z")
    assert valid_ts("2026-07-18T10:00:00+00:00")
    for bad in (None, "", "   ", 12345, 3.14, "not-a-date", []):
        assert not valid_ts(bad), bad


def test_changed_files_yields_only_new_bytes_in_a_private_copy(tmp_path) -> None:
    from agent_usage_tracker.collectors._util import changed_files, read_new_lines
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_bytes(b"one\ntwo\n")
    b.write_bytes(b"idle\n")
    st_a, st_b = a.stat(), b.stat()
    files = {str(a): {"off": 4, "ino": st_a.st_ino, "state": {"n": 1}},
             str(b): {"off": st_b.st_size, "ino": st_b.st_ino}}
    fresh_calls = []

    def fresh(st):
        fresh_calls.append(st.st_ino)
        return {"off": 0, "ino": st.st_ino, "new": True}
    got = list(changed_files([str(a), str(b), str(tmp_path / "gone.jsonl")], files, fresh,
                             Counter()))
    assert [(p, off, lines, end) for p, _cur, off, lines, end in got] == [(str(a), 4, [b"two\n"], 8)]
    assert fresh_calls == []                                   # `gone` was never statted to a row
    got[0][1]["state"]["n"] = 2
    assert files[str(a)]["state"] == {"n": 1}                  # only the caller's callback publishes
    assert read_new_lines(str(a), 8) == ([], 8)


def test_changed_files_starts_a_new_or_replaced_file_from_its_fresh_state(tmp_path) -> None:
    from agent_usage_tracker.collectors._util import changed_files
    f = tmp_path / "f.jsonl"
    f.write_bytes(b"one\n")
    stale = {str(f): {"off": 1, "ino": f.stat().st_ino + 1, "old": True}}
    for files in ({}, stale):
        [(_p, cur, off, lines, end)] = changed_files(
            [str(f)], files, lambda st: {"off": 0, "ino": st.st_ino, "new": True}, Counter())
        assert cur == {"off": 0, "ino": f.stat().st_ino, "new": True}
        assert (off, lines, end) == (0, [b"one\n"], 4)


def test_changed_files_restarts_a_tracked_file_truncated_to_nothing(tmp_path) -> None:
    # Its old offset must not stand: a file regrown past it would resume mid-file.
    from agent_usage_tracker.collectors._util import changed_files
    f = tmp_path / "f.jsonl"
    f.write_bytes(b"")
    ino = f.stat().st_ino
    fresh = lambda st: {"off": 0, "ino": st.st_ino}     # noqa: E731
    [(_p, cur, off, lines, end)] = changed_files([str(f)], {str(f): {"off": 9, "ino": ino, "x": 1}},
                                                 fresh, Counter())
    assert (cur, off, lines, end) == ({"off": 0, "ino": ino}, 0, [], 0)
    assert list(changed_files([str(f)], {str(f): cur}, fresh, Counter())) == []   # settled
    assert list(changed_files([str(f)], {}, fresh, Counter())) == []   # first sight: nothing yet


def test_changed_files_revisits_only_what_the_predicate_asks_for(tmp_path) -> None:
    from agent_usage_tracker.collectors._util import changed_files
    f = tmp_path / "f.jsonl"
    f.write_bytes(b"one\n")
    st = f.stat()
    entry = {"off": 4, "ino": st.st_ino, "open": True}

    def scan(revisit=None):
        return list(changed_files([str(f)], {str(f): entry}, dict, Counter(), revisit))
    assert scan() == [] and scan(lambda e: False) == []
    assert scan(lambda e: e["open"]) == [(str(f), entry, 4, [], 4)]


def _unreadable(path):
    """Pretend the OS refuses `path`: chmod 0 does nothing for root or on Windows."""
    real = open

    def refusing(file, *a, **kw):
        if str(file) == str(path):
            raise PermissionError(13, "denied")
        return real(file, *a, **kw)
    return refusing


def test_an_unreadable_log_degrades_the_collector_and_a_vanished_one_stays_silent(tmp_path, monkeypatch):
    import builtins

    from agent_usage_tracker.collectors._util import changed_files, raise_if_unreadable
    from agent_usage_tracker.event import CollectorUnavailable
    f = tmp_path / "f.jsonl"
    f.write_bytes(b"one\n")
    gone: Counter = Counter()
    assert list(changed_files([str(tmp_path / "gone.jsonl")], {}, dict, gone)) == [] and not gone
    raise_if_unreadable("x", gone)
    monkeypatch.setattr(builtins, "open", _unreadable(f))
    skipped: Counter = Counter()
    tracked = {str(f): {"off": 0, "ino": f.stat().st_ino}}
    assert list(changed_files([str(f)], tracked, lambda st: {"off": 0}, skipped)) == []
    assert list(changed_files([str(f)], {}, lambda st: {"off": 0}, skipped)) == []
    assert tracked[str(f)] == {"off": 0, "ino": f.stat().st_ino}   # keeps its entry
    assert skipped["unreadable"] == 2
    with pytest.raises(CollectorUnavailable, match="2 file"):
        raise_if_unreadable("x", skipped)


def test_a_bad_model_value_is_skipped_and_never_wedges_the_tally(tmp_path) -> None:
    from agent_usage_tracker.scan import _CollectorPass, _blank_agent_tally
    log = tmp_path / ".claude" / "projects" / "-p" / "s.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"type": "assistant", "uuid": "a", "timestamp": "2026-07-18T10:00:01Z",
                               "cwd": "/x/p", "sessionId": "s", "message": {
                                   "id": "m", "model": ["x"], "usage": {"input_tokens": 1}}}) + "\n")
    cfg = Config(machine="t", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=None,
                 home=tmp_path)
    run = _CollectorPass(_blank_agent_tally(), None)
    run.drain_all(claude_code.collect, cfg, {}, "claude_code")
    assert run.agg["by_model"] == {"unknown": {"events": 1, "total": 1}}
    cur = {"model": "keep", "provider": "p"}
    pi._note_model_change({"modelId": ["x"], "provider": {"a": 1}}, cur)
    assert cur == {"model": "keep", "provider": "p"}
    cur = {"model": "keep", "provider": "p", "cwd": None}
    codex._note_context("turn_context", {"model": {"a": 1}}, False, cur, None)
    assert cur["model"] == "keep"


def test_warn_skipped_names_the_failure_type_of_each_activity_skip(capsys) -> None:
    from agent_usage_tracker.collectors._util import warn_skipped
    warn_skipped("x", Counter({"activity:KeyError": 2, "activity:TypeError": 1}))
    err = capsys.readouterr().err
    assert "3 activity record(s)" in err and "KeyError x2, TypeError x1" in err
