"""Hermetic metrics tests: fixed rows -> asserted derived metrics. No DB, no network."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.metrics import Row, iter_sessions, cost_of, naive_cost_of  # noqa: E402


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


PRICES = {
    "claude-opus-4": {
        "input_cost": 1.5e-05,
        "output_cost": 7.5e-05,
        "cache_read_cost": 1.5e-06,
        "cache_write_cost": 1.875e-05,
    }
}


def test_cost_and_naive() -> None:
    r = _row("2026-01-01T10:00:00", inp=100, out=10, cr=1000, cw=50)
    # actual: 100*1.5e-5 + 10*7.5e-5 + 1000*1.5e-6 + 50*1.875e-5
    assert round(cost_of(r, PRICES), 10) == round(
        0.0015 + 0.00075 + 0.0015 + 0.0009375, 10
    )
    # naive reprices cache_read at the input rate: 1000*1.5e-5 (not 1000*1.5e-6)
    assert round(naive_cost_of(r, PRICES), 10) == round(
        0.0015 + 0.00075 + 0.015 + 0.0009375, 10
    )


def test_cost_none_for_unpriced_model() -> None:
    assert cost_of(_row("2026-01-01T10:00:00", model="unknown-x"), PRICES) is None
    assert naive_cost_of(_row("2026-01-01T10:00:00", model=None), PRICES) is None


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all passed")
