"""`aut time add`: one manual active-time interval, with its print_* report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from .config import AutError, Config
from .event import TimeInterval, make_event_id
from .push import insert_pg

_MAX_MANUAL = timedelta(hours=16)


def _parse_local_datetime(value: str) -> datetime:
    """A datetime typed by the user; no zone means this machine's local time."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise AutError(f"time add: not a date and time: {value!r} (use YYYY-MM-DD HH:MM)") \
            from None
    return dt if dt.tzinfo else dt.astimezone()


class ManualSpan(NamedTuple):
    """The interval as typed: --start and --end, or --minutes ending at --at (default now)."""
    start: str | None = None
    end: str | None = None
    minutes: int | None = None
    at: str | None = None


def build_manual_interval(cfg: Config, span: ManualSpan, *, not_relevant: bool = False,
                          now: datetime | None = None) -> TimeInterval:
    """One manual agents.time_interval row, or AutError with the reason it was refused."""
    now = now or datetime.now(timezone.utc)
    if span.minutes is not None:
        if span.start or span.end:
            raise AutError("time add: use --start/--end or --minutes, not both")
        end_dt = _parse_local_datetime(span.at) if span.at else now
        start_dt = end_dt - timedelta(minutes=span.minutes)
    elif span.start and span.end and not span.at:
        start_dt, end_dt = _parse_local_datetime(span.start), _parse_local_datetime(span.end)
    else:
        raise AutError("time add: give --start and --end, or --minutes [--at]")
    if end_dt <= start_dt:
        raise AutError("time add: the end must be after the start")
    if end_dt - start_dt > _MAX_MANUAL:
        raise AutError("time add: an interval can span at most 16 hours")
    if end_dt > now:                    # hours not yet worked are not active time
        raise AutError("time add: the end is in the future")
    s_iso = start_dt.astimezone(timezone.utc).isoformat()
    e_iso = end_dt.astimezone(timezone.utc).isoformat()
    return TimeInterval(event_id=make_event_id("manual", cfg.machine, s_iso, e_iso), ts=s_iso,
                        end_ts=e_iso, source="manual", app=None, title=None, category=None,
                        work_relevant=not not_relevant, attrs={}, machine=cfg.machine)


def run_time_add(cfg: Config, span: ManualSpan, *, not_relevant: bool = False) -> TimeInterval:
    row = build_manual_interval(cfg, span, not_relevant=not_relevant)
    insert_pg(cfg.require_pg("time add"), [row])
    return row


def print_time_add(row: TimeInterval) -> None:
    s, e = (datetime.fromisoformat(x).astimezone() for x in (row.ts, row.end_ts))
    mins = (e - s).total_seconds() / 60
    print(f"added manual interval {s:%Y-%m-%d %H:%M} -> {e:%Y-%m-%d %H:%M} ({mins:.0f} min, "
          f"{'relevant' if row.work_relevant else 'not relevant'})")
