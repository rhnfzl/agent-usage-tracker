"""Private, content-free external observations. Unknown counts remain None."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime
from typing import Any, Iterable

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 4096
MAX_BATCH_RECORDS = 10000
MAX_STORE_RECORDS = 1000000
COUNTS = (
    "input_tokens",
    "input_tokens_total",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
COMMON = {"version", "id", "kind", "timestamp"}
FIELDS = {
    "usage": set(COUNTS) | {"start", "temporality", "counter"},
    "quota": {"remaining", "limit", "unit", "period", "reset", "fresh_until", "scope"},
    "tool": {"status", "duration_ms"},
}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("timestamp must be an ISO timezone timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if result.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return result


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid identifier")
    return value


def _components_valid(r: dict[str, Any]) -> bool:
    total = r["input_tokens_total"]
    known_input = sum(r[key] for key in ("input_tokens", "cache_read_tokens") if r[key] is not None)
    if r["input_tokens"] is not None and r["cache_write_tokens"] is not None:
        known_input += r["cache_write_tokens"]
    if total is not None and known_input > total:
        return False
    if total is not None and all(
        r[key] is not None for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    ) and known_input != total:
        return False
    return not (
        r["reasoning_tokens"] is not None
        and r["output_tokens"] is not None
        and r["reasoning_tokens"] > r["output_tokens"]
    )


def validate_record(record: Any) -> dict[str, Any]:
    """Validate schema v1. Reject text and unknown fields at this trust boundary."""
    if not isinstance(record, dict):
        raise ValueError("record must be object")
    r = dict(record)
    kind = r.get("kind")
    if (
        not isinstance(kind, str)
        or kind not in FIELDS
        or type(r.get("version")) is not int
        or r["version"] != 1
    ):
        raise ValueError("unknown schema version or kind")
    if set(r) - COMMON - FIELDS[kind] or not COMMON <= set(r):
        raise ValueError("missing or unknown fields")
    identifier(r["id"])
    timestamp(r["timestamp"])
    for key in ("start", "reset", "fresh_until"):
        if r.get(key) is not None:
            timestamp(r[key])
    for key in (*COUNTS, "remaining", "limit", "duration_ms"):
        if key in r and r[key] is not None:
            if type(r[key]) is not int or not 0 <= r[key] <= 2**63 - 1:
                raise ValueError("counts must be nonnegative int64 or null")
    if kind == "usage":
        for key in COUNTS:
            r.setdefault(key, None)
        if not any(r[k] is not None for k in COUNTS):
            raise ValueError("usage needs a known count")
        if r.get("temporality", "delta") not in ("delta", "cumulative"):
            raise ValueError("invalid temporality")
        if ("start" in r or "temporality" in r) and "counter" not in r:
            raise ValueError("counter windows require a series identifier")
        if "counter" in r:
            identifier(r["counter"])
        if not _components_valid(r):
            raise ValueError("input components disagree with total or reasoning subset exceeds total")
        if r.get("start") is not None and timestamp(r["start"]) >= timestamp(r["timestamp"]):
            raise ValueError("invalid usage window")
    if kind == "quota":
        required = {"remaining", "limit", "unit", "period", "reset", "fresh_until", "scope"}
        if not required <= set(r):
            raise ValueError("quota needs explicit values, including null unknowns")
        if r["unit"] not in ("tokens", "requests", "percent", "credits", "unknown"):
            raise ValueError("invalid quota unit")
        if r["period"] not in ("minute", "hour", "day", "week", "month", "session", "unknown"):
            raise ValueError("invalid quota period")
        identifier(r["scope"])
    if kind == "tool" and r.get("status") not in ("ok", "error", "unknown"):
        raise ValueError("invalid tool status")
    if len(json.dumps(r).encode()) > MAX_RECORD_BYTES:
        raise ValueError("record too large")
    return r


def _connect(store: str | Path) -> sqlite3.Connection:
    path = Path(store)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS observations "
        "(source TEXT, stream TEXT, role TEXT, id TEXT, body TEXT, "
        "PRIMARY KEY(source, stream, id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS counters "
        "(source TEXT, stream TEXT, counter TEXT, timestamp TEXT, temporality TEXT, "
        "PRIMARY KEY(source, stream, counter))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS streams "
        "(source TEXT, stream TEXT, role TEXT, representation TEXT, "
        "PRIMARY KEY(source, stream))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS tool_disputes "
        "(source TEXT, stream TEXT, id TEXT, body TEXT, "
        "PRIMARY KEY(source, stream, id))"
    )
    return db


def ingest(
    store: str | Path, records: Iterable[dict[str, Any]], *, source: str, stream: str, role: str
) -> dict[str, Any]:
    identifier(source)
    identifier(stream)
    if role not in ("primary", "crosscheck"):
        raise ValueError("invalid role")
    batch: list[dict[str, Any]] = []
    for raw in records:
        if len(batch) >= MAX_BATCH_RECORDS:
            raise ValueError("batch too large")
        batch.append(validate_record(raw))
    result = dict(accepted=0, duplicates=0, conflicts=0, excluded=0)
    db = _connect(store)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            size = db.execute("SELECT count(*) FROM observations").fetchone()[0]
            for r in batch:
                mode = ("counter" if "counter" in r else "event") if r["kind"] == "usage" else None
                ownership = db.execute(
                    "SELECT role, representation FROM streams WHERE source=? AND stream=?",
                    (source, stream),
                ).fetchone()
                if ownership:
                    if ownership[0] != role:
                        raise ValueError("stream role changed; use a separate crosscheck stream")
                body = json.dumps(r, sort_keys=True, separators=(",", ":"))
                old = db.execute(
                    "SELECT body, role FROM observations WHERE source=? AND stream=? AND id=?",
                    (source, stream, r["id"]),
                ).fetchone()
                if old:
                    disputed = db.execute(
                        "SELECT 1 FROM tool_disputes WHERE source=? AND stream=? AND id=?",
                        (source, stream, r["id"]),
                    ).fetchone()
                    conflict = old != (body, role) or bool(disputed)
                    if conflict and (r["kind"] == "tool" or json.loads(old[0])["kind"] == "tool"):
                        # Retain conflicting evidence without rewriting the original observation.
                        db.execute(
                            "INSERT OR IGNORE INTO tool_disputes VALUES (?,?,?,?)",
                            (source, stream, r["id"], body),
                        )
                    result["conflicts" if conflict else "duplicates"] += 1
                    continue
                if ownership:
                    if mode is not None and ownership[1] is not None and mode != ownership[1]:
                        raise ValueError("incompatible usage representation")
                    mode = mode or ownership[1]
                db.execute(
                    "INSERT OR REPLACE INTO streams VALUES (?,?,?,?)", (source, stream, role, mode)
                )
                if size >= MAX_STORE_RECORDS:
                    raise ValueError("store limit reached")
                # Primary ownership is source-wide: alternate streams must be crosschecks.
                owners = db.execute(
                    "SELECT DISTINCT stream FROM observations WHERE source=? AND role='primary'",
                    (source,),
                ).fetchall()
                if role == "primary" and owners and (stream,) not in owners:
                    raise ValueError("overlapping primary streams; use crosscheck")
                if r["kind"] == "usage" and "counter" in r:
                    identity = (source, stream, r["counter"])
                    last = db.execute(
                        "SELECT timestamp, temporality FROM counters WHERE source=? AND stream=? AND counter=?",
                        identity,
                    ).fetchone()
                    if last and last[1] != r.get("temporality", "delta"):
                        raise ValueError("counter temporality changed")
                    if last and timestamp(r["timestamp"]) <= timestamp(last[0]):
                        raise ValueError("out-of-order counter observation")
                    db.execute(
                        "INSERT OR REPLACE INTO counters VALUES (?,?,?,?,?)",
                        (*identity, r["timestamp"], r.get("temporality", "delta")),
                    )
                db.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?)",
                    (source, stream, role, r["id"], body),
                )
                size += 1
                result["accepted"] += 1
                result["excluded"] += role == "crosscheck"
    finally:
        db.close()
    return result


def disputed_tool_ids(store: str | Path, *, source: str, stream: str) -> set[str]:
    """Return quarantined identities without loading unrelated observations."""
    db = _connect(store)
    try:
        return {row[0] for row in db.execute(
            "SELECT DISTINCT id FROM tool_disputes WHERE source=? AND stream=?", (source, stream)
        )}
    finally:
        db.close()


def records(store: str | Path) -> list[dict[str, Any]]:
    """Return normalized evidence; only additive=True observations can feed totals."""
    db = _connect(store)
    try:
        rows = db.execute("SELECT source, stream, role, body FROM observations").fetchall()
        disputed = set(db.execute("SELECT source, stream, id FROM tool_disputes").fetchall())
    finally:
        db.close()
    result = []
    for source, stream, role, body in rows:
        r = json.loads(body)
        r.update(source=source, stream=stream, role=role)
        if r["kind"] == "tool" and (source, stream, r["id"]) in disputed:
            r["status"] = "unknown"
            r["disputed"] = True
        r["additive"] = (
            role == "primary"
            and r["kind"] == "usage"
            and (r.get("temporality", "delta") == "delta")
        )
        result.append(r)
    # Windows are checked across all stored evidence, including late arrivals.
    windows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in result:
        if r["kind"] == "usage" and "counter" in r:
            windows.setdefault((r["source"], r["stream"], r["counter"]), []).append(r)
    for group in windows.values():
        group.sort(key=lambda r: timestamp(r["timestamp"]))
        previous = None
        for r in group:
            start = r.get("start")
            if r.get("temporality") == "cumulative":
                r["additive"] = False
                r["coverage"] = "unknown_first_point"
                r["delta"] = None
                if start is not None:
                    same_start = previous is not None and previous.get("start") == start
                    baseline: dict[str, Any] = (
                        previous
                        if same_start and previous is not None
                        else dict.fromkeys(COUNTS, 0)
                    )
                    delta = {
                        key: (
                            r[key] - baseline[key]
                            if r[key] is not None and baseline[key] is not None
                            else None
                        )
                        for key in COUNTS
                    }
                    if any(value is not None and value < 0 for value in delta.values()):
                        r["coverage"] = "counter_decreased"
                    elif (
                        previous is not None
                        and not same_start
                        and (timestamp(start) < timestamp(previous["timestamp"]))
                    ):
                        r["coverage"] = "overlapping_reset"
                    elif not _components_valid(delta):
                        r["coverage"] = "invalid_components"
                    else:
                        r["delta"] = delta
                        r["additive"] = r["role"] == "primary"
                        r["coverage"] = "delta" if same_start else "known_start_reset"
                elif previous is not None:
                    r["coverage"] = "unknown_start"
            elif start is None or (
                previous is not None and timestamp(start) < timestamp(previous["timestamp"])
            ):
                r["additive"] = False
                r["coverage"] = "unknown_or_overlapping_window"
                if previous is not None:
                    previous["additive"] = False
            if r.get("coverage") not in ("counter_decreased", "invalid_components"):
                previous = r
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in result:
        if r["kind"] == "quota":
            key = (r["source"], r["stream"], r["scope"])
            if key not in latest or timestamp(r["timestamp"]) > timestamp(latest[key]["timestamp"]):
                latest[key] = r
    for r in result:
        if r["kind"] == "quota":
            r["latest"] = latest[(r["source"], r["stream"], r["scope"])] is r
    return sorted(result, key=lambda r: (r["timestamp"], r["id"]))


def stable_id(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def import_file(
    store: str | Path, path: str | Path, *, format: str, source: str, stream: str, role: str
) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file too large")
    if format == "jsonl":
        parsed = [json.loads(line) for line in data.splitlines() if line.strip()]
        return ingest(store, parsed, source=source, stream=stream, role=role)
    if format == "otlp":
        from .telemetry import normalize_otlp

        normalized, unsupported = normalize_otlp(json.loads(data))
        result = ingest(store, normalized, source=source, stream=stream, role=role)
        result["unsupported"] = unsupported
        return result
    raise ValueError("unsupported format")
