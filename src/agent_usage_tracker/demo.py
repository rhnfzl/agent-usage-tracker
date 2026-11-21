"""Build sample native logs for the offline usage demo."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from .config import Config

SINCE = date(2026, 6, 15)
UNTIL = date(2026, 6, 16)
NOW = datetime.combine(UNTIL, time.min, timezone.utc)


def sample_prices(home: Path) -> Path:
    path = home / "model_prices.json"
    path.write_text(json.dumps({"claude-opus-4-8": {
        "input_cost": 0.00001, "output_cost": 0.00002, "cache_read_cost": 0.000001,
        "cache_write_cost": 0.00001,
    }}), encoding="utf-8")
    stamp = datetime.combine(SINCE, time.min, timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _write(home: Path, name: str, rows: list[dict[str, Any]]) -> None:
    path = home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def fixture(home: Path) -> Config:
    project = str(home / "sample-project")
    claude = {
        "type": "assistant", "uuid": "sample-claude-turn", "timestamp": "2026-06-15T00:00:00Z",
        "cwd": project, "message": {"id": "sample-message", "model": "claude-opus-4-8", "usage": {
            "input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
        }},
    }
    _write(home, ".claude/projects/sample-project/sample-claude.jsonl", [claude])
    _write(home, ".claude/projects/sample-project-copy/sample-claude.jsonl", [claude])
    snapshot = {
        "type": "event_msg", "timestamp": "2026-06-15T12:00:00Z", "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 40,
                "reasoning_output_tokens": 15, "total_tokens": 1040,
            }},
        },
    }
    _write(home, ".codex/sessions/2026/06/15/rollout-sample.jsonl", [
        {"type": "session_meta", "payload": {"id": "sample-codex", "cwd": project}},
        {"type": "turn_context", "payload": {"model": "codex-auto-review"}},
        snapshot,
        {**snapshot, "timestamp": "2026-06-16T00:00:00Z"},
    ])
    return Config(machine="sample", agents=("claude_code", "codex"), webhook_url=None,
                  webhook_secret=None, pg_dsn=None, home=home, ledger_timezone="UTC",
                  repo_key_salt="synthetic-sample")
