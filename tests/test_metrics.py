"""Hermetic metrics tests: fixed rows -> asserted derived metrics. No DB, no network."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.metrics import Row, iter_sessions  # noqa: E402


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _row(
    ts: str,
    agent: str = "claude_code",
    model: str | None = "claude-opus-4",
    kind: str = "agent",
    inp: int = 0,
    out: int = 0,
    cr: int = 0,
    cw: int = 0,
    reason: int = 0,
    total: int | None = None,
) -> Row:
    total = (inp + out + cr + cw + reason) if total is None else total
    return Row(_dt(ts), agent, "laptop", model, kind, inp, out, cr, cw, reason, total)


def test_sessions_split_on_idle_gap() -> None:
    rows = [
        _row("2026-01-01T10:00:00"),
        _row("2026-01-01T10:20:00"),  # <30m -> same session
        _row("2026-01-01T12:00:00"),  # >30m -> new session
    ]
    assert len(list(iter_sessions(rows, idle_gap_s=1800))) == 2


def test_sessions_do_not_merge_across_agents() -> None:
    rows = [
        _row("2026-01-01T10:00:00", agent="claude_code"),
        _row("2026-01-01T10:05:00", agent="codex"),
    ]
    assert len(list(iter_sessions(rows, idle_gap_s=1800))) == 2


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all passed")
