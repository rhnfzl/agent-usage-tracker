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
        ev = _drain(cursor.collect, _cfg(home))
        assert len(ev) == 2, ev
        e1 = next(e for e in ev if e.input_tokens == 100)
        assert e1.total_tokens == 120 and e1.ts is not None      # inherited composer ts
        e2 = next(e for e in ev if e.input_tokens == 5)
        assert e2.ts == "2026-01-02T09:30:07.621Z"               # ISO string kept as-is
        assert all(e.provider == "cursor" for e in ev)
    print("ok cursor")


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
        path.write_text('42\n[1,2,3]\n"a string"\n' + json.dumps(good) + "\n")
        ev = _drain(claude_code.collect, _cfg(home))
        assert len(ev) == 1 and ev[0].total_tokens == 2
    print("ok non_object_lines")


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
    print("ok sender_config")


def main() -> int:
    test_claude_code()
    test_codex()
    test_pi()
    test_cursor()
    test_cursor_db_path()
    test_non_object_lines()
    test_sender_config()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
