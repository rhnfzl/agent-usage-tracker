"""Import command evidence without retaining desktop content."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import AutError
from ..observations import ingest
from .store import Store, digest

MAX_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 100_000
DELIVERY = {
    "unknown": "unknown",
    "not_delivered": "safe",
    "delivery_uncertain": "unsafe",
    "delivered_unverified": "unsafe",
    "delivered_verified": "unsafe",
}
CODES = set(
    "PERM_DENIED ELEMENT_NOT_FOUND APP_NOT_FOUND ACTION_FAILED ACTION_NOT_SUPPORTED "
    "STALE_REF AMBIGUOUS_TARGET WINDOW_NOT_FOUND PLATFORM_NOT_SUPPORTED TIMEOUT "
    "INVALID_ARGS NOTIFICATION_NOT_FOUND SNAPSHOT_NOT_FOUND SANDBOX_NOT_FOUND "
    "POLICY_DENIED APP_UNRESPONSIVE INTERNAL".split()
)


def _integer(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def _identity(row: dict[str, Any], seq: str = "seq") -> tuple[int, int, int]:
    values = tuple(row.get(key) for key in ("writer_pid", "writer_proc_start_ms", seq))
    if not all(_integer(value) for value in values):
        raise AutError("desktop: invalid writer or sequence identity")
    return values  # type: ignore[return-value]


def _read(
    path: Path | None, coverage: Counter[str], budget: list[int], session_id: str | None = None
) -> list[dict[str, Any]]:
    if path is None:
        return []
    if not path.exists():
        coverage["missing_sources"] += 1
        return []
    if path.is_symlink():
        raise AutError("desktop: symbolic sources are unsupported")
    if path.is_dir():
        files: list[Path] = []
        for entry in path.iterdir():
            if len(files) >= 4096:
                raise AutError("desktop: directory entry cap exceeded")
            files.append(entry)
        if any(not entry.is_file() or entry.is_symlink() for entry in files):
            raise AutError("desktop: expected a directory of raw segment files")
    else:
        files = [path]
    rows: list[dict[str, Any]] = []
    for file in sorted(files):
        with file.open("rb") as stream:
            raw = stream.read(MAX_BYTES - budget[0] + 1)
        budget[0] += len(raw)
        if budget[0] > MAX_BYTES:
            raise AutError("desktop: source byte cap exceeded")
        try:
            whole = json.loads(raw)
        except (ValueError, UnicodeError):
            whole = None
        body = whole.get("data", whole) if isinstance(whole, dict) else None
        if isinstance(body, dict) and "events" in body:
            if session_id is not None and body.get("session_id", session_id) != session_id:
                raise AutError("desktop: explicit session mismatch")
            if body.get("truncated") is not False:
                coverage["truncated_exports"] += 1
            coverage["source_warnings"] += bool(body.get("warnings"))
            if body.get("total_events") != body.get("returned_events"):
                coverage["filtered_exports"] += 1
            batch = body["events"]
            if not isinstance(batch, list):
                raise AutError("desktop: invalid trace-show events")
        else:
            batch = []
            lines = raw.splitlines()
            for index, line in enumerate(lines):
                try:
                    batch.append(json.loads(line))
                except (ValueError, UnicodeError):
                    if index == len(lines) - 1 and not raw.endswith(b"\n"):
                        coverage["partial_lines"] += 1
                    else:
                        coverage["malformed_records"] += 1
        for row in batch:
            budget[1] += 1
            if budget[1] > MAX_EVENTS:
                raise AutError("desktop: source event cap exceeded")
            if not isinstance(row, dict):
                raise AutError("desktop: expected object records")
            rows.append(row)
    return rows


def _disposition(response: dict[str, Any]) -> dict[str, Any]:
    payload = response.get("data" if response.get("ok") is True else "error", {})
    if not isinstance(payload, dict):
        payload = {}
    disposition = payload.get("disposition", {})
    if not isinstance(disposition, dict):
        disposition = {}
    delivery = disposition.get("delivery", "unknown")
    retry = disposition.get("retry", "unknown")
    invalid = (
        not isinstance(delivery, str)
        or delivery not in DELIVERY
        or not isinstance(retry, str)
        or retry not in {"safe", "unsafe", "unknown"}
    )
    if invalid:
        delivery, retry = "unknown", "unknown"
    code = payload.get("code")
    return dict(
        delivery=delivery,
        retry=retry,
        disposition_conflict=invalid or DELIVERY[delivery] != retry,
        response_code=code if isinstance(code, str) and code in CODES else None,
    )


def import_desktop(
    store: Store, trace_path: Path, response_path: Path | None, mapping: dict[str, Any]
) -> dict[str, Any]:
    """Import bounded native traces with explicit parent association and unknown acceptance."""
    required = {"task_id", "session_id", "parent_call_id", "desktop_session_id"}
    if set(mapping) != required or any(
        not isinstance(value, str) or not value.strip() or len(value) > 512
        for value in mapping.values()
    ):
        raise AutError("desktop: four explicit bounded mapping identifiers are required")
    coverage: Counter[str] = Counter()
    budget = [0, 0]
    raw_events = _read(trace_path, coverage, budget, mapping["desktop_session_id"])
    raw_responses = _read(response_path, coverage, budget)
    events: dict[tuple[int, int, int], dict[str, Any]] = {}
    for row in raw_events:
        if row.get("session_id") is not None and row["session_id"] != mapping["desktop_session_id"]:
            raise AutError("desktop: explicit session mismatch")
        key = _identity(row)
        if key in events:
            if events[key] != row:
                raise AutError("desktop: conflicting event copies")
            coverage["duplicate_events"] += 1
        else:
            events[key] = row
    responses: dict[tuple[int, int, int], dict[str, Any]] = {}
    unkeyed_responses: list[dict[str, Any]] = []
    for row in raw_responses:
        if not isinstance(row.get("response"), dict):
            raise AutError("desktop: invalid response envelope")
        identity_fields = ("writer_pid", "writer_proc_start_ms", "command_seq")
        if any(row.get(field) is None for field in identity_fields):
            if any(
                row.get(field) is not None and not _integer(row[field]) for field in identity_fields
            ):
                raise AutError("desktop: invalid response identity")
            unkeyed_responses.append(row["response"])
            continue
        key = _identity(row, "command_seq")
        if key in responses and responses[key] != row["response"]:
            raise AutError("desktop: conflicting response copies")
        responses[key] = row["response"]
    writers: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for key, row in sorted(events.items()):
        writers[key[:2]].append(row)
    commands: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    matched: set[tuple[int, int, int]] = set()
    for writer, rows in writers.items():
        metas = [row for row in rows if row.get("event") == "trace.meta"]
        coverage["missing_meta"] += not metas
        coverage["unknown_schemas"] += sum(meta.get("schema") != 1 for meta in metas)
        seqs = [row["seq"] for row in rows]
        coverage["sequence_gaps"] += sum(b != a + 1 for a, b in zip(seqs, seqs[1:]))
        pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
        spans: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
        for row in rows:
            name = row.get("command")
            if row.get("event") not in {"command.start", "command.end"}:
                continue
            if not isinstance(name, str) or not name or len(name) > 512:
                raise AutError("desktop: invalid command name")
            if row["event"] == "command.start":
                pending[name].append(row)
            else:
                start = pending[name].pop() if pending[name] else None
                spans.append((start, row))
                coverage["unpaired_ends"] += start is None
        for stack in pending.values():
            for start in stack:
                spans.append((start, None))
                coverage["unpaired_starts"] += 1
        for start, end in spans:
            anchor = start if start is not None else end
            assert anchor is not None
            key = (*writer, anchor["seq"])
            identifier = digest(
                [
                    mapping["desktop_session_id"],
                    *key,
                    "start" if start is not None else "orphan_end",
                ]
            )
            result: dict[str, Any] = dict(
                id=identifier,
                command_hash=digest(anchor["command"]),
                status="unknown",
                duration_ms=None,
                delivery="unknown",
                retry="unknown",
                disposition_conflict=False,
                response_code=None,
                trace_code=end.get("code")
                if end is not None and isinstance(end.get("code"), str) and end.get("code") in CODES
                else None,
            )
            if start is not None and end is not None:
                if type(end.get("ok")) is bool:
                    result["status"] = "ok" if end["ok"] else "error"
                if _integer(end.get("duration_ms")):
                    result["duration_ms"] = end["duration_ms"]
            response = responses.get(key) if start is not None else None
            if response is not None:
                matched.add(key)
                if (
                    response.get("command") != anchor["command"]
                    or type(response.get("ok")) is not bool
                ):
                    coverage["mismatched_responses"] += 1
                else:
                    result.update(_disposition(response))
                    if end is not None and response["ok"] != end.get("ok"):
                        result["disposition_conflict"] = True
                        coverage["response_outcome_conflicts"] += 1
            stamp = anchor["ts_ms"]
            if not _integer(stamp):
                raise AutError("desktop: invalid event timestamp")
            try:
                timestamp = datetime.fromtimestamp(int(stamp) / 1000, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError) as exc:
                raise AutError("desktop: invalid event timestamp") from exc
            commands.append(result)
            if start is None or end is None:
                continue
            observations.append(
                dict(
                    version=1,
                    kind="tool",
                    id=identifier,
                    timestamp=timestamp,
                    status=result["status"],
                    duration_ms=result["duration_ms"],
                )
            )
    orphan_responses = []
    for response in [
        *unkeyed_responses,
        *(responses[key] for key in sorted(set(responses) - matched)),
    ]:
        sanitized = _disposition(response)
        orphan_responses.append(
            dict(
                association="unknown",
                status="ok"
                if response.get("ok") is True
                else "error"
                if response.get("ok") is False
                else "unknown",
                response_code=sanitized["response_code"],
                delivery="unknown",
                retry="unknown",
            )
        )
    coverage["orphan_responses"] = len(orphan_responses)
    coverage["missing_responses"] = len(commands) - len(matched)
    coverage["events"] = len(events)
    coverage["commands"] = len(commands)
    flags = (
        "missing_sources",
        "truncated_exports",
        "source_warnings",
        "filtered_exports",
        "partial_lines",
        "malformed_records",
        "missing_meta",
        "unknown_schemas",
        "sequence_gaps",
        "unpaired_starts",
        "unpaired_ends",
        "mismatched_responses",
        "response_outcome_conflicts",
        "orphan_responses",
    )
    complete = bool(events) and not any(coverage[flag] for flag in flags)
    receipt: dict[str, Any] = dict(
        version=1,
        source="agent-desktop",
        mapping={key: digest(value) for key, value in mapping.items()},
        commands=sorted(commands, key=lambda row: row["id"]),
        orphan_responses=sorted(orphan_responses, key=digest),
        coverage={**dict(coverage), "complete": complete},
        task_acceptance="unknown",
    )
    # Stable stream identity excludes parent calls so children never become parent tool calls.
    ingestion = ingest(
        store.root / "observations.sqlite3",
        observations,
        source="agent-desktop",
        stream="commands",
        role="primary",
    )
    receipt["ingestion"] = ingestion
    receipt["persistence"] = "disputed" if ingestion["conflicts"] else "stored"
    receipt["success"] = not bool(ingestion["conflicts"])
    if ingestion["conflicts"]:
        receipt["coverage"]["complete"] = False
    import_id = store.put("desktop_imports", receipt)
    if ingestion["conflicts"]:
        raise AutError(
            f"desktop: observation conflict; disputed receipt {import_id}; "
            "some observations may already be stored; conflict unresolved"
        )
    return {**receipt, "import_id": import_id}
