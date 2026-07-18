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
        ])
        ev = _drain(codex.collect, _cfg(home))
        assert len(ev) == 1, ev
        e = ev[0]
        assert e.input_tokens == 100, e.input_tokens          # 1000 - 900 cached
        assert e.cache_read_tokens == 900 and e.output_tokens == 40
        assert e.reasoning_tokens == 15 and e.total_tokens == 1040
        assert e.model == "gpt-5.6-sol" and e.provider == "openai"
        assert e.project == "repo"
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
        ])
        ev = _drain(pi.collect, _cfg(home))
        assert len(ev) == 1, ev
        e = ev[0]
        assert e.input_tokens == 200 and e.output_tokens == 80
        assert e.cache_read_tokens == 50 and e.cache_write_tokens == 10
        assert e.total_tokens == 340 and e.est_cost_usd == 0.0021
        assert e.model == "claude-opus-4-8"                    # streamed from model_change
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


def main() -> int:
    test_claude_code()
    test_codex()
    test_pi()
    test_cursor()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
