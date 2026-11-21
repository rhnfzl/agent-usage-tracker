"""Capture the documented Claude Code statusline quota fields and read their bounded cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, TypedDict
from uuid import UUID

from .coverage import _read_json
from .observations import ingest, stable_id, timestamp, validate_record
from .state import _locked, atomic_write_text, state_path

WINDOWS = {"five_hour": "5h", "seven_day": "7d"}
MAX_INPUT_CHARS = 256 * 1024
MAX_CACHE_BYTES = 64 * 1024
MAX_WINDOWS = 32
FRESH_FOR = timedelta(minutes=30)
LIMIT = 10_000  # basis points; loss of a decimal place would hide small quota changes
SOURCE = "claude_code"
STREAM = "statusline_quota"
_SCOPE = re.compile(r"[a-f0-9]{64}\Z")


class CachedWindow(TypedDict):
    samples: list[dict[str, Any]]
    conflict: bool


class QuotaWindow(TypedDict):
    source: str
    scope: str
    window: str
    used_percent: float | None
    observed_at: str
    expires_at: str
    reset_at: str | None
    freshness: str
    coverage: str
    pace_percent_per_hour: float | None
    reaches_limit_before_reset: bool | None
    pace_reason: str | None


class QuotaSnapshot(TypedDict):
    schema_version: int
    cache_status: str
    windows: list[QuotaWindow]


def quota_cache_path() -> Path:
    return state_path().parent / "quota-cache.json"


def quota_scope(scope: str, window: str) -> str:
    try:
        private_id = UUID(scope).hex
    except (ValueError, AttributeError):
        raise ValueError("quota scope must be an opaque UUID") from None
    return stable_id([SOURCE, STREAM, private_id, window])


def _clock(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("quota time requires a timezone")
    return now.astimezone(timezone.utc)


def claude_statusline_records(payload: Any, *, scope: str,
                              observed_at: datetime) -> list[dict[str, Any]]:
    """Keep only two documented quota windows. Drop paths, prompts and session identity."""
    now = _clock(observed_at)
    if not isinstance(payload, dict):
        raise ValueError("Claude statusline payload must be a JSON object")
    limits = payload.get("rate_limits")
    if limits is None:
        return []
    if not isinstance(limits, dict):
        raise ValueError("Claude rate_limits must be a JSON object")
    records = []
    for window in WINDOWS:
        quota = limits.get(window)
        if quota is None:
            continue
        if not isinstance(quota, dict):
            raise ValueError("Claude quota window must be a JSON object")
        percent, epoch = quota.get("used_percentage"), quota.get("resets_at")
        if percent is None and epoch is None:
            continue
        if percent is not None and (type(percent) not in (int, float) or not 0 <= percent <= 100):
            raise ValueError("Claude quota percentage must be between 0 and 100")
        reset = None
        if epoch is not None:
            if type(epoch) is not int:
                raise ValueError("Claude quota reset must use Unix seconds")
            try:
                reset = datetime.fromtimestamp(epoch, timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise ValueError("Claude quota reset is outside the supported range") from None
        internal_scope = quota_scope(scope, window)
        stamp = now.isoformat()
        fresh = min(now + FRESH_FOR, reset) if reset else now + FRESH_FOR
        remaining = LIMIT - round(Decimal(str(percent)) * 100) if percent is not None else None
        records.append(validate_record({
            "version": 1, "id": stable_id([internal_scope, stamp]), "kind": "quota",
            "timestamp": stamp, "remaining": remaining, "limit": LIMIT, "unit": "basis_points",
            "period": window, "reset": reset.isoformat() if reset else None,
            "fresh_until": fresh.isoformat(), "scope": internal_scope,
        }))
    return records


def _read_cache(path: Path) -> tuple[str, dict[str, CachedWindow]]:
    status, raw = _read_json(path, MAX_CACHE_BYTES)
    if status != "available":
        return status, {}
    windows = raw.get("windows")
    if (type(raw.get("version")) is not int or raw["version"] != 1
            or not isinstance(windows, dict) or len(windows) > MAX_WINDOWS):
        return "invalid", {}
    clean: dict[str, CachedWindow] = {}
    try:
        for key, entry in windows.items():
            if not _SCOPE.fullmatch(key):
                return "invalid", {}
            samples = entry["samples"]
            conflict = entry["conflict"]
            if not isinstance(samples, list) or not 1 <= len(samples) <= 2 or type(conflict) is not bool:
                return "invalid", {}
            rows = [validate_record(row) for row in samples]
            for row in rows:
                if (row["kind"] != "quota" or row["scope"] != key or row["period"] not in WINDOWS
                        or row["limit"] != LIMIT or row["unit"] != "basis_points"
                        or row["remaining"] is not None and row["remaining"] > LIMIT
                        or row["fresh_until"] is None):
                    return "invalid", {}
            if len(rows) == 2 and (rows[0]["period"] != rows[1]["period"]
                                    or timestamp(rows[0]["timestamp"]) >= timestamp(rows[1]["timestamp"])):
                return "invalid", {}
            clean[key] = {"samples": rows, "conflict": conflict}
    except (ValueError, KeyError, TypeError, RecursionError):
        return "invalid", {}
    return status, clean


def capture_claude_quota(payload: Any, *, scope: str, db: Path, cache: Path | None = None,
                         observed_at: datetime | None = None) -> dict[str, int]:
    """Record only changes. A repeated host payload alone cannot refresh evidence freshness."""
    quota_scope(scope, "five_hour")
    rows = claude_statusline_records(payload, scope=scope, observed_at=_clock(observed_at))
    receipt = {"accepted": 0, "duplicates": 0, "conflicts": 0, "unchanged": 0,
               "out_of_order": 0, "missing_windows": len(WINDOWS) - len(rows)}
    if not rows:
        return receipt
    path = cache or quota_cache_path()
    with _locked(path):
        status, windows = _read_cache(path)
        if status not in ("available", "missing"):
            raise ValueError("quota cache is unavailable")
        if len(windows.keys() | {row["scope"] for row in rows}) > MAX_WINDOWS:
            raise ValueError("quota cache window limit reached")
        original = json.dumps(windows, sort_keys=True)
        try:
            for row in rows:
                key = row["scope"]
                previous = windows.get(key)
                last = previous["samples"][-1] if previous else None
                if last is not None:
                    at, prior_at = timestamp(row["timestamp"]), timestamp(last["timestamp"])
                    if at < prior_at:
                        oldest = previous["samples"][0] if previous else None
                        if oldest and timestamp(oldest["timestamp"]) == at and oldest != row:
                            stored = ingest(db, [row], source=SOURCE, stream=STREAM, role="primary")
                            receipt["conflicts"] += stored["conflicts"]
                            windows[key] = {"samples": [last], "conflict": True}
                        else:
                            receipt["out_of_order"] += 1
                        continue
                    if row == last:
                        receipt["duplicates"] += 1
                        continue
                    if at > prior_at and all(last[k] == row[k] for k in ("remaining", "reset")):
                        receipt["unchanged"] += 1
                        continue
                stored = ingest(db, [row], source=SOURCE, stream=STREAM, role="primary")
                for count in ("accepted", "duplicates", "conflicts"):
                    receipt[count] += stored[count]
                if stored["conflicts"] or (last and row["timestamp"] == last["timestamp"]):
                    windows[key] = {"samples": [last or row], "conflict": True}
                    continue
                prior = previous["samples"][-1:] if previous and not previous["conflict"] else []
                windows[key] = {"samples": [*prior, row], "conflict": False}
        finally:
            if json.dumps(windows, sort_keys=True) != original:
                atomic_write_text(path, json.dumps({"version": 1, "windows": windows}, sort_keys=True))
    return receipt


def _window_status(entry: CachedWindow, now: datetime) -> QuotaWindow:
    current = entry["samples"][-1]
    used = ((LIMIT - current["remaining"]) / 100 if current["remaining"] is not None else None)
    reset = timestamp(current["reset"]) if current["reset"] else None
    observed = timestamp(current["timestamp"])
    freshness = ("unknown" if observed > now else
                 "stale" if timestamp(current["fresh_until"]) <= now else "current")
    result: QuotaWindow = {
        "source": SOURCE, "scope": current["scope"], "window": current["period"],
        "used_percent": None if entry["conflict"] else used, "observed_at": current["timestamp"],
        "expires_at": current["fresh_until"], "reset_at": current["reset"], "freshness": freshness,
        "coverage": ("conflicting" if entry["conflict"] else "observed"
                     if used is not None and reset else "partial"),
        "pace_percent_per_hour": None, "reaches_limit_before_reset": None, "pace_reason": None,
    }
    prior = entry["samples"][0]
    if entry["conflict"]:
        result["pace_reason"] = "conflicting_samples"
    elif reset and reset <= now:
        result["pace_reason"] = "reset_passed"
    elif freshness != "current":
        result["pace_reason"] = "stale_sample" if freshness == "stale" else "clock_mismatch"
    elif used is None or reset is None:
        result["pace_reason"] = "missing_percent_or_reset"
    elif len(entry["samples"]) < 2:
        result["pace_reason"] = "single_sample"
    elif current["reset"] != prior["reset"]:
        result["pace_reason"] = "window_reset" if prior["reset"] else "missing_percent_or_reset"
    elif prior["remaining"] is None:
        result["pace_reason"] = "missing_percent_or_reset"
    elif current["remaining"] > prior["remaining"]:
        result["coverage"] = "conflicting"
        result["pace_reason"] = "counter_decreased"
    elif timestamp(prior["fresh_until"]) <= now:
        result["pace_reason"] = "stale_sample"
    else:
        seconds = (observed - timestamp(prior["timestamp"])).total_seconds()
        consumed = prior["remaining"] - current["remaining"]
        result["pace_percent_per_hour"] = round(consumed * 36 / seconds, 4)
        result["reaches_limit_before_reset"] = (
            consumed * (reset - observed).total_seconds() >= current["remaining"] * seconds
        )
    return result


def quota_snapshot(path: Path | None = None, *, now: datetime | None = None) -> QuotaSnapshot:
    """Read at most 64 KiB of cached evidence. Never open observation storage or native logs."""
    clock = _clock(now)
    status, entries = _read_cache(path or quota_cache_path())
    windows = [_window_status(entry, clock) for entry in entries.values()]
    return {"schema_version": 1, "cache_status": status,
            "windows": sorted(windows, key=lambda row: (row["window"], row["scope"]))}
