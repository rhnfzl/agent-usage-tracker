"""Hermetic tests for session identity (agents.event_session) on synthetic Claude Code fixtures.

Locks the rules:
  - main file -> session_id = sessionId, no parent; subagent file (also under workflows/) ->
    session_id = agentId, parent = sessionId;
  - run_origin from the file kind and entrypoint;
  - turn_id = promptId of the most recent earlier user record (tool_result included); a subagent
    keeps its first user record's promptId;
  - Postgres delivery writes usage + session rows in ONE transaction, and a failure commits nothing;
  - the sessions-only backfill sends zero usage rows and never touches the watermark.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
from pathlib import Path

import pytest

from agent_usage_tracker import push, scan
from agent_usage_tracker.collectors import claude_code
from agent_usage_tracker.config import Config
from agent_usage_tracker.event import ActivityEvent

TS = "2026-07-18T10:00:00Z"


def _cfg(home: Path, pg_dsn: str | None = None, webhook_url: str | None = None) -> Config:
    return Config(machine="test", agents=("claude_code",), webhook_url=webhook_url,
                  webhook_secret="s" if webhook_url else None, pg_dsn=pg_dsn, home=home)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _user(uid: str, pid: str | None, content, **kw) -> dict:
    r = {"type": "user", "uuid": uid, "timestamp": TS, "message": {"role": "user",
                                                                    "content": content}, **kw}
    if pid is not None:
        r["promptId"] = pid
    return r


def _asst(mid: str, **kw) -> dict:
    return {"type": "assistant", "uuid": "a-" + mid, "requestId": "r-" + mid, "timestamp": TS,
            "message": {"id": mid, "model": "claude-x", "usage": {
                "input_tokens": 1, "output_tokens": 2,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}, **kw}


TOOL_RESULT = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]


def _fixture(home: Path) -> None:
    proj = home / ".claude/projects/-p"
    # main interactive session: an assistant before any user record, a typed prompt, a tool_result
    # (same promptId, as measured on real logs), then a second typed prompt with a new promptId
    _write_jsonl(proj / "S1.jsonl", [
        _asst("m0", sessionId="S1", entrypoint="cli"),
        _user("h1", "P1", "do the thing", sessionId="S1", entrypoint="cli"),
        _asst("m1", sessionId="S1", entrypoint="cli"),
        _user("t1", "P1", TOOL_RESULT, sessionId="S1", entrypoint="cli"),
        _asst("m2", sessionId="S1", entrypoint="cli"),
        _user("h2", "P2", "next", sessionId="S1", entrypoint="cli"),
        _asst("m3", sessionId="S1", entrypoint="cli"),
    ])
    # subagent spawned by turn P1; its later user record carries another promptId (a resume)
    _write_jsonl(proj / "S1/subagents/agent-A1.jsonl", [
        _user("sa1", "P1", "sub task", sessionId="S1", agentId="A1", isSidechain=True,
              entrypoint="cli"),
        _asst("sm1", sessionId="S1", agentId="A1", isSidechain=True, entrypoint="cli"),
        _user("sa2", "P9", TOOL_RESULT, sessionId="S1", agentId="A1", isSidechain=True),
        _asst("sm2", sessionId="S1", agentId="A1", isSidechain=True, entrypoint="cli"),
    ])
    # workflow subagent with no agentId/sessionId on its records: path fallbacks
    _write_jsonl(proj / "S1/subagents/workflows/wf1/agent-A2.jsonl", [
        _user("wa1", "P2", "wf task", isSidechain=True),
        _asst("wm1", isSidechain=True),
    ])
    _write_jsonl(proj / "S2.jsonl", [
        _user("x1", "Q1", "go", sessionId="S2", entrypoint="sdk-py"),
        _asst("x2", sessionId="S2", entrypoint="sdk-py"),
    ])
    _write_jsonl(proj / "S3.jsonl", [_asst("y1", sessionId="S3", entrypoint="claude-desktop")])


def _sessions(home: Path) -> dict:
    out = {}
    for events, _commit in claude_code.collect(_cfg(home), {}):
        for e in events:
            if not isinstance(e, ActivityEvent):    # activity rows: tests/collectors/test_activity.py
                out[e.event_id] = e
    return out


def _by_msg(evs: dict, mid: str):
    eid = claude_code.make_event_id(claude_code.NAME, f"{mid}|r-{mid}")
    return evs[eid].session


def _human(evs: dict, uid: str):
    return evs[claude_code.make_event_id(claude_code.NAME, "human", uid)].session


def test_session_rules() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _fixture(home)
        evs = _sessions(home)
        assert all(e.session is not None for e in evs.values()), "every event gets a session"

        s = _by_msg(evs, "m0")
        assert (s.session_id, s.parent_session_id, s.run_origin, s.turn_id) == \
            ("S1", None, "interactive", None), s           # no user record seen yet
        assert _by_msg(evs, "m1").turn_id == "P1"
        assert _by_msg(evs, "m2").turn_id == "P1"          # carried through the tool_result
        assert _by_msg(evs, "m3").turn_id == "P2"
        assert _human(evs, "h1").turn_id == "P1" and _human(evs, "h2").turn_id == "P2"
        assert _human(evs, "h1").run_origin == "interactive"

        for mid in ("sm1", "sm2"):                         # pinned to the spawning turn
            s = _by_msg(evs, mid)
            assert (s.session_id, s.parent_session_id, s.run_origin, s.turn_id) == \
                ("A1", "S1", "subagent", "P1"), s
        s = _by_msg(evs, "wm1")
        assert (s.session_id, s.parent_session_id, s.run_origin, s.turn_id) == \
            ("A2", "S1", "subagent", "P2"), s

        assert _by_msg(evs, "x2").run_origin == "automated"
        assert _by_msg(evs, "x2").turn_id == "Q1"
        assert _by_msg(evs, "y1").run_origin == "unknown"

        e = next(iter(evs.values()))
        assert "session" not in e.to_dict()                 # webhook payload stays usage-only


class _Cur:
    def __init__(self, log, fail_on=None):
        self.log, self.fail_on = log, fail_on

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("insert failed")
        self.log.append((sql, list(rows)))


@contextlib.contextmanager
def _fake_psycopg(log, fail_on=None):
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur(log, fail_on)

        def commit(self):
            log.append("commit")

    connects: list = []

    def _connect(dsn, connect_timeout):
        connects.append(dsn)
        return _Conn()

    fake = types.ModuleType("psycopg")
    fake.connect = _connect
    with pytest.MonkeyPatch.context() as m:
        m.setitem(sys.modules, "psycopg", fake)
        yield connects


def _events(home: Path) -> list:
    evs = list(_sessions(home).values())
    for e in evs:
        e.machine = "test"
    return evs


def test_pg_one_transaction() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _fixture(home)
        batch = _events(home)
        send = push.make_sender(_cfg(home, pg_dsn="postgresql://x"))

        log: list = []
        with _fake_psycopg(log) as connects:
            send(batch)
        assert len(connects) == 1, connects                 # one connection
        assert [x if x == "commit" else x[0].split(" (")[0] for x in log] == [
            "INSERT INTO agents.usage_event", "INSERT INTO agents.event_session", "commit"], log
        sql, rows = log[1]
        assert "ON CONFLICT (event_id) DO NOTHING" in sql
        assert len(rows) == len(batch)
        assert rows[0] == tuple(batch[0].session_row()[c] for c in push.SESSION_COLUMNS)
        assert rows[0][:3] == (batch[0].event_id, "test", "claude_code")

        log = []
        with _fake_psycopg(log, fail_on="event_session"):
            try:
                send(batch)
                raise AssertionError("a failed session insert must raise")
            except RuntimeError as exc:
                assert "insert failed" in str(exc)
        assert "commit" not in log, log                     # nothing committed -> watermark stays


def test_webhook_skips_sessions_once(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _fixture(home)
        batch = _events(home)
        posted: list = []
        monkeypatch.setattr(push, "post_webhook", lambda url, secret, b, h=None: posted.append(b))
        err = io.StringIO()
        send = push.make_sender(_cfg(home, webhook_url="https://hooks.example/h"))
        with contextlib.redirect_stderr(err):
            send(batch)
            send(batch)
        assert len(posted) == 2
        assert err.getvalue().count("\n") == 1 and "session" in err.getvalue(), err.getvalue()


def _with_state(fn):
    with tempfile.TemporaryDirectory() as sd, pytest.MonkeyPatch.context() as mp:
        state = Path(sd) / "state.json"
        state.write_text('{"collectors": {"claude_code": {"files": {"keep": {"off": 7}}}}}')
        mp.setenv("AUT_STATE", str(state))
        return fn(state)


def test_sessions_only_backfill(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _fixture(home)
        cfg = _cfg(home, pg_dsn="postgresql://x")
        n_events = len(_sessions(home))

        def run(state: Path):
            before = (state.read_text(), state.stat().st_mtime_ns)
            log: list = []
            monkeypatch.setattr(scan, "make_sender", lambda c: (_ for _ in ()).throw(AssertionError("no usage sender")))
            err = io.StringIO()
            with _fake_psycopg(log), contextlib.redirect_stderr(err):
                counts = scan.run_history_backfill(cfg, ["claude_code"], dry_run=False)
                dry_log: list = []
            with _fake_psycopg(dry_log), contextlib.redirect_stderr(err):
                dry = scan.run_history_backfill(cfg, ["claude_code"], dry_run=True)
            assert (state.read_text(), state.stat().st_mtime_ns) == before, "state untouched"
            return counts, dry, log, dry_log, err.getvalue()

        counts, dry, log, dry_log, err = _with_state(run)
        inserts = [x for x in log if x != "commit"]
        assert inserts and all("agents.event_session" in sql for sql, _ in inserts), log
        assert not any("usage_event" in sql for sql, _ in inserts)
        assert sum(len(rows) for _, rows in inserts) == n_events
        assert dry_log == [], "dry run sends nothing"
        assert counts == dry, (counts, dry)
        assert dry["rows"] == n_events
        assert dry["by_origin"] == {"interactive": 6, "subagent": 3, "automated": 1,
                                    "unknown": 1}, dry
        assert dry["turn_null"] == 2 and dry["subagent_no_parent"] == 0, dry
        assert "11 session rows sent" in err and "counted" in err, err   # progress on stderr


def test_turn_carries_across_scans() -> None:
    # The file grows between scans: the assistant record in the 2nd scan answers turn P1 from the
    # 1st. A project dir literally named `subagents` is still a main session.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        f = home / ".claude/projects/subagents/S1.jsonl"
        _write_jsonl(f, [_user("h1", "P1", "do the thing", sessionId="S1", entrypoint="cli")])
        wm: dict = {}
        for _events, commit in claude_code.collect(_cfg(home), wm):
            commit()
        with f.open("a") as fh:
            fh.write(json.dumps(_asst("m9", sessionId="S1", entrypoint="cli")) + "\n")
        evs = {}
        for events, commit in claude_code.collect(_cfg(home), wm):
            evs.update({e.event_id: e for e in events})
            commit()
        s = _by_msg(evs, "m9")
        assert (s.turn_id, s.run_origin, s.parent_session_id) == ("P1", "interactive", None), s
