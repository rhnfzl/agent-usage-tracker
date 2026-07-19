"""Hermetic collector tests on synthetic fixtures (no real logs, no PII, no framework).

Run: `python tests/test_collectors.py`  (or `pytest`).
Locks the normalization rules that are easy to get wrong:
  - Claude Code captures all four token classes (the "undercount" trap).
  - Codex subtracts cached from input and uses the per-turn delta, keeps reasoning.
  - Pi maps message.usage.
  - Cursor joins bubbleId -> composerData for a timestamp and handles ISO + epoch-ms.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.collectors import claude_code, codex, cursor, cursor_sdk, pi  # noqa: E402


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
    print("ok codex")


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


def test_cursor() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        db = home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("composerData:c1", json.dumps({"createdAt": 1700000000000})))
        # bubble WITHOUT its own timestamp -> must inherit from composerData
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b1", json.dumps(
                        {"bubbleId": "b1", "usageUuid": "u1",
                         "tokenCount": {"inputTokens": 100, "outputTokens": 20}})))
        # bubble WITH an ISO-string createdAt
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b2", json.dumps(
                        {"bubbleId": "b2", "createdAt": "2026-01-02T09:30:07.621Z",
                         "tokenCount": {"inputTokens": 5, "outputTokens": 5}})))
        # zero-token bubble -> ignored
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:c1:b3", json.dumps({"tokenCount": {"inputTokens": 0}})))
        con.commit()
        con.close()
        cfg = _cfg(home)
        cfg.cursor_db_path = str(db)   # platform-independent: point straight at the test db
        ev = _drain(cursor.collect, cfg)
        assert len(ev) == 2, ev
        e1 = next(e for e in ev if e.input_tokens == 100)
        assert e1.total_tokens == 120 and e1.ts is not None      # inherited composer ts
        e2 = next(e for e in ev if e.input_tokens == 5)
        assert e2.ts == "2026-01-02T09:30:07.621Z"               # ISO string kept as-is
        assert all(e.provider == "cursor" for e in ev)
    print("ok cursor")


def test_cursor_sdk() -> None:
    # Synthetic orchestrator manifest (real schema, invented numbers). Exercises: finished-with-usage
    # emitted, no-usage skipped, zero-total skipped, missing total_tokens -> summed fallback, and a
    # glob-matched non-manifest JSON ignored (schema-scoped).
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        mpath = home / "proj/.state/runs/TEST-1/manifest.json"
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps({
            "ticket": "TEST-1", "project": "myrepo", "status": "in_review",
            "runs": [
                {"index": 1, "phase": "readiness-1", "model": "composer-2.5", "status": "completed",
                 "started_at": "2026-07-18T10:00:00+00:00", "finished_at": "2026-07-18T10:00:42+00:00",
                 "run_id": "r1",
                 "usage": {"input_tokens": 100, "output_tokens": 20,
                           "total_tokens": 220, "cache_read_tokens": 100}},
                {"index": 2, "phase": "proposal-1", "model": "composer-2.5", "status": "running",
                 "started_at": "2026-07-18T10:01:00+00:00"},                      # no usage -> skipped
                {"index": 3, "phase": "judge", "model": "gpt-5.6", "status": "completed",
                 "finished_at": "2026-07-18T10:02:00+00:00",
                 "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}},   # zero -> skipped
                {"index": 4, "phase": "revise", "model": "composer-2.5", "status": "completed",
                 "finished_at": "2026-07-18T10:03:00+00:00", "run_id": "r4",
                 "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 5}},  # no total
                {"index": 5, "phase": "bad-model", "model": {"nested": "obj"},   # non-str -> None, no crash
                 "status": "completed", "finished_at": "2026-07-18T10:04:00+00:00", "run_id": "r5",
                 "usage": {"input_tokens": 9, "output_tokens": 1}},
            ],
        }))
        # a stray JSON matched by the same glob but NOT a manifest -> must be ignored, not crash
        stray = home / "proj/.state/runs/other/manifest.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text(json.dumps({"foo": "bar"}))

        cfg = _cfg(home)
        cfg.cursor_sdk_manifests = str(home / "**/.state/runs/*/manifest.json")
        ev = _drain(cursor_sdk.collect, cfg)
        assert len(ev) == 3, ev                          # index 1, 4, 5; 2 (no usage) & 3 (zero) out
        e1 = next(e for e in ev if e.input_tokens == 100)
        assert e1.output_tokens == 20 and e1.cache_read_tokens == 100 and e1.cache_write_tokens == 0
        assert e1.total_tokens == 220                            # given total kept as-is
        assert e1.model == "composer-2.5" and e1.provider == "cursor" and e1.agent == "cursor_sdk"
        assert e1.project == "myrepo" and e1.ts == "2026-07-18T10:00:42+00:00"  # finished_at preferred
        e4 = next(e for e in ev if e.input_tokens == 7)
        assert e4.total_tokens == 15                             # 7 + 3 + 5, summed when total absent
        assert e1.event_id != e4.event_id
        e5 = next(e for e in ev if e.input_tokens == 9)
        assert e5.model is None                          # non-str model coerced to None (no crash)
    # two runs with NO index and NO run_id must still get distinct ids (append-order position)
    with tempfile.TemporaryDirectory() as d2:
        h2 = Path(d2)
        mp2 = h2 / "x/.state/runs/T/manifest.json"
        mp2.parent.mkdir(parents=True, exist_ok=True)
        mp2.write_text(json.dumps({"ticket": "T", "runs": [
            {"phase": "a", "model": "m", "finished_at": "2026-07-18T10:00:00+00:00",
             "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"phase": "b", "model": "m", "finished_at": "2026-07-18T10:00:01+00:00",
             "usage": {"input_tokens": 2, "output_tokens": 2}},
        ]}))
        cfg2 = _cfg(h2)
        cfg2.cursor_sdk_manifests = str(h2 / "**/.state/runs/*/manifest.json")
        ev2 = _drain(cursor_sdk.collect, cfg2)
        assert len(ev2) == 2 and ev2[0].event_id != ev2[1].event_id, ev2
    # a documented "~/..."-style glob must expand the home dir (glob.glob does not do this itself)
    import os as _os
    fake_home = _os.path.join(tempfile.gettempdir(), "aut_cursor_sdk_hometest")
    mp3 = Path(fake_home) / ".state/runs/H/manifest.json"
    mp3.parent.mkdir(parents=True, exist_ok=True)
    mp3.write_text(json.dumps({"ticket": "H", "runs": [
        {"index": 1, "model": "m", "finished_at": "2026-07-18T10:00:00+00:00",
         "usage": {"input_tokens": 4, "output_tokens": 1}}]}))
    old = _os.environ.get("HOME")
    _os.environ["HOME"] = tempfile.gettempdir()
    try:
        cfg3 = _cfg(Path("/tmp"))
        cfg3.cursor_sdk_manifests = "~/aut_cursor_sdk_hometest/.state/runs/*/manifest.json"
        ev3 = _drain(cursor_sdk.collect, cfg3)
        assert len(ev3) == 1 and ev3[0].input_tokens == 4, ev3   # ~ expanded and matched
    finally:
        if old is not None:
            _os.environ["HOME"] = old
        import shutil as _sh
        _sh.rmtree(fake_home, ignore_errors=True)
    # unset glob -> collector no-ops (opt-in)
    assert _drain(cursor_sdk.collect, _cfg(Path("/tmp"))) == []
    print("ok cursor_sdk")


def test_export_drains_past_cap() -> None:
    # aut export is one-shot ("from scratch"); it must return EVERY event even when a single file
    # exceeds the per-scan _MAX_LINES cap (regression guard for the drain loop).
    import io

    from agent_usage_tracker import cli
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
            n, skipped = cli.run_export(_cfg(home), ["claude_code"], io.StringIO())
            assert n == 5, (n, skipped)       # all 5 drained despite cap=2
    finally:
        _util._MAX_LINES = orig
    print("ok export_drains_past_cap")


def test_file_identity_reset() -> None:
    # scan_file records (dev, ino) and re-reads from offset 0 when the file behind the path is
    # replaced (rotation / inode reuse), so a replacement's opening lines are never skipped.
    from agent_usage_tracker.collectors._util import scan_file
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "f.jsonl")
        Path(p).write_text("a\nb\n")
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
    print("ok file_identity_reset")


def test_cursor_api() -> None:
    from agent_usage_tracker.collectors import cursor_api
    UID = 114903562
    # _map_event: maps my event; REJECTS a teammate's event (owningUser != uid); skips zero-token.
    mine = {"owningUser": UID, "timestamp": "1784314315897", "model": "composer-2.5",
            "conversationId": "conv-1",
            "tokenUsage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 500}}
    teammate = {**mine, "owningUser": 999999, "conversationId": "conv-2"}
    zero = {"owningUser": UID, "timestamp": "1784314315999", "model": "m", "conversationId": "c",
            "tokenUsage": {"inputTokens": 0, "outputTokens": 0}}
    ev = cursor_api._map_event(mine, UID)
    assert ev is not None and ev.total_tokens == 620 and ev.cache_write_tokens == 0
    assert ev.agent == "cursor" and ev.provider == "cursor" and ev.model == "composer-2.5"
    assert cursor_api._map_event(teammate, UID) is None      # SAFETY: never a teammate's event
    assert cursor_api._map_event(zero, UID) is None
    # privacy hardening: empty/None/'None' ids must NEVER match, even on both sides
    assert not cursor_api._same_user("", "")
    assert not cursor_api._same_user(None, UID)
    assert not cursor_api._same_user("None", None)
    assert not cursor_api._same_user(UID, "")
    assert not cursor_api._same_user([UID], [UID])           # equal containers must NOT match
    assert not cursor_api._same_user({"id": UID}, {"id": UID})
    assert not cursor_api._same_user(True, True)             # bool is an int subclass -> reject
    assert cursor_api._same_user(UID, UID) and cursor_api._same_user(str(UID), UID)
    assert cursor_api._map_event({**mine, "owningUser": ""}, "") is None   # empty both sides -> drop
    # non-str model coerced to None (no crash downstream)
    assert cursor_api._map_event({**mine, "model": {"x": 1}, "conversationId": "c3"}, UID).model is None

    # full collect() with injected HTTP: pagination + incremental watermark + safety filter
    calls = {"pages": []}
    page1 = {"usageEventsDisplay": [mine, teammate,
             {"owningUser": UID, "timestamp": "1784314310000", "model": "composer-2.5",
              "conversationId": "conv-3", "tokenUsage": {"inputTokens": 5, "outputTokens": 5}}]}

    def fake_post(cookie, name, body):
        if name == "get-me":
            return {"userId": UID}
        calls["pages"].append(body.get("page"))
        return page1 if body.get("page") == 1 else {"usageEventsDisplay": []}

    cfg = _cfg(Path("/tmp"))
    cfg.cursor_api = True
    orig_auth, orig_post = cursor_api._auth, cursor_api._post
    cursor_api._auth = lambda c: ("cookie", 3867693)
    cursor_api._post = fake_post
    try:
        wm: dict = {}
        events = []
        for batch, commit in cursor_api.collect(cfg, wm):
            events.extend(batch)
            commit()
        # 2 of my events emitted (teammate filtered out); watermark = newest ts
        assert len(events) == 2, events
        assert all(e.agent == "cursor" for e in events)
        assert wm["since_ms"] == 1784314315897, wm
        # a second scan with only-older events -> nothing new
        page1["usageEventsDisplay"] = [{"owningUser": UID, "timestamp": "1784314310000",
                                        "model": "m", "conversationId": "conv-3",
                                        "tokenUsage": {"inputTokens": 1, "outputTokens": 1}}]
        again = [e for batch, _ in cursor_api.collect(cfg, wm) for e in batch]
        assert again == [], again                            # older than watermark -> skipped
    finally:
        cursor_api._auth, cursor_api._post = orig_auth, orig_post
    # a same-ms event at the watermark must NOT be permanently skipped (stop is strictly-older)
    wm2 = {"since_ms": 1784314315897}
    page1["usageEventsDisplay"] = [
        {"owningUser": UID, "timestamp": "1784314315897", "model": "m", "conversationId": "same-ms",
         "tokenUsage": {"inputTokens": 3, "outputTokens": 3}},          # == watermark -> re-emitted
        {"owningUser": UID, "timestamp": "1784314310000", "model": "m", "conversationId": "older",
         "tokenUsage": {"inputTokens": 1, "outputTokens": 1}}]          # older -> stops paging
    cursor_api._auth, cursor_api._post = (lambda c: ("ck", 1)), fake_post
    try:
        boundary = [e for batch, _ in cursor_api.collect(cfg, wm2) for e in batch]
    finally:
        cursor_api._auth, cursor_api._post = orig_auth, orig_post
    assert len(boundary) == 1 and boundary[0].total_tokens == 6, boundary   # same-ms emitted, not skipped

    # _auth must NOT crash on a malformed stored token (non-JWT / bad base64) -> returns None
    for bad in ("not-a-jwt", "a.b", "x.@@@notbase64@@@.z", ""):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.vscdb"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
            con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (bad,))
            con.commit()
            con.close()
            c = _cfg(Path(d))
            c.cursor_db_path = str(db)
            assert cursor_api._auth(c) is None, bad          # malformed -> no auth, no crash
    # disabled by default -> no network, no events
    assert list(cursor_api.collect(_cfg(Path("/tmp")), {})) == []
    print("ok cursor_api")


def test_hardening_edges() -> None:
    # basename: cross-platform + non-str guard (Windows path reduced on POSIX, containers -> None)
    from agent_usage_tracker.collectors._util import basename
    assert basename("/home/u/repo") == "repo"
    assert basename("C:\\Users\\a\\private\\proj") == "proj"     # windows path on posix
    assert basename("repo/") == "repo" and basename("plain-name") == "plain-name"
    assert basename(None) is None and basename(["x"]) is None and basename({"a": 1}) is None

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


def test_cursor_db_path() -> None:
    from agent_usage_tracker.collectors.cursor import _cursor_db
    home = Path("/tmp/fakehome")
    override = _cfg(home)
    object.__setattr__(override, "cursor_db_path", "/custom/state.vscdb")
    assert str(_cursor_db(override)) == "/custom/state.vscdb"
    p = _cursor_db(_cfg(home))
    assert p is not None
    assert str(p).endswith("Cursor/User/globalStorage/state.vscdb")   # same subpath every OS
    assert str(home) in str(p) or "AppData" in str(p)
    print("ok cursor_db_path")


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


def test_sender_config() -> None:
    from agent_usage_tracker.push import make_sender
    # nothing configured -> refuse to send
    try:
        make_sender(_cfg(Path("/tmp")))
        raise AssertionError("expected SystemExit for no transport")
    except SystemExit:
        pass
    # webhook url set but secret missing -> fail closed (no unauthenticated POST)
    c = Config(machine="t", agents=(), webhook_url="https://n8n.example/webhook/x",
               webhook_secret=None, pg_dsn=None, home=Path("/tmp"))
    try:
        make_sender(c)
        raise AssertionError("expected SystemExit for missing secret")
    except SystemExit:
        pass
    # Allowlist, not blocklist: plaintext http:// to a non-local host AND any non-http(s) scheme
    # (ftp/file/no-scheme) must all be refused so the secret can't leak or misfire.
    for url in ("http://evil.example/h", "HTTP://evil.example/h", "Http://evil.example/h",
                "ftp://evil.example/h", "file:///etc/passwd", "//evil.example/h", "evil.example/h"):
        cc = Config(machine="t", agents=(), webhook_url=url, webhook_secret="s",
                    pg_dsn=None, home=Path("/tmp"))
        try:
            make_sender(cc)
            raise AssertionError(f"expected SystemExit for {url}")
        except SystemExit:
            pass
    # http://localhost (testing) and any https:// are allowed
    for url in ("http://localhost:5678/h", "HTTPS://ok.example/h"):
        make_sender(Config(machine="t", agents=(), webhook_url=url, webhook_secret="s",
                           pg_dsn=None, home=Path("/tmp")))
    print("ok sender_config")


def test_cf_access_headers() -> None:
    from agent_usage_tracker import push
    from agent_usage_tracker.config import Config
    both = Config(machine="t", agents=(), webhook_url="https://x/h", webhook_secret="s",
                  pg_dsn=None, home=Path("/tmp"),
                  cf_access_client_id="id.abc", cf_access_client_secret="sec.xyz")
    assert both.cf_access_headers == {"CF-Access-Client-Id": "id.abc",
                                      "CF-Access-Client-Secret": "sec.xyz"}
    # only one half set -> no headers (never send a lone token half)
    partial = Config(machine="t", agents=(), webhook_url="https://x/h", webhook_secret="s",
                     pg_dsn=None, home=Path("/tmp"), cf_access_client_id="id.abc")
    assert partial.cf_access_headers == {}
    # _post_webhook merges the extra headers onto the request alongside X-AUT-Secret
    captured = {}

    class _Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=0):
            captured.update(req.headers)
            return _Resp()

    orig = push._OPENER
    push._OPENER = _Opener()
    try:
        push._post_webhook("https://x/h", "sec", [], extra_headers=both.cf_access_headers)
    finally:
        push._OPENER = orig
    keys = {k.lower() for k in captured}
    assert {"cf-access-client-id", "cf-access-client-secret", "x-aut-secret"} <= keys, captured
    print("ok cf_access_headers")


def main() -> int:
    test_claude_code()
    test_claude_code_multiblock_dedup()
    test_codex()
    test_pi()
    test_cursor()
    test_cursor_sdk()
    test_cursor_api()
    test_hardening_edges()
    test_export_drains_past_cap()
    test_file_identity_reset()
    test_cursor_db_path()
    test_non_object_lines()
    test_valid_ts()
    test_sender_config()
    test_cf_access_headers()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
