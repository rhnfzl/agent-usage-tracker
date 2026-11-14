"""T3 app activity from its read-only v2 projection, after an explicit metric cutover."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Iterator

from ..config import Config
from ..event import (
    ActivityEvent,
    Batch,
    CollectorUnavailable,
    EventSession,
    Row,
    RunOrigin,
    UsageEvent,
    make_event_id,
    parse_ts,
)
from . import codex
from ._util import project_name
from .activity_common import first_record, human_event, ms_between

NAME = "t3"
_PREFIX = "orchestration_v2_projection_"
_MAX_ROWS = 100_000
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_QUERY_BYTES = 16 * 1024 * 1024
_PAGE = 500
_DRIVERS = {"codex": "codex", "claudeAgent": "claude_code"}


def database_path(home: Path) -> Path:
    return home / ".t3/userdata/statev2.sqlite"


def _stamp(value: object) -> datetime:
    stamp = parse_ts(value)
    if stamp is None or stamp.tzinfo is None:
        raise ValueError("missing timezone-aware timestamp")
    return stamp


def _payload(value: str) -> dict[str, Any]:
    obj = json.loads(value)
    if not isinstance(obj, dict):
        raise ValueError("projection payload is not an object")
    return obj


def confirmed_human(payload: dict[str, Any]) -> bool:
    return payload.get("createdBy") == "user" and payload.get("creationSource") in {"web", "mobile"}


@contextmanager
def connection(home: Path) -> Iterator[sqlite3.Connection]:
    """A live WAL-compatible snapshot; never create a database or read auth tables."""
    con = sqlite3.connect(database_path(home).as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con
    finally:
        con.close()


def _read(
    con: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    """Bound SQLite's returned cells before Python allocation; reject incomplete query results."""
    description = con.execute(f"SELECT * FROM ({query}) LIMIT 0", params).description
    assert description is not None
    columns = []
    for column in description:
        name = '"' + column[0].replace('"', '""') + '"'
        columns.extend(
            [
                f"CASE WHEN typeof({name}) IN ('text','blob') "
                f"THEN substr(CAST({name} AS BLOB),1,{_MAX_RECORD_BYTES + 1}) ELSE {name} END",
                f"typeof({name})",
            ]
        )
    cursor = con.execute(f"SELECT {','.join(columns)} FROM ({query}) LIMIT {_MAX_ROWS + 1}", params)
    rows: list[tuple[Any, ...]] = []
    total = 0
    for bounded in cursor:
        values, types = bounded[::2], bounded[1::2]
        size = sum(len(value) if isinstance(value, bytes) else 8 for value in values)
        total += size
        if len(rows) >= _MAX_ROWS or size > _MAX_RECORD_BYTES or total > _MAX_QUERY_BYTES:
            raise ValueError("projection exceeds bounded read limit")
        rows.append(
            tuple(
                value.decode("utf-8") if kind == "text" else value
                for value, kind in zip(values, types)
            )
        )
    return rows


@dataclass(frozen=True)
class Binding:
    session_id: str
    start: datetime
    end: datetime
    origin: RunOrigin = "interactive"


@dataclass
class Snapshot:
    messages: list[tuple[Any, ...]]
    runs: list[tuple[Any, ...]]
    projects: dict[str, str | None]
    bindings: dict[tuple[str, str], list[Binding]]
    native_ids: dict[str, tuple[str, str]]
    message_runs: dict[str, list[tuple[Any, ...]]]
    tool_attrs: dict[str, dict[str, Any]]
    covered_runs: set[str]


def snapshot(con: sqlite3.Connection, since: datetime, *, include_text: bool = True) -> Snapshot:
    cutoff = (since.isoformat(),)
    current = (
        f"WITH current_runs AS (SELECT run_id,thread_id,provider_thread_id "
        f"FROM {_PREFIX}runs WHERE julianday(requested_at) >= julianday(?) "
        "OR julianday(requested_at) IS NULL) "
    )
    raw_runs = _read(
        con,
        current
        + f"SELECT run_id,thread_id,provider,provider_thread_id,status,requested_at,completed_at,"
        "json_object('userMessageId',json_extract(payload_json,'$.userMessageId')) AS payload_json "
        f"FROM {_PREFIX}runs WHERE run_id IN (SELECT run_id FROM current_runs) "
        "ORDER BY completed_at,run_id",
        cutoff,
    )
    projects = dict(
        _read(
            con,
            current
            + "SELECT project_id,workspace_root FROM projection_projects WHERE project_id IN "
            f"(SELECT project_id FROM {_PREFIX}threads WHERE thread_id IN "
            "(SELECT thread_id FROM current_runs))",
            cutoff,
        )
    )
    thread_projects = {}
    for tid, pid, raw in _read(
        con,
        current + "SELECT thread_id,project_id,"
        "json_object('worktreePath',json_extract(payload_json,'$.worktreePath')) AS payload_json "
        f"FROM {_PREFIX}threads WHERE thread_id IN (SELECT thread_id FROM current_runs)",
        cutoff,
    ):
        payload = _payload(raw)
        thread_projects[tid] = project_name(payload.get("worktreePath") or projects.get(pid))
    text = "json_extract(payload_json,'$.text')" if include_text else "NULL"
    messages = []
    proofs: dict[str, tuple[str, datetime, RunOrigin]] = {}
    for row in _read(
        con,
        "SELECT rowid,message_id,thread_id,run_id,role,created_at,"
        "json_object('createdBy',json_extract(payload_json,'$.createdBy'),"
        "'creationSource',json_extract(payload_json,'$.creationSource'),"
        f"'text',{text}) AS payload_json "
        f"FROM {_PREFIX}messages WHERE role = 'user' AND "
        "(julianday(created_at) >= julianday(?) OR julianday(created_at) IS NULL) ORDER BY rowid",
        cutoff,
    ):
        payload = _payload(row[6])
        stamp = _stamp(row[5])
        if row[4] == "user" and stamp >= since:
            origin: RunOrigin = (
                "interactive"
                if confirmed_human(payload)
                else "automated"
                if payload.get("createdBy") == "agent"
                else "unknown"
            )
            proofs[row[1]] = row[2], stamp, origin
            if origin == "interactive":
                messages.append((*row[:6], payload))
    native_ids = {}
    native_threads = {}
    for ptid, tid, raw in _read(
        con,
        current + "SELECT provider_thread_id,thread_id,json_object('nativeThreadRef',json_object("
        "'driver',json_extract(payload_json,'$.nativeThreadRef.driver'),"
        "'nativeId',json_extract(payload_json,'$.nativeThreadRef.nativeId'),"
        "'strength',json_extract(payload_json,'$.nativeThreadRef.strength'))) AS payload_json "
        f"FROM {_PREFIX}provider_threads WHERE provider_thread_id IN "
        "(SELECT provider_thread_id FROM current_runs)",
        cutoff,
    ):
        ref = _payload(raw).get("nativeThreadRef")
        if isinstance(ref, dict) and ref.get("strength") == "strong":
            agent, sid = _DRIVERS.get(str(ref.get("driver"))), ref.get("nativeId")
            if agent and isinstance(sid, str) and sid:
                native_ids[ptid] = agent, sid
                native_threads[ptid] = tid
    runs: list[tuple[Any, ...]] = []
    message_runs: dict[str, list[tuple[Any, ...]]] = {}
    bindings: dict[tuple[str, str], list[Binding]] = {}
    run_bindings: dict[str, tuple[str, str]] = {}
    for row in raw_runs:
        payload = _payload(row[7])
        proof = proofs.get(str(payload.get("userMessageId")))
        if not proof or proof[0] != row[1]:
            continue
        start = min(proof[1], _stamp(row[5]))
        if start < since:
            continue
        end = _stamp(row[6]) if row[6] else datetime.now(timezone.utc)
        if end < start:
            raise ValueError("run ends before its human prompt")
        if proof[2] == "interactive":
            runs.append(row)
            message_runs.setdefault(str(payload.get("userMessageId")), []).append(row)
        key = native_ids.get(row[3])
        if key and native_threads.get(row[3]) == row[1]:
            run_bindings[row[0]] = key
            bindings.setdefault(key, []).append(Binding("t3:" + row[1], start, end, proof[2]))
    # A native identity bound to several app threads is not evidence of one canonical session.
    bindings = {
        key: spans
        for key, spans in bindings.items()
        if len({span.session_id for span in spans}) == 1
    }
    tool_attrs: dict[str, dict[str, Any]] = {}
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_PREFIX + "turn_items",)
    ).fetchone():
        for runid, kind, status, exit_code in _read(
            con,
            current + f"SELECT run_id,type,status,json_extract(payload_json,'$.exitCode') "
            f"FROM {_PREFIX}turn_items WHERE run_id IN (SELECT run_id FROM current_runs) "
            "AND type IN ('command_execution','dynamic_tool','file_change')",
            cutoff,
        ):
            attrs = tool_attrs.setdefault(runid, {"tool_calls": 0, "tool_errors": 0, "by_tool": {}})
            attrs["tool_calls"] += 1
            attrs["tool_errors"] += status == "failed" or (
                type(exit_code) is int and exit_code != 0
            )
            attrs["by_tool"][kind] = attrs["by_tool"].get(kind, 0) + 1
    covered_runs = {rid for rid, key in run_bindings.items() if key in bindings}
    return Snapshot(
        messages,
        runs,
        thread_projects,
        bindings,
        native_ids,
        message_runs,
        tool_attrs,
        covered_runs,
    )


def _since(cfg: Config) -> datetime:
    if cfg.t3_activity_since is None:
        raise CollectorUnavailable(
            "t3: set t3_activity_since to an approved metric cutover timestamp"
        )
    try:
        return _stamp(cfg.t3_activity_since)
    except ValueError as exc:
        raise CollectorUnavailable(
            "t3: t3_activity_since needs a timezone-aware ISO timestamp"
        ) from exc


def native_bindings(cfg: Config) -> dict[tuple[str, str], list[Binding]]:
    """Read app proof once per native collector pass, without blocking native collection."""
    if "t3" not in cfg.agents or not cfg.t3_activity_since or not database_path(cfg.home).exists():
        return {}
    try:
        with connection(cfg.home) as con:
            return snapshot(con, _since(cfg), include_text=False).bindings
    except (OSError, sqlite3.Error, ValueError, TypeError, CollectorUnavailable) as exc:
        # T3 reports its own degraded heartbeat; an unavailable app never blocks native usage.
        import warnings

        warnings.warn(
            f"t3: native session linking unavailable ({type(exc).__name__})", stacklevel=2
        )
        return {}


def native_clients(cfg: Config) -> set[tuple[str, str]]:
    """Client provenance can quarantine T3 Codex mirrors before a strong app binding exists."""
    if "t3" not in cfg.agents or not cfg.t3_activity_since:
        return set()
    clients = set()
    # shortcut: the native thread stays T3-owned after cutover; direct CLI resumes need per-launch provenance.
    for path in codex.session_files(cfg.home):
        head = first_record(path)
        payload = head.get("payload") if head and head.get("type") == "session_meta" else None
        if isinstance(payload, dict) and payload.get("originator") == "T3 Code":
            sid = payload.get("id") or payload.get("session_id")
            if isinstance(sid, str):
                clients.add(("codex", sid))
    return clients


def canonicalize(
    rows: list[Row],
    bindings: dict[tuple[str, str], list[Binding]],
    clients: Collection[tuple[str, str]] = (),
    since: datetime | None = None,
) -> list[Row]:
    """Map proven native intervals and omit only their app-mirrored human rows."""
    out: list[Row] = []
    for row in rows:
        if not isinstance(row, (UsageEvent, ActivityEvent)):
            out.append(row)
            continue
        if isinstance(row, ActivityEvent) and row.kind == "link" \
                and row.attrs.get("run_origin") in {"automated", "subagent"}:
            out.append(row)
            continue
        sess = row.session if isinstance(row, UsageEvent) else None
        sid = (
            sess.session_id if sess else row.session_id if isinstance(row, ActivityEvent) else None
        )
        stamp = parse_ts(row.ts)
        spans = bindings.get((row.agent, sid), []) if isinstance(sid, str) else []
        matches = [
            span for span in spans if stamp and stamp.tzinfo and span.start <= stamp <= span.end
        ]
        match = matches[0] if matches else None
        if match and len({span.origin for span in matches}) > 1:
            match = Binding(match.session_id, match.start, match.end, "unknown")
        if (
            not match
            and (row.agent, sid) in clients
            and stamp
            and stamp.tzinfo
            and since
            and stamp >= since
        ):
            if isinstance(row, UsageEvent):
                if row.kind == "human":
                    continue
                if sess and sess.run_origin not in {"automated", "subagent"}:
                    sess.run_origin = "unknown"
            elif row.kind in {"steer", "turn"}:
                continue
            elif row.kind == "link":
                row.attrs["run_origin"] = "unknown"
        elif match and match.origin != "interactive":
            if isinstance(row, UsageEvent):
                if row.kind == "human":
                    continue
                if sess and sess.run_origin not in {"automated", "subagent"}:
                    sess.run_origin = match.origin
            elif row.kind in {"steer", "turn"}:
                continue
            elif row.kind == "link":
                row.attrs["run_origin"] = match.origin
        elif match and not (sess and sess.run_origin in {"automated", "subagent"}):
            if isinstance(row, UsageEvent):
                if row.kind == "human":
                    continue
                if sess:
                    sess.session_id = match.session_id
                    sess.run_origin = "interactive"
            elif isinstance(row, ActivityEvent):
                if row.kind in {"steer", "turn"}:
                    continue
                row.session_id = match.session_id
                if row.kind == "link":
                    row.attrs["run_origin"] = "interactive"
        out.append(row)
    return out


def collect(cfg: Config, wm: dict[str, Any]) -> Iterator[Batch]:
    if not database_path(cfg.home).exists() and not cfg.t3_activity_since:
        return
    since = _since(cfg)
    try:
        with connection(cfg.home) as con:
            data = snapshot(con, since)
            sent_messages = set(wm.get("message_ids", []))
            sent_runs = set(wm.get("run_ids", []))
            ambiguous = set()
            eligible: set[str] = set()
            for mid, runs in data.message_runs.items():
                if all(run[0] in data.covered_runs for run in runs):
                    eligible.add(mid)
                else:
                    ambiguous.add(mid)
            by_run = {run[0]: run for run in data.runs}
            pending: list[tuple[str, str, list[Row]]] = []
            for rid, mid, tid, runid, role, ts, payload in data.messages:
                if mid not in data.message_runs:
                    owner = by_run.get(runid)
                    if owner and owner[1] == tid and runid in data.covered_runs:
                        eligible.add(mid)
                if mid not in eligible:
                    ambiguous.add(mid)
                    continue
                if mid in sent_messages:
                    continue
                sid = "t3:" + tid
                text = payload.get("text") if isinstance(payload.get("text"), str) else ""
                sess = EventSession(sid, None, "interactive", runid)
                pending.append(
                    (
                        "message_ids",
                        mid,
                        [
                            human_event(NAME, (mid,), ts, sess, data.projects.get(tid)),
                            ActivityEvent(
                                make_event_id(NAME, "steer", mid),
                                ts,
                                NAME,
                                "steer",
                                sid,
                                runid,
                                {
                                    "via": "typed",
                                    "words": len(text.split()),
                                    "chars": len(text),
                                },
                            ),
                        ],
                    )
                )
            for rid, tid, provider, ptid, status, start, end, raw in data.runs:
                if status != "completed" or not end or rid in sent_runs:
                    continue
                if rid not in data.covered_runs:
                    continue
                pending.append(
                    (
                        "run_ids",
                        rid,
                        [
                            ActivityEvent(
                                make_event_id(NAME, "turn", rid),
                                end,
                                NAME,
                                "turn",
                                "t3:" + tid,
                                rid,
                                {
                                    "duration_ms": ms_between(start, end),
                                    **data.tool_attrs.get(rid, {}),
                                },
                            )
                        ],
                    )
                )
            for offset in range(0, len(pending), _PAGE):
                page = pending[offset : offset + _PAGE]
                rows = [row for key, value, batch in page for row in batch]
                delivered = [(key, value) for key, value, batch in page]

                def advance(delivered: list[tuple[str, str]] = delivered) -> None:
                    for key, value in delivered:
                        ids = wm.setdefault(key, [])
                        if value not in ids:
                            ids.append(value)

                yield rows, advance
            if ambiguous:
                raise CollectorUnavailable(
                    f"t3: {len(ambiguous)} human messages omitted, native overlap is unproved"
                )
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise CollectorUnavailable("t3: projection database is unavailable or invalid") from exc


def describe(cfg: Config, wm: dict[str, Any]) -> str:
    if not database_path(cfg.home).exists() and not cfg.t3_activity_since:
        return "not installed"
    if not database_path(cfg.home).exists():
        return "degraded: T3 database is unavailable"
    if not cfg.t3_activity_since:
        return "degraded: t3_activity_since is not configured"
    return f"activity since {cfg.t3_activity_since}; {len(wm.get('message_ids', []))} acknowledged human messages"
