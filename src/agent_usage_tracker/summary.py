"""Read normalized usage in a ledger window without writing state or contacting a sink."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal, Sequence, TypedDict

from . import diagnostics
from .collectors import REGISTRY
from .config import AutError, Config, ledger_zone
from .event import UsageEvent, parse_ts
from .pricing import default_price_path, read_price_snapshot
from .scan import _one_shot_batches
from .usage_price import Comparison, PricedUsage, PriceTally, PriceTotals, compare_prices, price_usage

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
    pricing: PriceTotals


class GroupTotals(Totals):
    key: str | None


class SourceCoverage(TypedDict):
    source: str
    status: str
    events: int
    invalid_timestamps: int
    skipped_records: dict[str, int]
    first_event_at: str | None
    latest_event_at: str | None


class ModelPrices(PriceTotals):
    model: str | None


class Forecast(TypedDict):
    since: str
    until: str
    calendar_days: int
    active_days: int
    api_cost_per_calendar_day_usd: str | None
    api_cost_per_active_day_usd: str | None
    api_cost_for_30_calendar_days_usd: str | None
    api_cost_for_30_active_days_usd: str | None
    assumption: str
    reason: str | None


class Summary(TypedDict):
    schema_version: int
    window: Window
    group_by: Group
    units: dict[str, str]
    totals: Totals
    groups: list[GroupTotals]
    sources: list[SourceCoverage]
    rate_status: str
    rate_synced_at: str | None
    rate_age_seconds: int | None
    prices_by_model: list[ModelPrices]
    price_comparison: Comparison | None
    price_forecast: Forecast


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
        self.pricing = PriceTally()

    def add(self, row: UsageEvent, cost: PricedUsage) -> None:
        self.events += 1
        self.pricing.add(cost)
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
            "pricing": self.pricing.result(),
        }


def _source_usage(cfg: Config, name: str, window_start: datetime, window_end: datetime,
                  add: Callable[[UsageEvent, datetime], None]) -> SourceCoverage:
    coverage: SourceCoverage = {
        "source": name, "status": "no_usage", "events": 0, "invalid_timestamps": 0,
        "skipped_records": {},
        "first_event_at": None, "latest_event_at": None,
    }
    if name == "cursor_api":
        coverage["status"] = "offline" if cfg.cursor_api else "disabled"
        return coverage
    if ("usage" not in REGISTRY[name].rows or name in ("t3", "cursor_cli")
            or (name == "cursor" and cfg.cursor_human_only)):
        coverage["status"] = "not_usage_source"
        return coverage
    seen: set[str] = set()
    failed: set[str] = set()
    first = latest = None
    with diagnostics.capture() as observed:
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
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=ledger_zone(cfg))
                    stamp = stamp.astimezone(timezone.utc)
                    first = stamp if first is None or stamp < first else first
                    latest = stamp if latest is None or stamp > latest else latest
                    if window_start <= stamp < window_end:
                        coverage["events"] += 1
                        add(row, stamp)
                advance()
        except AutError:
            failed.add(name)
    coverage["skipped_records"] = {key: count for key, count in observed.skipped.items()
                                   if key != "activity_errors"}
    if first is not None and latest is not None:
        coverage["first_event_at"] = first.isoformat()
        coverage["latest_event_at"] = latest.isoformat()
        coverage["status"] = "limited" if name == "cursor" else "observed"
    if failed or coverage["invalid_timestamps"] or coverage["skipped_records"]:
        coverage["status"] = "partial"
    return coverage


def _forecast(since: date, until: date, start: datetime, end: datetime,
              sources: list[SourceCoverage], active_days: set[date],
              past_prices: PriceTally) -> Forecast:
    calendar_days = max(0, (until - since).days)
    result: Forecast = {
        "since": since.isoformat(), "until": until.isoformat(),
        "calendar_days": calendar_days, "active_days": len(active_days),
        "api_cost_per_calendar_day_usd": None, "api_cost_per_active_day_usd": None,
        "api_cost_for_30_calendar_days_usd": None, "api_cost_for_30_active_days_usd": None,
        "assumption": ("Apply the current API rates and the observed daily usage to 30 days. "
                       "Calendar days include inactive days. Active days had recorded usage."),
        "reason": None,
    }
    gaps = []
    for source in sources:
        if source["status"] in ("not_usage_source", "disabled"):
            continue
        first, latest = (parse_ts(source[key]) for key in ("first_event_at", "latest_event_at"))
        if source["status"] != "observed" or first is None or latest is None:
            gaps.append(source["source"])
        elif first > start or latest < end:
            gaps.append(source["source"])
    if calendar_days < 2:
        result["reason"] = "At least two complete ledger days are required."
    elif gaps:
        result["reason"] = "Retained usage does not cover the completed window for " + ", ".join(gaps)
    elif len(active_days) < 2:
        result["reason"] = "At least two active days with observed usage are required."
    elif past_prices.known_cost is None or past_prices.unpriced_events:
        result["reason"] = "Some usage in the completed window has no rate."
    else:
        calendar_days_cost = past_prices.known_cost / Decimal(calendar_days)
        active_days_cost = past_prices.known_cost / Decimal(len(active_days))
        result["api_cost_per_calendar_day_usd"] = format(calendar_days_cost, "f")
        result["api_cost_per_active_day_usd"] = format(active_days_cost, "f")
        result["api_cost_for_30_calendar_days_usd"] = format(30 * calendar_days_cost, "f")
        result["api_cost_for_30_active_days_usd"] = format(30 * active_days_cost, "f")
    return result


def build_summary(cfg: Config, agents: Sequence[str] | None = None, *, group_by: Group = "agent",
                  period: str | None = None, since: date | None = None, until: date | None = None,
                  now: datetime | None = None, prices_path: Path | None = None,
                  compare: tuple[str, str] | None = None) -> Summary:
    """Summarize the same local rows export keeps after timestamp and ID validation.

    Source observation bounds describe readable records only. They cannot prove that a source
    captured everything in the window.
    """
    zone = ledger_zone(cfg)
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(zone).date()
    try:
        rates, synced_at = read_price_snapshot(prices_path or default_price_path())
        rate_status = "available" if rates else "missing"
        if not rates and synced_at is not None:
            rate_status = "empty"
    except AutError:
        rates, synced_at, rate_status = {}, None, "invalid"
    since, until = date_window(today, period, since, until)
    start, end = (datetime.combine(day, time.min, zone).astimezone(timezone.utc)
                  for day in (since, until))
    selected = tuple(dict.fromkeys(cfg.agents if agents is None else agents))
    # Collectors compute activity keys alongside usage. A disposable salt keeps that pass read-only.
    cfg = replace(cfg, agents=tuple(dict.fromkeys((*cfg.agents, *selected))),
                  repo_key_salt="local-summary-unused-activity-key")
    groups: dict[str | None, _Totals] = {}
    totals = _Totals()
    models: dict[str | None, PriceTally] = {}
    active_days: set[date] = set()
    past_prices = PriceTally()
    completed_until = max(since, min(today, until))

    def add(row: UsageEvent, stamp: datetime) -> None:
        cost = price_usage(row, rates)
        totals.add(row, cost)
        key = getattr(row, group_by) or None
        if key not in groups:
            groups[key] = _Totals()
        groups[key].add(row, cost)
        model = row.model or None
        if model not in models:
            models[model] = PriceTally()
        models[model].add(cost)
        day = stamp.astimezone(zone).date()
        if day < completed_until:
            active_days.add(day)
            past_prices.add(cost)

    sources = [_source_usage(cfg, name, start, end, add) for name in selected]
    result = totals.result()
    completed_end = datetime.combine(completed_until, time.min, zone).astimezone(timezone.utc)

    def order(key: str | None) -> tuple[bool, str]:
        return key is None, key or ""

    age = max(0, int((now - synced_at).total_seconds())) if synced_at is not None else None
    return {
        "schema_version": 1,
        "window": {"since": since.isoformat(), "until": until.isoformat(), "timezone": zone.key},
        "group_by": group_by,
        "units": {"tokens": "tokens", "logged_cost_usd": "USD",
                  "api_cost_usd": "USD decimal string", "rate_age_seconds": "seconds"},
        "totals": result,
        "groups": [GroupTotals(key=key, **groups[key].result())
                   for key in sorted(groups, key=order)],
        "sources": sources,
        "rate_status": rate_status,
        "rate_synced_at": synced_at.isoformat() if synced_at is not None else None,
        "rate_age_seconds": age,
        "prices_by_model": [ModelPrices(model=model, **models[model].result())
                            for model in sorted(models, key=order)],
        "price_comparison": (compare_prices(compare, rates, result["tokens"], totals.pricing.denominator)
                             if compare is not None else None),
        "price_forecast": _forecast(since, completed_until, start, completed_end, sources,
                                    active_days, past_prices),
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
    age = report["rate_age_seconds"]
    lines.append(f'Local rates {report["rate_status"]}, file age '
                 f'{str(age) + " seconds" if age is not None else "unknown"}')
    lines.append("API-equivalent pricing by model (USD)")
    for label, prices in [("Total", report["totals"]["pricing"]),
                          *((p["model"] or "(unknown)", p) for p in report["prices_by_model"])]:
        known = prices["known_api_cost_usd"] or "unknown"
        estimated = prices["estimated_api_cost_usd"] or "unknown"
        lines.append(f'{label}: {prices["priced_tokens"]}/{prices["coverage_tokens"]} priced tokens, '
                     f'{prices["unpriced_tokens"]} unpriced, '
                     f'known cost {known}, estimated total {estimated}')
    if comparison := report["price_comparison"]:
        lines.append(comparison["assumption"])
        if comparison["reason"]:
            lines.append(f'Comparison unavailable. {comparison["reason"]}')
        elif comparison["model_cost_usd"]:
            for model, model_cost in comparison["model_cost_usd"].items():
                lines.append(f"{model}: {model_cost} USD")
    forecast = report["price_forecast"]
    if forecast["reason"]:
        lines.append(f'Forecast unavailable. {forecast["reason"]}')
    else:
        lines.append(f'Forecast basis: {forecast["active_days"]} active days in '
                     f'{forecast["calendar_days"]} completed ledger days. {forecast["assumption"]}')
        lines.append(f'30 calendar days: {forecast["api_cost_for_30_calendar_days_usd"]} USD. '
                     f'30 active days: {forecast["api_cost_for_30_active_days_usd"]} USD.')
    lines.append("Source coverage (observed records only)")
    for source in report["sources"]:
        first = source["first_event_at"] or "unknown"
        latest = source["latest_event_at"] or "unknown"
        lines.append(f'{source["source"]}: {source["status"]}, {source["events"]} in window, '
                     f'first {first}, latest {latest}, '
                     f'{source["invalid_timestamps"]} invalid timestamps')
        for reason, count in source["skipped_records"].items():
            lines.append(f"  {reason}: {count}")
    return "\n".join(lines)
