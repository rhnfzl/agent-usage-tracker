"""`aut history-steers`: daily steer estimates for days whose session logs are gone.

Claude Code: the 2 March to 19 April 2026 gap. Codex: its whole history file, 11 February to
2 October 2026, except days that already have counted Codex steers, so no day counts twice.
The CLI history files still hold what was typed.
Each day and agent becomes one activity_event row of kind `steer_estimate`, with a count and no
text. It is a separate kind, so no view counts it as a steer and it can be deleted on its own:
DELETE FROM agents.activity_event WHERE kind = 'steer_estimate'. The files are only read.

Slash and shell commands are left out (the owner's rule), although a logged steer includes them.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from .collectors.claude_code import _is_injected
from .collectors.codex import is_scheduled_automation
from .config import Config
from .event import ActivityEvent, make_event_id
from .push import send_activity_rows
from .sink import connect

# agent -> (file under home, text key, time key, time units per second, first day, last day)
SOURCES = {"claude_code": (".claude/history.jsonl", "display", "timestamp", 1000,
                           date(2026, 3, 2), date(2026, 4, 19)),
           "codex": (".codex/history.jsonl", "text", "ts", 1,
                     date(2026, 2, 11), date(2026, 10, 2))}


def is_steer(text: object) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    return text.lstrip()[0] not in "/!" and not _is_injected(text) \
        and not is_scheduled_automation(text)


def daily_counts(path: Path, text_key: str, time_key: str, per_second: int, first: date,
                 last: date) -> Counter[date]:
    """Steers per local day in the window. A missing file is no rows; a bad line is skipped."""
    days: Counter[date] = Counter()
    if not path.is_file():
        return days
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
                day = datetime.fromtimestamp(o[time_key] / per_second).date()
            except (ValueError, KeyError, TypeError, OSError):
                continue
            if first <= day <= last and is_steer(o.get(text_key)):
                days[day] += 1
    return days


def counted_days(dsn: str) -> set[tuple[str, date]]:
    """(agent, local day) pairs that already have counted steers."""
    with connect(dsn) as conn:
        rows = conn.execute("SELECT DISTINCT agent, (ts AT TIME ZONE 'Europe/Amsterdam')::date "
                            "FROM agents.activity_event WHERE kind = 'steer'").fetchall()
    return {(a, d) for a, d in rows}


def estimates(cfg: Config, counted: set[tuple[str, date]]) -> list[ActivityEvent]:
    rows = []
    for agent, (rel, text_key, time_key, per_second, first, last) in SOURCES.items():
        counts = daily_counts(cfg.home / rel, text_key, time_key, per_second, first, last)
        for day, n in sorted(counts.items()):
            if (agent, day) in counted:
                continue
            rows.append(ActivityEvent(
                event_id=make_event_id(agent, "steer_estimate", day), agent=agent,
                ts=datetime(day.year, day.month, day.day, 12).astimezone().isoformat(),
                kind="steer_estimate", session_id=None, turn_id=None,
                attrs={"count": n, "source": "history"}, machine=cfg.machine))
    return rows


def run(cfg: Config, dry_run: bool) -> Counter[str]:
    dsn = cfg.require_pg("history-steers")
    rows = estimates(cfg, counted_days(dsn))
    if not dry_run:
        send_activity_rows(dsn, rows)
    return Counter({a: sum(r.attrs["count"] for r in rows if r.agent == a) for a in SOURCES})
