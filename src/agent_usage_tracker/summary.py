"""Read normalized usage in a ledger window without writing state or contacting a sink."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Sequence, TypedDict

from .collectors import REGISTRY
from .config import AutError, Config, ledger_zone
from .event import UsageEvent, parse_ts
from .scan import _one_shot_batches

Group = Literal["agent", "model", "project"]
GROUPS = ("agent", "model", "project")
COUNTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
          "reasoning_tokens", "total_tokens")


class Window(TypedDict):
    since: str
    until: str
    timezone: str


class Totals(TypedDict):
    events: int
    tokens: dict[str, int | None]
    unknown_events: dict[str, int]
    logged_cost_usd: float | None


class GroupTotals(Totals):
    key: str | None


class SourceCoverage(TypedDict):
    source: str
    status: str
    events: int
    invalid_timestamps: int
    first_event_at: str | None
    latest_event_at: str | None


class Summary(TypedDict):
    schema_version: int
    window: Window
    group_by: Group
    units: dict[str, str]
    totals: Totals
    groups: list[GroupTotals]
    sources: list[SourceCoverage]


def date_window(today: date, period: str | None = None, since: date | None = None,
                until: date | None = None) -> tuple[date, date]:
    if since is not None or until is not None:
        if period is not None or since is None or until is None or since >= until:
            raise AutError("summary needs both --since and --until, in order, without --period")
        return since, until
    if period == "week":
        since = today - timedelta(days=today.weekday())
    elif period == "month":
        since = today.replace(day=1)
    else:
        since = today
    return since, today + timedelta(days=1)


class _Totals:
    def __init__(self) -> None:
        self.events = 0
        self.counts: Counter[str] = Counter()
        self.missing: Counter[str] = Counter()
        self.logged_cost = 0.0

    def add(self, row: UsageEvent) -> None:
        self.events += 1
        for name in COUNTS:
            value = getattr(row, name)
            if value is None:
                self.missing[name] += 1
            else:
                self.counts[name] += value
        for name in ("model", "project"):
            if not getattr(row, name):
                self.missing[name] += 1
        if row.est_cost_usd is None:
            self.missing["logged_cost_usd"] += 1
        else:
            self.logged_cost += row.est_cost_usd

    def result(self) -> Totals:
        unknown = (*COUNTS, "model", "project", "logged_cost_usd")
        return {
            "events": self.events,
            "tokens": {name: (self.counts[name] if self.events > self.missing[name] else None)
                       for name in COUNTS},
            "unknown_events": {name: self.missing[name] for name in unknown},
            "logged_cost_usd": (self.logged_cost
                                if self.events > self.missing["logged_cost_usd"] else None),
        }


def _source_usage(cfg: Config, name: str, window_start: datetime, window_end: datetime,
                  by: Group, groups: dict[str | None, _Totals], totals: _Totals) -> SourceCoverage:
    coverage: SourceCoverage = {
        "source": name, "status": "no_usage", "events": 0, "invalid_timestamps": 0,
        "first_event_at": None, "latest_event_at": None,
    }
    if name == "cursor_api":
        coverage["status"] = "offline" if cfg.cursor_api else "disabled"
        return coverage
    if "usage" not in REGISTRY[name].rows:
        coverage["status"] = "not_usage_source"
        return coverage
    seen: set[str] = set()
    failed: set[str] = set()
    first = latest = None
    try:
        for rows, advance in _one_shot_batches(name, cfg, {}, failed, {"usage"}):
            for row in rows:
                if not isinstance(row, UsageEvent):
                    continue
                stamp = parse_ts(row.ts)
                if stamp is None:
                    coverage["invalid_timestamps"] += 1
                    continue
                if row.event_id in seen:
                    continue
                seen.add(row.event_id)
                if row.kind != "agent":
                    continue
                stamp = stamp.replace(tzinfo=stamp.tzinfo or ledger_zone(cfg)).astimezone(timezone.utc)
                first = stamp if first is None or stamp < first else first
                latest = stamp if latest is None or stamp > latest else latest
                if window_start <= stamp < window_end:
                    coverage["events"] += 1
                    totals.add(row)
                    key = getattr(row, by) or None
                    if key not in groups:
                        groups[key] = _Totals()
                    groups[key].add(row)
            advance()
    except AutError:
        failed.add(name)
    if first is not None and latest is not None:
        coverage["first_event_at"] = first.astimezone(timezone.utc).isoformat()
        coverage["latest_event_at"] = latest.astimezone(timezone.utc).isoformat()
        coverage["status"] = "observed"
    if failed or coverage["invalid_timestamps"]:
        coverage["status"] = "partial"
    return coverage


def build_summary(cfg: Config, agents: Sequence[str] | None = None, *, group_by: Group = "agent",
                  period: str | None = None, since: date | None = None, until: date | None = None,
                  now: datetime | None = None) -> Summary:
    """Summarize the same local rows export keeps after timestamp and ID validation.

    Source observation bounds describe readable records only. They cannot prove that a source
    captured everything in the window.
    """
    zone = ledger_zone(cfg)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    since, until = date_window(today, period, since, until)
    start, end = (datetime.combine(day, time.min, zone).astimezone(timezone.utc)
                  for day in (since, until))
    selected = tuple(dict.fromkeys(cfg.agents if agents is None else agents))
    # Collectors compute activity keys alongside usage. A disposable salt keeps that pass read-only.
    cfg = replace(cfg, agents=tuple(dict.fromkeys((*cfg.agents, *selected))),
                  repo_key_salt="local-summary-unused-activity-key")
    groups: dict[str | None, _Totals] = {}
    totals = _Totals()
    sources = [_source_usage(cfg, name, start, end, group_by, groups, totals) for name in selected]
    return {
        "schema_version": 1,
        "window": {"since": since.isoformat(), "until": until.isoformat(), "timezone": zone.key},
        "group_by": group_by,
        "units": {"tokens": "tokens", "logged_cost_usd": "USD"},
        "totals": totals.result(),
        "groups": [GroupTotals(key=key, **groups[key].result())
                   for key in sorted(groups, key=lambda key: (key is None, key or ""))],
        "sources": sources,
    }


def render(report: Summary) -> str:
    window = report["window"]
    lines = [f'{window["since"]} to {window["until"]} (exclusive), {window["timezone"]}',
             f'Usage by {report["group_by"]}']
    for label, totals in [("Total", report["totals"]),
                          *((group["key"] or "(unknown)", group) for group in report["groups"])]:
        tokens = ", ".join(f'{name}: {value if value is not None else "unknown"}'
                           for name, value in totals["tokens"].items())
        lines.append(f'{label} ({totals["events"]} events)  {tokens}')
        cost = totals["logged_cost_usd"]
        lines.append(f'  Logged cost (USD): {cost if cost is not None else "unknown"}')
        unknowns = ", ".join(f"{name}: {count}" for name, count in totals["unknown_events"].items()
                             if count)
        if unknowns:
            lines.append(f"  Events with unknown values: {unknowns}")
    lines.append("Source coverage (observed records only)")
    for source in report["sources"]:
        first = source["first_event_at"] or "unknown"
        latest = source["latest_event_at"] or "unknown"
        lines.append(f'{source["source"]}: {source["status"]}, {source["events"]} in window, '
                     f'first {first}, latest {latest}, '
                     f'{source["invalid_timestamps"]} invalid timestamps')
    return "\n".join(lines)
