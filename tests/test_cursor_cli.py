"""Hermetic tests for the cursor_cli collector (Cursor CLI chat stores and SDK run stores), on
synthetic sqlite fixtures built in a temp dir.

Run: `python tests/test_cursor_cli.py`  (or `pytest`).
Locks the rules:
  - no token usage ever (tokens come from cursor_api); human rows only for CLI prompts carrying
    `providerOptions.cursor.requestId`, never for a subagent chat or a summary message;
  - message order from every root blob version (a summarized chat's older messages live only in
    an older root), prompt ts from the embedded `<timestamp>`, else the chat's createdAt;
  - session rows: session_id = agentId, turn_id = requestId, run_origin interactive;
  - activity: steer, commit (Shell + paired result, shared parser), rejection (SwitchMode), turn
    (tool aggregates, isError, hook blocks); a turn is emitted once it is closed (a later prompt,
    or the chat idle), and never twice;
  - SDK runs: one turn row per finished/cancelled/error run, status enum, no error text;
  - attrs never carry free text; backfill works and never touches the watermark.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_activity import _git_repo, _key, _plain_value  # noqa: E402

from agent_usage_tracker import cli  # noqa: E402
from agent_usage_tracker.collectors import REGISTRY, cursor_cli  # noqa: E402
from agent_usage_tracker.config import ALL_AGENTS, Config  # noqa: E402
from agent_usage_tracker.event import (ActivityEvent, CollectorUnavailable, UsageEvent,  # noqa: E402
                                       make_event_id)

SALT = "test-salt"
NAME = "cursor_cli"
TYPED = "please tidy the parser module"
LEAKS = (TYPED, "tidy", "clientname", "acmecorp", "ABC-1", "feature", "src/pkg", "usage limit",
         "explain why", "summary of", "hook said no")
A = "aaaaaaaa-0000-4000-8000-000000000001"       # interactive chat
B = "bbbbbbbb-0000-4000-8000-000000000002"       # its subagent
C = "cccccccc-0000-4000-8000-000000000003"       # a chat still in progress
CREATED_MS = 1784538000000                        # 2026-07-20T09:00:00Z
OLD = time.time() - 7200                          # mtime of an idle store


def _cfg(home: Path) -> Config:
    return Config(machine="test", agents=(NAME,), webhook_url=None, webhook_secret=None,
                  pg_dsn=None, home=home, repo_key_salt=SALT)


def _msg(role: str, content, **po) -> dict:
    m = {"role": role, "content": content}
    if po:
        m["providerOptions"] = {"cursor": po}
    return m


def _prompt(rid: str, text: str, stamp: str | None = None, images: int = 0) -> dict:
    body = (f"<timestamp>{stamp}</timestamp>\n" if stamp else "") + \
        f"<user_query>\n{text}\n</user_query>"
    blocks = [{"type": "text", "text": body}] + [{"type": "image", "image": "x"}] * images
    return _msg("user", blocks, requestId=rid)


def _calls(*calls: tuple) -> dict:
    return _msg("assistant", [{"type": "reasoning", "text": "think"}] + [
        {"type": "tool-call", "toolCallId": cid, "toolName": name, "args": args}
        for cid, name, args in calls])


def _result(cid: str, name: str, result: str, err: bool = False, **hl) -> dict:
    return _msg("tool", [{"type": "tool-result", "toolCallId": cid, "toolName": name,
                          "result": result}],
                highLevelToolCallResult={"isError": err, **hl})


def _store(home: Path, agent: str, roots: list[list[dict]], extra: list[dict] = (),
           mtime: float = OLD, **meta) -> Path:
    """A chat store: each root lists its messages as protobuf field 1 (32-byte blob ids), with an
    unrelated field after them; the LAST root is meta's latestRootBlobId."""
    p = home / ".cursor/chats/0123abcd" / agent / "store.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    def put(data: bytes) -> str:
        k = hashlib.sha256(data).hexdigest()
        con.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (k, data))
        return k

    root = None
    for msgs in roots:
        ids = [put(json.dumps(m).encode()) for m in msgs]
        root = put(b"".join(b"\x0a\x20" + bytes.fromhex(i) for i in ids) + b"\x2a\x03xyz")
    for m in extra:                                   # messages in no root
        put(json.dumps(m).encode())
    put(b"\x12\x05other")                            # a binary blob that is not a root
    m = {"agentId": agent, "latestRootBlobId": root, "name": "a chat about clientname",
         "createdAt": CREATED_MS, "mode": "default", **meta}
    con.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(m).encode().hex(),))
    con.commit()
    con.close()
    os.utime(p, (mtime, mtime))
    return p


def _sys() -> dict:
    return _msg("system", "you are an agent", systemPromptFingerprint={"agentType": "cli"})


def _chat_a(repo: Path) -> list[list[dict]]:
    cwd = str(repo)
    head = [
        _sys(),
        _msg("user", "<user_info>injected context</user_info>"),        # no requestId
        _prompt("r1", TYPED, "Monday, Jul 20, 2026, 9:05 AM (UTC+2)", images=1),
        _calls(("c1", "Shell", {"command": "git add -A && git commit -m 'tidy clientname'",
                                "working_directory": cwd, "description": "commit"}),
               ("c2", "Read", {"path": "src/pkg/clientname.py"}),
               ("c3", "mcp_acmecorp-internal_lookup", {"q": "clientname"})),
        _result("c1", "Shell", "Exit code: 0\n\nCommand output:\n\n"
                "[feature/ABC-1-clientname 1a2b3c4] tidy clientname\n 1 file changed"),
        _result("c2", "Read", "no such file src/pkg/clientname.py", err=True),
        _result("c3", "mcp_acmecorp-internal_lookup", "ok"),
        _prompt("r2", "explain why"),                                    # no timestamp
        _calls(("c4", "SwitchMode", {"target_mode_id": "plan", "explanation": "explain why"}),
               ("c5", "Shell", {"command": "rm -rf build"}),
               ("c6", "Subagent", {"prompt": "go", "description": "x"})),
        _result("c4", "SwitchMode", "Mode switch was rejected by the user"),
        _result("c5", "Shell", "Rejected: Hook blocked with message: hook said no", err=True),
        _result("c6", "Subagent", "child finished", output={"success": {"agentId": B}}),
    ]
    # the chat was summarized: the latest root holds only a summary and what came after
    tail = [
        _sys(),
        _msg("user", "summary of the chat so far", isSummary=True),
        _prompt("r3", "and again", "Monday, Jul 20, 2026, 11:40 PM (UTC-3)"),
        _calls(("c7", "Shell", {"command": "git commit --amend --no-edit"})),
        _result("c7", "Shell", "Exit code: 0\n\nCommand output:\n\nok 9f8e7d6"),
    ]
    return [head[:5], head, tail]       # an early root (a prefix), the full one, the latest


def _fixture(home: Path, repo: Path) -> None:
    _store(home, A, _chat_a(repo),
           extra=[_prompt("r0", "a prompt whose root is gone", "Sunday, Jul 19, 2026, 8:00 PM (UTC)")])
    _store(home, B, [[_sys(), _prompt("rb", "you are a subagent: look at clientname"),
                      _calls(("b1", "Read", {"path": "x"})), _result("b1", "Read", "ok")]],
           subagentInfo={"parentAgentId": A, "rootParentAgentId": A, "toolCallId": "c6",
                         "typeName": "explore"})
    _sdk(home)


def _sdk(home: Path) -> None:
    p = home / ".cursor/projects/some-slug/sdk-agent-store/feedc0de/index.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, agent_id TEXT, turn_number INT, "
                "status TEXT, error_code TEXT, result TEXT, created_at TEXT, started_at TEXT, "
                "finished_at TEXT, cancelled_at TEXT)")
    con.execute("INSERT INTO agents VALUES ('agent-sdk1', 'clientname bot')")
    con.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("run-1", "agent-sdk1", 1, "FINISHED", None, "tidy clientname", "2026-07-20T10:00:00.000Z",
         "2026-07-20T10:00:01.000Z", "2026-07-20T10:00:31.500Z", None),
        ("run-2", "agent-sdk1", 2, "CANCELLED", "operation was aborted", None,
         "2026-07-20T10:01:00.000Z", "2026-07-20T10:01:01.000Z", None, "2026-07-20T10:01:09.000Z"),
        ("run-3", "agent-sdk1", 3, "ERROR", "usage limit for acmecorp", None,
         "2026-07-20T10:02:00.000Z", "2026-07-20T10:02:01.000Z", None, None),
        ("run-4", "agent-sdk1", 4, "QUEUED", None, None, "2026-07-20T10:03:00.000Z", None, None,
         None),
        ("run-5", "agent-sdk1", 5, "RUNNING", None, None, "2026-07-20T10:04:00.000Z",
         "2026-07-20T10:04:01.000Z", None, None),
    ])
    con.commit()
    con.close()


def _run(fn):
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        repo = _git_repo(home, "https://Example.com/Owner/Repo.git")
        _fixture(home, repo)
        return fn(home, repo)


def _collect(home: Path, wm: dict | None = None) -> list:
    out = []
    for events, commit in cursor_cli.collect(_cfg(home), {} if wm is None else wm):
        out.extend(events)
        commit()
    return out


def _acts(evs: list, kind: str | None = None, sid: str | None = None) -> list:
    return [e for e in evs if isinstance(e, ActivityEvent) and (kind is None or e.kind == kind)
            and (sid is None or e.session_id == sid)]


def _eid(kind: str, seed: str) -> str:
    return make_event_id(NAME, kind, seed)


def test_registered() -> None:
    assert NAME in REGISTRY and set(REGISTRY) == set(ALL_AGENTS), "config and registry drifted"
    print("ok registered")


def test_cli_human_and_sessions() -> None:
    def check(home, repo):
        evs = _collect(home)
        usage = [e for e in evs if isinstance(e, UsageEvent)]
        assert all(e.kind == "human" and not e.any_tokens and e.model is None for e in usage)
        by = {e.session.turn_id: e for e in usage}
        # r0 sits in no root but is still a prompt; rb is the subagent's, not a person's
        assert sorted(by) == ["r0", "r1", "r2", "r3"], sorted(by)
        assert {(e.session.session_id, e.session.parent_session_id, e.session.run_origin)
                for e in usage} == {(A, None, "interactive")}
        assert by["r1"].ts == "2026-07-20T09:05:00+02:00", by["r1"].ts
        assert by["r3"].ts == "2026-07-20T23:40:00-03:00", by["r3"].ts
        assert by["r0"].ts == "2026-07-19T20:00:00+00:00", by["r0"].ts
        assert by["r2"].ts == "2026-07-20T09:00:00+00:00", by["r2"].ts          # chat createdAt
        assert by["r1"].event_id == make_event_id(NAME, "human", A, "r1")
        steer = {e.turn_id: e for e in _acts(evs, "steer")}
        assert sorted(steer) == ["r0", "r1", "r2", "r3"]
        assert steer["r1"].attrs == {"via": "typed", "words": 5, "chars": len(TYPED),
                                     "images": 1}, steer["r1"].attrs
        assert steer["r1"].ts == by["r1"].ts and steer["r1"].session_id == A
    _run(check)
    print("ok cli_human_and_sessions")


def test_cli_activity() -> None:
    def check(home, repo):
        evs = _collect(home)
        turns = {(e.session_id, e.turn_id): e for e in _acts(evs, "turn") if e.session_id in (A, B)}
        assert sorted(turns) == [(A, "r1"), (A, "r2"), (A, "r3"), (B, "rb")], sorted(turns)
        t1 = turns[(A, "r1")]
        assert t1.event_id == _eid("turn", f"{A}|r1") and t1.ts == "2026-07-20T09:05:00+02:00"
        assert t1.attrs == {"tool_calls": 3, "tool_errors": 1, "hook_blocks": 0,
                            "by_tool": {"Shell": 1, "Read": 1, "mcp": 1},
                            "max_identical_run": 1, "retry_flag": False,
                            "runaway_flag": False, "run_origin": "interactive"}, t1.attrs
        t2 = turns[(A, "r2")].attrs
        assert (t2["tool_calls"], t2["tool_errors"], t2["hook_blocks"], t2["by_tool"]) == \
            (3, 1, 1, {"SwitchMode": 1, "Shell": 1, "Subagent": 1}), t2
        tb = turns[(B, "rb")].attrs
        assert (tb["by_tool"], tb["run_origin"], tb["parent_session_id"]) == \
            ({"Read": 1}, "subagent", A), tb
        assert "parent_session_id" not in t1.attrs
        commits = {e.attrs["sha"]: e for e in _acts(evs, "commit")}
        assert sorted(commits) == ["1a2b3c4", "9f8e7d6"], commits
        c1, c7 = commits["1a2b3c4"], commits["9f8e7d6"]
        assert c1.attrs == {"sha": "1a2b3c4", "amend": False,
                            "repo_key": _key("example.com/owner/repo")}, c1.attrs
        assert (c1.turn_id, c1.event_id) == ("r1", _eid("commit", f"{A}|c1|1a2b3c4"))
        assert c7.attrs == {"sha": "9f8e7d6", "amend": True, "repo_key": None}, c7.attrs
        assert c7.turn_id == "r3"
        [rej] = _acts(evs, "rejection")
        assert (rej.session_id, rej.turn_id, rej.attrs) == (A, "r2", {})
        assert rej.event_id == _eid("rejection", f"{A}|c4")
        assert not _acts(evs, "steer", B)
    _run(check)
    print("ok cli_activity")


def test_open_turn_waits_and_counts_once() -> None:
    def check(home, repo):
        _store(home, C, [[_sys(), _prompt("q1", "first"), _calls(("d1", "Read", {"p": 1})),
                          _result("d1", "Read", "ok"), _prompt("q2", "second"),
                          _calls(("d2", "Read", {"p": 2}))]], mtime=time.time())
        wm: dict = {}
        first = [e for e in _collect(home, wm) if (e.session.session_id if isinstance(
            e, UsageEvent) else e.session_id) == C]
        # q1 is closed by q2; q2 is still open on a fresh store, so nothing of it yet
        assert sorted((type(e).__name__, getattr(e, "kind", "")) for e in first) == [
            ("ActivityEvent", "link"), ("ActivityEvent", "steer"), ("ActivityEvent", "turn"),
            ("UsageEvent", "human")], first
        assert {e.turn_id if isinstance(e, ActivityEvent) else e.session.turn_id
                for e in first} == {"q1"}
        snap = json.dumps(wm, sort_keys=True)
        assert _collect(home, wm) == [], "an unchanged scan sends nothing again"
        assert json.dumps(wm, sort_keys=True) == snap
        p = home / ".cursor/chats/0123abcd" / C / "store.db"
        os.utime(p, (OLD, OLD))                      # the chat went idle: q2 is done
        # an uncommitted scan (a failed push) leaves the watermark alone
        assert sum(len(evs) for evs, _c in cursor_cli.collect(_cfg(home), wm)) == 4
        assert json.dumps(wm, sort_keys=True) == snap
        later = _collect(home, wm)
        assert sorted((e.kind, e.turn_id if isinstance(e, ActivityEvent) else e.session.turn_id)
                      for e in later) == [("human", "q2"), ("link", "q2"), ("steer", "q2"),
                                          ("turn", "q2")], later
        [t2] = _acts(later, "turn")
        assert t2.attrs["tool_calls"] == 1
        assert _collect(home, wm) == []
    _run(check)
    print("ok open_turn_waits_and_counts_once")


def _blob(m: dict) -> str:
    return hashlib.sha256(json.dumps(m).encode()).hexdigest()


def _rows(evs: list, sid: str) -> list:
    return [e for e in evs if (e.session.session_id if isinstance(e, UsageEvent)
                               else e.session_id) == sid]


def _tid(e) -> str | None:
    return e.session.turn_id if isinstance(e, UsageEvent) else e.turn_id


def test_rewritten_call_counts_once() -> None:
    def check(home, repo):
        # a later root re-lists call e1 in a rewritten assistant blob (new blob id, same call)
        p, res = _prompt("q1", "first"), _result("e1", "Read", "ok")
        one = _calls(("e1", "Read", {"p": 1}))
        two = _msg("assistant", [{"type": "reasoning", "text": "think again"},
                                 {"type": "tool-call", "toolCallId": "e1", "toolName": "Read",
                                  "args": {"p": 1}}])
        _store(home, C, [[_sys(), p, one, res], [_sys(), p, two, res,
                                                 _calls(("e2", "Grep", {"p": 2}))]])
        [t] = _acts(_rows(_collect(home), C), "turn")
        assert (t.attrs["tool_calls"], t.attrs["by_tool"]) == (2, {"Read": 1, "Grep": 1}), t.attrs
    _run(check)
    print("ok rewritten_call_counts_once")


def test_follow_up_prompt_in_running_turn() -> None:
    def check(home, repo):
        # a second prompt sent while q1 ran reuses q1's requestId; a re-serialized copy of the
        # first prompt (same text, new blob) is not a new prompt
        p1 = _prompt("q1", "first", "Monday, Jul 20, 2026, 9:05 AM (UTC)")
        p2 = _prompt("q1", "and also this", "Monday, Jul 20, 2026, 9:10 AM (UTC)")
        dup = dict(p1, providerOptions={"cursor": {"requestId": "q1", "mode": "agent"}})
        _store(home, C, [[_sys(), p1, _calls(("e1", "Read", {})), _result("e1", "Read", "ok"),
                          p2, _calls(("e2", "Read", {})), _result("e2", "Read", "ok"), dup]])
        evs = _rows(_collect(home), C)
        human = sorted(((e.event_id, e.ts, e.session.turn_id) for e in evs
                        if isinstance(e, UsageEvent)), key=lambda x: x[1])
        assert human == [
            (make_event_id(NAME, "human", C, "q1"), "2026-07-20T09:05:00+00:00", "q1"),
            (make_event_id(NAME, "human", C, "q1", _blob(p2)), "2026-07-20T09:10:00+00:00",
             "q1")], human
        steer = sorted(_acts(evs, "steer"), key=lambda e: e.ts)
        assert [(e.event_id, e.turn_id, e.attrs["words"]) for e in steer] == [
            (_eid("steer", f"{C}|q1"), "q1", 1),
            (_eid("steer", f"{C}|q1|{_blob(p2)}"), "q1", 3)], steer
        [t] = _acts(evs, "turn")
        assert (t.turn_id, t.attrs["tool_calls"], t.ts) == ("q1", 2, "2026-07-20T09:05:00+00:00")
    _run(check)
    print("ok follow_up_prompt_in_running_turn")


def test_unlisted_prompt_waits_while_active() -> None:
    def check(home, repo):
        # the prompt blob landed before the root that lists it: an active chat waits for it
        p1, p2 = _prompt("q1", "first"), _prompt("q2", "second")
        head = [_sys(), p1, _calls(("e1", "Read", {})), _result("e1", "Read", "ok")]
        _store(home, C, [head], extra=[p2], mtime=time.time())
        wm: dict = {}
        assert not [e for e in _rows(_collect(home, wm), C) if _tid(e) == "q2"], "q2 must wait"
        _store(home, C, [head, head + [p2, _calls(("e2", "Grep", {}),
                                                  ("e3", "Grep", {}))]])
        later = _rows(_collect(home, wm), C)
        q2 = sorted(e.kind for e in later if _tid(e) == "q2")
        assert q2 == ["human", "link", "steer", "turn"], q2
        [t] = [e for e in _acts(later, "turn") if e.turn_id == "q2"]
        assert t.attrs["tool_calls"] == 2, t.attrs
    _run(check)
    print("ok unlisted_prompt_waits_while_active")


def test_sdk_runs() -> None:
    def check(home, repo):
        evs = _collect(home)
        turns = {e.turn_id: e for e in _acts(evs, sid="agent-sdk1")}
        assert sorted(turns) == ["run-1", "run-2", "run-3"], sorted(turns)
        assert all(e.kind == "turn" for e in turns.values())
        auto = {"run_origin": "automated"}
        assert turns["run-1"].attrs == {"status": "finished", "duration_ms": 30500, **auto}
        assert turns["run-2"].attrs == {"status": "cancelled", "duration_ms": None, **auto}
        assert turns["run-3"].attrs == {"status": "error", "duration_ms": None, **auto}
        assert turns["run-1"].ts == "2026-07-20T10:00:01.000Z"
        assert turns["run-1"].event_id == _eid("turn", "agent-sdk1|run-1")
        wm: dict = {}
        _collect(home, wm)
        assert not [e for e in _collect(home, wm) if e.session_id == "agent-sdk1"]
    _run(check)
    print("ok sdk_runs")


def test_attrs_carry_no_text() -> None:
    def check(home, repo):
        evs = _acts(_collect(home))
        assert {e.kind for e in evs} == {"steer", "commit", "rejection", "turn", "link"}
        for e in evs:
            for k, v in e.attrs.items():
                assert _plain_value(k, v), (e.kind, k, v)
            blob = json.dumps(e.attrs)
            for leak in (*LEAKS, str(repo)):
                assert leak not in blob, (e.kind, leak)
        for bad in ("mcp_acmecorp-internal_lookup", "wc -l x", "a/b"):
            assert cursor_cli._tool(bad) in ("mcp", "other"), bad
    _run(check)
    print("ok attrs_carry_no_text (cursor_cli)")


def test_activity_errors_never_block_human_rows() -> None:
    def check(home, repo):
        real = cursor_cli._Activity._record

        def boom(*a, **k):
            raise RuntimeError("boom")
        cursor_cli._Activity._record = boom
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                evs = _collect(home)
        finally:
            cursor_cli._Activity._record = real
        assert len([e for e in evs if isinstance(e, UsageEvent)]) == 4
        assert "activity record(s) skipped" in err.getvalue()
    _run(check)
    print("ok activity_errors_never_block_human_rows")


def test_unreadable_store_is_reported_after_the_rest() -> None:
    def check(home, repo):
        bad = home / ".cursor/chats/hhh/ffffffff-0000-4000-8000-00000000000f/store.db"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"this is not a database" * 100)
        wm: dict = {}
        out = []
        try:
            for events, advance_watermark in cursor_cli.collect(_cfg(home), wm):
                out.extend(events)
                advance_watermark()
            raise AssertionError("a corrupt store must not look like a quiet week")
        except CollectorUnavailable as exc:
            assert "1 store(s)" in str(exc), exc
        assert [e for e in out if isinstance(e, UsageEvent)], \
            "the readable chats still came first"
        assert str(bad) not in wm["chats"], "and the bad one is retried next scan"
    _run(check)
    print("ok unreadable_store_is_reported_after_the_rest")


def test_backfill_cursor_cli() -> None:
    def check(home, repo):
        prev = os.environ.get("AUT_STATE")
        with tempfile.TemporaryDirectory() as sd:
            state = Path(sd) / "state.json"
            state.write_text('{"collectors": {"cursor_cli": {"chats": {}}}}')
            os.environ["AUT_STATE"] = str(state)
            before = (state.read_text(), state.stat().st_mtime_ns)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    c = cli.run_session_backfill(_cfg(home), [NAME], dry_run=True,
                                                 sessions=True, activity=True)
            finally:
                if prev is None:
                    os.environ.pop("AUT_STATE", None)
                else:
                    os.environ["AUT_STATE"] = prev
            assert (state.read_text(), state.stat().st_mtime_ns) == before, "state untouched"
        assert c["by_origin"] == {"interactive": 4}, c
        assert c["turn_null"] == 0, c
        assert c["by_kind"] == {"steer": 4, "turn": 7, "commit": 2, "rejection": 1, "link": 4}, c   # SDK runs: no link
    _run(check)
    print("ok backfill_cursor_cli")


def main() -> int:
    test_registered()
    test_cli_human_and_sessions()
    test_cli_activity()
    test_open_turn_waits_and_counts_once()
    test_rewritten_call_counts_once()
    test_follow_up_prompt_in_running_turn()
    test_unlisted_prompt_waits_while_active()
    test_sdk_runs()
    test_attrs_carry_no_text()
    test_activity_errors_never_block_human_rows()
    test_backfill_cursor_cli()
    test_unreadable_store_is_reported_after_the_rest()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
