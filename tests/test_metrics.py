"""Hermetic metrics tests: fixed rows -> asserted derived metrics. No DB, no network."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.metrics import (  # noqa: E402
    Row, iter_sessions, cost_of, naive_cost_of, compute_metrics,
)


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

    sessions = list(iter_sessions(rows, idle_gap_s=1800))
    assert sessions[0] == ("claude_code", "laptop", _dt("2026-01-01T10:00:00"))
    assert sessions[1] == ("claude_code", "laptop", _dt("2026-01-01T12:00:00"))


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
    r2 = _row("2026-01-01T10:00:00", reason=4)
    # reasoning bills at the output rate
    assert round(cost_of(r2, PRICES), 10) == round(4 * 7.5e-05, 10)


def test_cost_none_for_unpriced_model() -> None:
    assert cost_of(_row("2026-01-01T10:00:00", model="unknown-x"), PRICES) is None
    assert naive_cost_of(_row("2026-01-01T10:00:00", model=None), PRICES) is None


def test_compute_metrics_end_to_end() -> None:
    rows = [
        _row("2026-01-05T10:00:00", inp=100, out=50, cr=900, cw=0, kind="agent"),
        _row("2026-01-05T10:05:00", agent="claude_code", kind="human", model=None),
        _row("2026-02-10T09:00:00", agent="codex", model="unknown-x",
             inp=200, out=100, reason=30, total=330),
    ]
    m = compute_metrics(rows, PRICES, idle_gap_s=1800, now=_dt("2026-03-01T00:00:00"))

    assert m["as_of"] == "2026-03-01"
    assert m["collaboration"]["human_turns"] == 1
    assert m["collaboration"]["agent_turns"] == 2
    assert m["collaboration"]["agent_turns_per_human_turn"] == 2.0
    # cache_hit_rate over agent rows = cr / (cr + input) = 900 / (900 + 300)
    assert m["efficiency"]["cache_hit_rate"] == round(900 / 1200, 4)
    assert m["fluency"]["active_days"] == 2              # 2026-01-05 and 2026-02-10
    assert m["fluency"]["distinct_agents"] == 2
    assert m["priced_coverage"]["models_priced"] == 1    # only the claude row is priced
    assert "unknown-x" in m["priced_coverage"]["models_unpriced"]
    assert m["volume"]["priced"] is True
    assert len(m["trends"]["by_month"]) == 2             # 2026-01 and 2026-02
    assert m["instrument"]["tracks_human_and_agent_turns"] is True

    jan, feb = m["trends"]["by_month"][0], m["trends"]["by_month"][1]
    assert jan["output_leverage"] == 0.5                 # out 50 / in 100
    assert jan["est_cost_usd"] == 0.01                   # cost_of(claude row) = 0.0066 -> 0.01
    assert jan["cache_savings_multiple"] == 2.84         # naive 0.01875 / actual 0.0066
    assert jan["reasoning_share"] == 0.0                 # no reasoning tokens in Jan
    assert feb["reasoning_share"] == round(30 / 330, 4)  # 0.0909
    assert feb["est_cost_usd"] is None                   # unknown-x is unpriced
    assert feb["cache_savings_multiple"] is None
    assert feb["output_leverage"] == 0.5                 # out 100 / in 200


def test_compute_metrics_unpriced_only_degrades_cleanly() -> None:
    rows = [_row("2026-01-05T10:00:00", model="unknown-x", inp=10, out=5, total=15)]
    m = compute_metrics(rows, PRICES, idle_gap_s=1800, now=_dt("2026-02-01T00:00:00"))
    assert m["volume"]["priced"] is False
    assert m["volume"]["est_cost_usd"] is None
    assert m["efficiency"]["cache_savings_multiple"] is None
    assert m["efficiency"]["output_leverage"] == round(5 / 10, 4)  # token ratios still work


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all passed")
