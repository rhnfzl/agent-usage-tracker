"""Read cached operational evidence and maintain an opt-in private quota notice inbox."""

from __future__ import annotations

from datetime import datetime, time, timedelta
import json
from pathlib import Path
from typing import Any, Sequence, TypedDict

from .coverage import ALERT_HOURS, FRESH_HOURS, _read_json, _stamp, report_path
from .quota import MAX_WINDOWS, QuotaWindow, _clock, quota_snapshot
from .state import _locked, atomic_write_text, state_path

MAX_COVERAGE_BYTES = 256 * 1024
MAX_NOTICE_BYTES = 64 * 1024
MAX_NOTICES = 64
QUOTA_WARNING = 90


def _freshness(at: datetime | None, now: datetime) -> str:
    if at is None or at > now:
        return "unknown"
    return "stale" if now - at > timedelta(hours=FRESH_HOURS) else "current"


def statusline_snapshot(*, quota_path: Path | None = None, coverage_path: Path | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
    """Read two bounded files. Coverage and no-scan paging remain with the existing route."""
    now = _clock(now)
    status, raw = _read_json(coverage_path or report_path(), MAX_COVERAGE_BYTES)
    generated = _stamp(raw.get("generated_at")) if raw.get("version") == 1 else None
    if status == "available" and generated is None:
        status, raw = "invalid", {}
    collector_rows = raw.get("collectors")
    latest = None
    for collector in collector_rows[:128] if isinstance(collector_rows, list) else []:
        if not isinstance(collector, dict) or collector.get("configured") is not True:
            continue
        observation = collector.get("observation")
        at = _stamp(observation.get("finished_at")) if isinstance(observation, dict) else None
        if at is not None and (latest is None or at > latest):
            latest = at
    issues = raw.get("issues")
    warning_count = (sum(1 for row in issues if isinstance(row, dict)
                         and row.get("severity") in ("warning", "error"))
                     if isinstance(issues, list) else None)
    alerts = raw.get("alerts")
    first_seen = [_stamp(row.get("first_seen")) for row in alerts.values()
                  if isinstance(row, dict) and not row.get("absent_since")]         if isinstance(alerts, dict) else None
    new_alerts = (sum(1 for at in first_seen if at is not None and
                      timedelta(0) <= now - at < timedelta(hours=ALERT_HOURS))
                  if first_seen is not None else None)
    delivery = raw.get("delivery")
    queue = delivery.get("pending") if isinstance(delivery, dict) else None
    available = isinstance(delivery, dict) and delivery.get("status") == "available"
    pending = (sum(queue.values()) if available and isinstance(queue, dict) and
               all(key in ("usage", "session", "activity", "time") and
                   type(count) is int and 0 <= count <= 2**63 - 1
                   for key, count in queue.items()) else None)
    return {
        "schema_version": 1,
        "quota": quota_snapshot(quota_path, now=now),
        "operations": {"cache_status": status, "assessed_at": generated.isoformat() if generated else None,
                       "assessment_freshness": _freshness(generated, now),
                       "latest_scan_at": latest.isoformat() if latest else None,
                       "scan_freshness": _freshness(latest, now), "coverage_warnings": warning_count,
                       "new_coverage_alerts": new_alerts, "pending_delivery": pending,
                       "alert_owner": "existing_scan_route"},
        "attention": {"state": "unknown", "reason": "no_registered_approval_event_source"},
    }


class Notice(TypedDict):
    kind: str
    scope: str
    window: str
    observed_at: str
    emitted_at: str
    reset_at: str
    used_percent: float


def quota_notices_path() -> Path:
    return state_path().parent / "quota-notices.json"


def _read_notice_file(path: Path) -> dict[str, Any]:
    status, raw = _read_json(path, MAX_NOTICE_BYTES)
    if status == "missing":
        return {"version": 1, "episodes": {}, "notices": []}
    if (status != "available" or raw.get("version") != 1
            or not isinstance(raw.get("episodes"), dict) or len(raw["episodes"]) > MAX_WINDOWS
            or not isinstance(raw.get("notices"), list) or len(raw["notices"]) > MAX_NOTICES
            or any(not isinstance(entry, dict) for entry in raw["episodes"].values())):
        raise ValueError("quota notice inbox is unavailable")
    return raw


def _quiet(now: datetime, hours: tuple[time, time] | None) -> bool:
    if hours is None:
        return False
    start, end = hours
    clock = now.time()
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def _condition(window: QuotaWindow, episode: dict[str, Any]) -> str | None:
    percent = window["used_percent"]
    reset = window["reset_at"]
    if (window["freshness"] != "current" or window["coverage"] != "observed"
            or percent is None or reset is None):
        return None
    active = episode.get("active_reset")
    if percent >= QUOTA_WARNING or window["reaches_limit_before_reset"] is True:
        return "quota_warning" if active != reset else None
    if active and (reset != active or window["reaches_limit_before_reset"] is False):
        return "quota_recovered"
    return None


def check_quota_notices(windows: Sequence[QuotaWindow], *, path: Path | None = None,
                        now: datetime, quiet_hours: tuple[time, time] | None = None) -> list[Notice]:
    """Persist emitted local notices before returning them. Missing evidence never recovers."""
    clock = _clock(now)
    if _quiet(now, quiet_hours) or not windows:
        return []
    target = path or quota_notices_path()
    emitted: list[Notice] = []
    with _locked(target):
        inbox = _read_notice_file(target)
        episodes = inbox["episodes"]
        for window in windows:
            scope = window["scope"]
            episode = episodes.get(scope, {})
            muted = _stamp(episode.get("muted_until"))
            last_observed = _stamp(episode.get("last_observed"))
            observed = _stamp(window["observed_at"])
            expires = _stamp(window["expires_at"])
            if (observed is None or observed > clock or expires is None or expires <= clock
                    or muted and muted > clock or last_observed and last_observed >= observed):
                continue
            kind = _condition(window, episode)
            if kind is None:
                continue
            reset, percent = window["reset_at"], window["used_percent"]
            assert reset is not None and percent is not None
            emitted.append({"kind": kind, "scope": scope, "window": window["window"],
                            "observed_at": window["observed_at"], "emitted_at": clock.isoformat(),
                            "reset_at": reset, "used_percent": percent})
            episodes[scope] = {"active_reset": reset if kind == "quota_warning" else None,
                               "last_observed": window["observed_at"]}
        if emitted:
            if len(episodes) > MAX_WINDOWS:
                raise ValueError("quota notice window limit reached")
            inbox["notices"] = (inbox["notices"] + emitted)[-MAX_NOTICES:]
            atomic_write_text(target, json.dumps(inbox, sort_keys=True))
    return emitted


def mute_quota(window: QuotaWindow, *, path: Path | None = None,
               now: datetime | None = None) -> str:
    clock = _clock(now)
    reset = window["reset_at"]
    at = _stamp(reset)
    if reset is None or at is None or at <= clock:
        raise ValueError("quota mute requires an observed future reset")
    target = path or quota_notices_path()
    with _locked(target):
        inbox = _read_notice_file(target)
        episode = inbox["episodes"].setdefault(window["scope"], {})
        episode["muted_until"] = reset
        if len(inbox["episodes"]) > MAX_WINDOWS:
            raise ValueError("quota notice window limit reached")
        atomic_write_text(target, json.dumps(inbox, sort_keys=True))
    return reset


def read_quota_notices(path: Path | None = None) -> list[Notice]:
    """Read the private, bounded inbox of notices emitted on this machine."""
    notices: list[Notice] = _read_notice_file(path or quota_notices_path())["notices"]
    return notices
