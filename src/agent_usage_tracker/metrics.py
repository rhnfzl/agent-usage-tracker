"""Pure derivation of showcase metrics from sink rows + a price map. No I/O here: the caller
supplies rows (report.fetch_rows) and prices (pricing.load_prices), so this module is
deterministic and unit-tested without a database or network.

`input` throughout means FRESH (non-cached) prompt tokens. Every $ figure is NOTIONAL /
API-equivalent (subscriptions are flat-rate).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, NamedTuple

from .pricing import normalize_model_key


class Row(NamedTuple):
    ts: datetime
    agent: str
    machine: str | None
    model: str | None
    kind: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int | None
    total_tokens: int


def iter_sessions(rows: Iterable[Row], idle_gap_s: int):
    """Yield (agent, machine, first_ts) per session. A session is a run of one
    agent+machine's events with <= idle_gap_s between consecutive events
    (ordered by ts). `rows` includes human turns, so a session/active_days span
    a human prompt as activity too — not agent-only work time."""
    buckets: dict[tuple, list[datetime]] = defaultdict(list)
    for r in rows:
        buckets[(r.agent, r.machine)].append(r.ts)
    for (agent, machine), times in buckets.items():
        times.sort()
        session_start: datetime | None = None
        prev: datetime | None = None
        for t in times:
            if prev is None or (t - prev).total_seconds() > idle_gap_s:
                if session_start is not None:
                    yield (agent, machine, session_start)
                session_start = t
            prev = t
        if session_start is not None:
            yield (agent, machine, session_start)


def cost_of(row: Row, prices: dict[str, dict[str, float]]) -> float | None:
    """API-equivalent cost of one row, or None if the model is not priced.
    cache_read/write use their own rate when present, else fall back to the input rate.
    reasoning_tokens is already inside output_tokens, so it is never charged again."""
    p = prices.get(normalize_model_key(row.model or ""))
    if not p:
        return None
    in_rate = p.get("input_cost", 0.0)
    return (
        row.input_tokens * in_rate
        + row.output_tokens * p.get("output_cost", 0.0)
        + row.cache_read_tokens * p.get("cache_read_cost", in_rate)
        + row.cache_write_tokens * p.get("cache_write_cost", in_rate)
    )


def naive_cost_of(row: Row, prices: dict[str, dict[str, float]]) -> float | None:
    """What the row WOULD cost with no cache discount: cache_read repriced at the full
    input rate. sum(naive) / sum(actual) over priced rows is the cache-savings multiple."""
    p = prices.get(normalize_model_key(row.model or ""))
    if not p:
        return None
    in_rate = p.get("input_cost", 0.0)
    return (
        row.input_tokens * in_rate
        + row.output_tokens * p.get("output_cost", 0.0)
        + row.cache_read_tokens * in_rate
        + row.cache_write_tokens * p.get("cache_write_cost", in_rate)
    )


_INSTRUMENT = {
    "agents_normalized": ["claude_code", "codex", "cursor", "cursor_sdk", "cursor_api", "pi"],
    "token_classes": ["input", "output", "cache_read", "cache_write", "reasoning"],
    "passive": True,
    "subscription_safe": True,
    "deterministic_idempotency": True,
    "tracks_human_and_agent_turns": True,
    "note": ("Summing only input_tokens undercounts real usage ~460x; the cache token classes "
             "dominate and are captured explicitly."),
}


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def compute_metrics(
    rows: Iterable[Row], prices: dict[str, dict[str, float]], *, idle_gap_s: int, now: datetime
) -> dict[str, Any]:
    """Derive the full showcase metric set. `rows` is any iterable of Row; `prices` may be {}
    (then $ metrics degrade to None while token-ratio metrics still compute)."""
    rows = list(rows)
    agent_rows = [r for r in rows if r.kind != "human"]
    human_rows = [r for r in rows if r.kind == "human"]

    tot_in = sum(r.input_tokens for r in agent_rows)
    tot_out = sum(r.output_tokens for r in agent_rows)
    tot_cr = sum(r.cache_read_tokens for r in agent_rows)
    tot_reason = sum(r.reasoning_tokens or 0 for r in agent_rows)
    tot_tokens = sum(r.total_tokens for r in agent_rows)

    # Cost + savings over the priced subset only; a model absent from `prices` is counted as
    # unpriced (never guessed) and surfaced in priced_coverage.
    actual = naive = 0.0
    priced_tokens = unpriced_tokens = 0
    priced_models: set[str] = set()
    unpriced_models: set[str] = set()
    for r in agent_rows:
        c = cost_of(r, prices)
        if c is None:
            unpriced_tokens += r.total_tokens
            if r.model:
                unpriced_models.add(r.model)
            continue
        actual += c
        naive += naive_cost_of(r, prices) or 0.0
        priced_tokens += r.total_tokens
        if r.model:
            priced_models.add(r.model)

    # active_days/sessions span all rows, including human turns: a prompt counts as
    # activity, so these are not agent-only work time.
    active_days = {r.ts.date() for r in rows}
    sessions = list(iter_sessions(rows, idle_gap_s))
    sessions_by_month: dict[str, int] = defaultdict(int)
    for _a, _m, first_ts in sessions:
        sessions_by_month[first_ts.strftime("%Y-%m")] += 1

    # Monthly trends (all rows; human turns counted separately, never in the token rollup).
    by_month: dict[str, dict] = {}
    for r in rows:
        mb = by_month.setdefault(r.ts.strftime("%Y-%m"),
                                 {"tokens": 0, "in": 0, "cr": 0, "human": 0, "agent": 0,
                                  "days": set(), "model_tokens": defaultdict(int),
                                  "out": 0, "reason": 0, "actual": 0.0, "naive": 0.0})
        mb["days"].add(r.ts.date())
        if r.kind == "human":
            mb["human"] += 1
            continue
        mb["agent"] += 1
        mb["tokens"] += r.total_tokens
        mb["in"] += r.input_tokens
        mb["cr"] += r.cache_read_tokens
        mb["model_tokens"][r.model or "unknown"] += r.total_tokens
        mb["out"] += r.output_tokens
        mb["reason"] += r.reasoning_tokens or 0
        c = cost_of(r, prices)
        if c is not None:
            mb["actual"] += c
            mb["naive"] += naive_cost_of(r, prices) or 0.0

    trends = []
    for month in sorted(by_month):
        mb = by_month[month]
        denom = mb["tokens"] or 1
        trends.append({
            "month": month,
            "tokens": mb["tokens"],
            "cache_hit_rate": round(_safe_div(mb["cr"], mb["cr"] + mb["in"]), 4),
            "active_days": len(mb["days"]),
            "sessions": sessions_by_month.get(month, 0),
            "human_turns": mb["human"],
            "agent_turns": mb["agent"],
            "model_mix": {mdl: round(tok / denom, 4) for mdl, tok in
                          sorted(mb["model_tokens"].items(), key=lambda kv: -kv[1])},
            "est_cost_usd": round(mb["actual"], 2) if mb["actual"] else None,
            "cache_savings_multiple": round(_safe_div(mb["naive"], mb["actual"]), 2)
                                      if mb["actual"] else None,
            "output_leverage": round(_safe_div(mb["out"], mb["in"]), 4),
            "reasoning_share": round(_safe_div(mb["reason"], mb["tokens"]), 4),
        })

    # Per-agent adoption span.
    spans: dict[str, dict] = {}
    for r in rows:
        s = spans.setdefault(r.agent, {"first": r.ts, "last": r.ts})
        s["first"] = min(s["first"], r.ts)
        s["last"] = max(s["last"], r.ts)
    fluency_agents = {
        a: {"first_seen": v["first"].date().isoformat(),
            "last_seen": v["last"].date().isoformat(),
            "months_active": (v["last"].year - v["first"].year) * 12
                             + (v["last"].month - v["first"].month) + 1}
        for a, v in spans.items()
    }

    human_n, agent_n = len(human_rows), len(agent_rows)
    return {
        "as_of": now.date().isoformat(),
        "window": {
            "first_ts": min((r.ts for r in rows), default=now).isoformat(),
            "last_ts": max((r.ts for r in rows), default=now).isoformat(),
        },
        "volume": {
            "total_tokens": tot_tokens,
            "est_cost_usd": round(actual, 2) if priced_tokens else None,
            "priced": priced_tokens > 0,
        },
        "efficiency": {
            "cache_hit_rate": round(_safe_div(tot_cr, tot_cr + tot_in), 4),
            "cache_savings_multiple": round(_safe_div(naive, actual), 2)
                                      if actual else None,
            "output_leverage": round(_safe_div(tot_out, tot_in), 4),
            "reasoning_share": round(_safe_div(tot_reason, tot_tokens), 4),
            "cost_per_active_day": round(actual / len(active_days), 2)
                                   if (actual and active_days) else None,
        },
        "fluency": {
            "agents": fluency_agents,
            "active_days": len(active_days),
            "sessions": len(sessions),
            "distinct_models": len({r.model for r in agent_rows if r.model}),
            "distinct_agents": len({r.agent for r in rows}),
        },
        "collaboration": {
            "human_turns": human_n,
            "agent_turns": agent_n,
            "agent_turns_per_human_turn": round(_safe_div(agent_n, human_n), 2)
                                          if human_n else None,
            "human_turn_share": round(_safe_div(human_n, human_n + agent_n), 4),
        },
        "trends": {"by_month": trends},
        "priced_coverage": {
            "models_priced": len(priced_models),
            "models_unpriced": sorted(unpriced_models),
            "unpriced_token_share": round(
                _safe_div(unpriced_tokens, priced_tokens + unpriced_tokens), 4),
        },
        "instrument": _INSTRUMENT,
    }
