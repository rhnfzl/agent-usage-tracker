"""Transport: push normalized rows to the sink.

A scan's usage rows go over the n8n webhook (behind Cloudflare Access) when one is configured, and
fall back to direct Postgres (transport.pg_dsn) when it fails, or go to Postgres alone without a
webhook. Everything else (session, activity, time, outcome, label and heartbeat rows) goes over
pg_dsn only. All of it is idempotent at the sink via ON CONFLICT, so re-delivery is harmless.

A sender that cannot deliver or durably queue rows raises, so the watermark stays unchanged.
Webhook-only machines keep session, activity and time rows in a private local SQLite outbox.
Postgres delivery replays that outbox before sending new rows.

Delivery is strict on purpose: redirects are NOT followed (a 3xx that turns the POST into a
GET could otherwise look like success and advance the watermark over data the sink never
stored), and any non-2xx is a hard failure.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal
from urllib.parse import urlparse

from . import __version__
from .config import AutError, Config
from .event import ActivityEvent, Row, TimeInterval, UsageEvent
from .net import HTTP_ERRORS, LOCAL_HOSTS, NoRedirect
from .redact import redact
from .sink import connect, write_many
from .state import state_path

# Column order shared with the homelab schema (agents.usage_event). Keep in sync.
COLUMNS = (
    "event_id", "ts", "machine", "agent", "kind", "model", "provider",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "total_tokens", "est_cost_usd", "project",
)
TABLE = "agents.usage_event"

# One row per collector per scan (agents.collector_scan): proof a machine ran and how it ended.
SCAN_COLUMNS = (
    "machine", "collector", "started_at", "finished_at", "status", "events", "human",
    "newest_ts", "error", "aut_version",
)
SCAN_TABLE = "agents.collector_scan"

# One row per usage event_id (agents.event_session): which session, parent and turn it belongs to.
SESSION_COLUMNS = (
    "event_id", "machine", "agent", "session_id", "parent_session_id", "run_origin", "turn_id",
)
SESSION_TABLE = "agents.event_session"

# One row per non-token activity (agents.activity_event): steers, commits, turns. attrs is jsonb.
ACTIVITY_COLUMNS = ("event_id", "ts", "machine", "agent", "kind", "session_id", "turn_id", "attrs")
ACTIVITY_TABLE = "agents.activity_event"

# One row per active-time interval (agents.time_interval): ActivityWatch window time and manual
# entries. title is private (redacted on this machine; retention is off by default, see CONTRACT.md).
TIME_COLUMNS = ("event_id", "ts", "end_ts", "machine", "source", "app", "title", "category",
                "work_relevant", "attrs")
TIME_TABLE = "agents.time_interval"
_ERROR_MAX = 300
_JSONB_COLUMN = "attrs"              # the one jsonb column of these tables
ScanStatus = Literal["ok", "degraded", "error"]


class DeliveryError(RuntimeError):
    """Rows could not be delivered and no route is left: an outage main reports on one line."""


def post_webhook(url: str, secret: str, batch: list[UsageEvent],
                  extra_headers: dict[str, str] | None = None) -> None:
    body = json.dumps({"events": [e.to_dict() for e in batch]}).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-AUT-Secret": secret}
    if extra_headers:                    # e.g. CF-Access-Client-Id/Secret to clear Cloudflare Access
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:            # 3xx (no-follow) / 4xx / 5xx
        raise RuntimeError(f"webhook HTTP {exc.code}") from exc
    if not (200 <= status < 300):
        raise RuntimeError(f"webhook HTTP {status}")


def _insert_sql(table: str, columns: tuple[str, ...], conflict: str) -> str:
    marks = ", ".join("%s::jsonb" if c == _JSONB_COLUMN else "%s" for c in columns)
    return (f"INSERT INTO {table} ({', '.join(columns)}) VALUES "  # nosec B608 # constants only
            f"({marks}) ON CONFLICT ({conflict}) DO NOTHING")


def _params(row: dict[str, Any], columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(json.dumps(row[c], sort_keys=True) if c == _JSONB_COLUMN else row[c]
                 for c in columns)


def _session_rows(batch: list[UsageEvent]) -> list[tuple[Any, ...]]:
    return [_params(r, SESSION_COLUMNS) for r in map(UsageEvent.session_row, batch) if r]


_USAGE_SQL = _insert_sql(TABLE, COLUMNS, "event_id, ts")
_SESSION_SQL = _insert_sql(SESSION_TABLE, SESSION_COLUMNS, "event_id")
_ACTIVITY_SQL = _insert_sql(ACTIVITY_TABLE, ACTIVITY_COLUMNS, "event_id, ts")
_TIME_SQL = _insert_sql(TIME_TABLE, TIME_COLUMNS, "event_id, ts")
_SCAN_SQL = _insert_sql(SCAN_TABLE, SCAN_COLUMNS, "machine, collector, finished_at")
_OUTBOX_TYPES = {"session": (_SESSION_SQL, SESSION_COLUMNS),
                 "activity": (_ACTIVITY_SQL, ACTIVITY_COLUMNS), "time": (_TIME_SQL, TIME_COLUMNS)}
_OUTBOX_BATCH = 500


def outbox_path() -> Path:
    """Private deferred sink rows, kept independently of collector watermarks."""
    return state_path().parent / "delivery-outbox" / "pending.sqlite3"


@contextmanager
def _outbox() -> Iterator[sqlite3.Connection]:
    path = outbox_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        conn = sqlite3.connect(path, timeout=30)
        try:
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("CREATE TABLE IF NOT EXISTS pending ("
                         "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "kind TEXT NOT NULL, event_id TEXT NOT NULL, ts TEXT NOT NULL, "
                         "payload TEXT NOT NULL, UNIQUE(kind, event_id, ts))")
            yield conn
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        raise DeliveryError("local delivery outbox failed; watermark unchanged. "
                            f"Keep {path} and fix its storage or permissions: {exc}") from exc


def outbox_status() -> dict[str, int]:
    """Pending rows by sink type. Corrupt or unreadable storage raises, never appears empty."""
    if not outbox_path().exists():
        return {}
    with _outbox() as conn:
        return dict(conn.execute("SELECT kind, COUNT(*) FROM pending GROUP BY kind"))


def _queue_rest(batch: list[Row]) -> None:
    with _outbox() as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for event in batch:
                if isinstance(event, UsageEvent):
                    row = event.session_row()
                    if row is None:
                        continue
                    kind = "session"
                else:
                    row = asdict(event)
                    kind = "activity" if isinstance(event, ActivityEvent) else "time"
                snapshot = (kind == "activity" and row["kind"] in ("turn", "link")
                            and "revision_sources" in row["attrs"])
                if snapshot:
                    _revision_sources(row["attrs"])
                    old = conn.execute("SELECT payload FROM pending "
                                       "WHERE kind=? AND event_id=? AND ts=?",
                                       (kind, event.event_id, event.ts or "")).fetchone()
                    if old and not _snapshot_covers(json.loads(old[0])["attrs"], row["attrs"]):
                        continue
                conn.execute("INSERT INTO pending(kind, event_id, ts, payload) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(kind, event_id, ts) DO UPDATE SET payload=excluded.payload "
                             "WHERE ?", (kind, event.event_id, event.ts or "",
                                         json.dumps(row, sort_keys=True), snapshot))


def replay_outbox(cfg: Config) -> int:
    """Replay a finite FIFO snapshot, at most 500 rows in memory, deleting after PG commit only."""
    dsn = cfg.require_pg("outbox replay")
    if not outbox_path().exists():
        return 0
    delivered = 0
    deferred = False
    after = 0
    with _outbox() as conn:
        ceiling = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM pending").fetchone()[0]
        while True:
            with conn:
                # A writer cannot replace a row between its delivery and local acknowledgement.
                conn.execute("BEGIN IMMEDIATE")
                pending = conn.execute("SELECT rowid, kind, payload FROM pending "
                                       "WHERE rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
                                       (after, ceiling, _OUTBOX_BATCH)).fetchall()
                if not pending:
                    break
                records = [(rowid, kind, json.loads(payload)) for rowid, kind, payload in pending]
                blocked: set[int] = set()
                with connect(dsn) as pg:
                    with pg.cursor() as cur:
                        for kind, (sql, columns) in _OUTBOX_TYPES.items():
                            rows = [row for _, k, row in records if k == kind]
                            if kind == "activity":
                                waiting = _write_activities(cur, rows)
                                keys = {(r["event_id"], r["ts"]) for r in waiting}
                                blocked.update(rowid for rowid, k, row in records if k == kind
                                               and (row["event_id"], row["ts"]) in keys)
                            elif rows:
                                cur.executemany(sql, [_params(row, columns) for row in rows])
                        if any(kind not in _OUTBOX_TYPES for _, kind, _ in pending):
                            raise DeliveryError("unknown row type in local delivery outbox")
                    pg.commit()
                conn.executemany("DELETE FROM pending WHERE rowid=?",
                                 [(r[0],) for r in pending if r[0] not in blocked])
            delivered += len(pending) - len(blocked)
            deferred |= bool(blocked)
            after = pending[-1][0]
    if deferred:
        _snapshot_permission_note()
    return delivered


def _snapshot_permission_note() -> None:
    print(f"aut: note: turn/link snapshot updates queued privately at {outbox_path()}; "
          "the sink role needs UPDATE(attrs) on agents.activity_event. "
          "Grant that permission, then run aut scan to replay them.", file=sys.stderr)


def _write_activities(cur: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordinary events are immutable; covered turn snapshots may refresh their aggregate counts."""
    snapshots, ordinary = [], []
    deferred = []
    can_update = None
    for row in rows:
        if row["kind"] in ("turn", "link") and "revision_sources" in row["attrs"]:
            snapshots.append(row)
        else:
            ordinary.append(row)
    if ordinary:
        cur.executemany(_ACTIVITY_SQL, [_params(row, ACTIVITY_COLUMNS) for row in ordinary])
    for row in sorted(snapshots, key=lambda r: r["event_id"]):
        _revision_sources(row["attrs"])
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (row["event_id"],))
        # Match event_id alone because older Pi snapshots used the turn's end timestamp.
        cur.execute("SELECT ts, agent, kind, machine, attrs FROM agents.activity_event "
                    "WHERE event_id=%s", (row["event_id"],))
        existing = cur.fetchall()
        if not existing:
            cur.executemany(_ACTIVITY_SQL, [_params(row, ACTIVITY_COLUMNS)])
            continue
        for ts, agent, kind, machine, attrs in existing:
            if (agent, kind) != (row["agent"], row["kind"]):
                raise DeliveryError("turn snapshot event identity conflicts with an existing row")
        for ts, agent, kind, machine, attrs in existing:
            if attrs != row["attrs"] and _snapshot_covers(attrs, row["attrs"]):
                if can_update is None:
                    cur.execute("SELECT has_column_privilege(current_user, "
                                "'agents.activity_event', 'attrs', 'UPDATE')", ())
                    can_update = bool(cur.fetchone()[0])
                if not can_update:
                    deferred.append(row)
                    break
                cur.execute("UPDATE agents.activity_event SET attrs=%s::jsonb "
                            "WHERE event_id=%s AND ts=%s",
                            (json.dumps(row["attrs"], sort_keys=True), row["event_id"], ts))
    return deferred


def _revision_sources(attrs: dict[str, Any]) -> set[str]:
    sources = attrs.get("revision_sources")
    if not isinstance(sources, list) or not sources or any(
            not isinstance(source, str) or not source for source in sources):
        raise DeliveryError("turn snapshot has invalid revision source coverage")
    return set(sources)


def _snapshot_covers(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """A shorter or divergent scan cannot replace a more complete turn snapshot."""
    sources = _revision_sources(new)
    if "revision_sources" in old and not _revision_sources(old) <= sources:
        return False
    for key in ("tool_calls", "tool_errors"):
        before, after = old.get(key, 0), new.get(key, 0)
        if type(before) is not int or type(after) is not int or after < before:
            return False
    return True


def _split(batch: list[Row]) -> tuple[list[UsageEvent], list[ActivityEvent], list[TimeInterval]]:
    usage = [e for e in batch if isinstance(e, UsageEvent)]
    acts = [e for e in batch if isinstance(e, ActivityEvent)]
    return usage, acts, [e for e in batch if isinstance(e, TimeInterval)]


def insert_pg(dsn: str, batch: list[Row], with_usage: bool = True) -> None:
    """Deliver rows together; durably queue covered snapshot changes lacking UPDATE permission.
    A raise leaves the watermark unchanged, so committed rows retry with stable IDs.
    with_usage=False: the webhook already carried the usage rows."""
    usage, acts, times = _split(batch)
    rows = [_params(e.to_dict(), COLUMNS) for e in usage] if with_usage else []
    sessions = _session_rows(usage)
    deferred = []
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            if rows:
                cur.executemany(_USAGE_SQL, rows)
            if sessions:
                cur.executemany(_SESSION_SQL, sessions)
            if acts:
                deferred = _write_activities(cur, [asdict(a) for a in acts])
            if times:
                cur.executemany(_TIME_SQL, [_params(asdict(t), TIME_COLUMNS) for t in times])
        conn.commit()
    if deferred:
        _queue_rest([ActivityEvent(**row) for row in deferred])
        _snapshot_permission_note()


def send_activity_rows(dsn: str, acts: list[ActivityEvent]) -> None:
    """Activity rows only (the activity backfill). Idempotent on (event_id, ts); raises on failure."""
    if acts:
        insert_pg(dsn, list(acts))


def send_session_rows(dsn: str, events: list[UsageEvent]) -> None:
    """The events' session rows only (the sessions-only backfill). Idempotent on event_id; raises
    on failure."""
    write_many(dsn, _SESSION_SQL, _session_rows(events))


def webhook_url_allowed(url: str) -> bool:
    """Allowlist, not blocklist: only https, or http to localhost for testing. Anything else
    (http to a remote host, or ftp:/file:/no-scheme) could leak the secret or misfire."""
    try:
        parsed = urlparse(url)
        return parsed.scheme.lower() == "https" or (
            parsed.scheme.lower() == "http" and (parsed.hostname or "").lower() in LOCAL_HOSTS)
    except ValueError:
        return False


def _check_webhook(cfg: Config) -> None:
    """Raises if a configured webhook is missing its secret or would leak it."""
    url = cfg.webhook_url or ""
    if not cfg.webhook_secret:
        raise AutError(
            "transport.webhook_url is set but webhook_secret is missing or unresolved; "
            "refusing to send unauthenticated. Set webhook_secret (or its env: value)."
        )
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not webhook_url_allowed(url):
        raise AutError(
            f"refusing to send the webhook secret to host '{host}': only https:// "
            f"(or http:// to localhost for testing) is allowed, not '{scheme or 'no'}-scheme'."
        )
    # A warning, not a refusal: a webhook outside Access works today without these headers.
    if bool(cfg.cf_access_client_id) != bool(cfg.cf_access_client_secret):
        print("aut: warning: only one of cf_access_client_id and cf_access_client_secret is "
              "set, so no Cloudflare Access headers are sent.", file=sys.stderr)


class _Sender:
    """send(batch): usage over the webhook, or over pg_dsn without one or when it fails, and every
    other row over pg_dsn. Each stderr note shows once per scan."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.note_shown = self.fallback_shown = False
        self.replayed = False

    def __call__(self, batch: list[Row]) -> None:
        if self.cfg.pg_dsn and not self.replayed:
            replay_outbox(self.cfg)
            self.replayed = True
        if not batch:
            return
        cfg = self.cfg
        webhook_err = None
        if cfg.webhook_url and cfg.webhook_secret:
            webhook_err = self._post_usage(batch, cfg.webhook_url, cfg.webhook_secret)
            if webhook_err is None:
                self._send_the_rest(batch)
                return
        if cfg.pg_dsn:
            insert_pg(cfg.pg_dsn, batch)
            return
        raise DeliveryError("webhook delivery failed and no PG fallback: "
                            f"{redacted_error(webhook_err) if webhook_err else 'not sent'}") \
            from webhook_err

    def _post_usage(self, batch: list[Row], url: str, secret: str) -> Exception | None:
        """The webhook's failure, if any: the caller falls back to pg_dsn on it."""
        usage = _split(batch)[0]
        try:
            if usage:
                post_webhook(url, secret, usage, self.cfg.cf_access_headers)
        except (*HTTP_ERRORS, RuntimeError) as exc:
            if self.cfg.pg_dsn and not self.fallback_shown:  # printed first: a PG failure shows both
                self.fallback_shown = True
                print(f"aut: warning: webhook delivery failed, using transport.pg_dsn: "
                      f"{redacted_error(exc)}", file=sys.stderr)
            return exc
        return None

    def _send_the_rest(self, batch: list[Row]) -> None:
        """What the webhook does not carry goes over pg_dsn; its failure raises, watermark stays."""
        usage, acts, times = _split(batch)
        if not (acts or times or any(e.session for e in usage)):
            return
        if self.cfg.pg_dsn:
            insert_pg(self.cfg.pg_dsn, batch, with_usage=False)
        else:
            _queue_rest(batch)
            if not self.note_shown:
                self.note_shown = True
                print(f"aut: note: session/activity/time rows queued privately at {outbox_path()}; "
                      "set transport.pg_dsn and run aut scan to replay them.", file=sys.stderr)


def make_sender(cfg: Config) -> Callable[[list[Row]], None]:
    """Return a send(batch), see _Sender. Raises (before any send) if nothing is configured or a
    configured webhook is missing its secret."""
    if not cfg.webhook_url and not cfg.pg_dsn:
        raise AutError(
            "no transport configured: set [transport].webhook_url (and/or pg_dsn) in your "
            "config, or run with --dry-run to preview without sending."
        )
    if cfg.webhook_url:
        _check_webhook(cfg)
    return _Sender(cfg)


def error_type_name(exc: BaseException) -> str:
    """The error stored in a scan row: the exception TYPE only. Messages can carry a file path
    (which names a project) or part of a credential, and no redaction pattern catches every
    form, so the full message stays on this machine's stderr and never reaches the sink."""
    return type(exc).__name__[:_ERROR_MAX]


def redacted_error(exc: BaseException) -> str:
    """The error for this machine's stderr: type and message on one line, credentials redacted."""
    return f"{type(exc).__name__}: {redact(' '.join(str(exc).split()))}"


def is_operational_failure(exc: Exception) -> bool:
    """An outage, not a bug: a sink or webhook that failed, or an OSError, the OS's own report on a
    file or socket (the line keeps its errno and path). Any other RuntimeError is a bug.
    By module name because the driver is an optional extra; its errors are not imported."""
    return isinstance(exc, (*HTTP_ERRORS, DeliveryError)) \
        or type(exc).__module__.partition(".")[0] == "psycopg"


def scan_row(cfg: Config, collector: str, started_at: datetime, *, status: ScanStatus = "ok",
             error: str | None = None, events: int = 0, human: int = 0,
             newest_ts: datetime | None = None) -> dict[str, Any]:
    """One agents.collector_scan row, finished now."""
    return {"machine": cfg.machine, "collector": collector, "started_at": started_at,
            "finished_at": datetime.now(timezone.utc), "status": status, "events": events,
            "human": human, "newest_ts": newest_ts, "error": error, "aut_version": __version__}


def send_scan_rows(dsn: str, rows: list[dict[str, Any]]) -> None:
    """Scan heartbeat rows. Idempotent on (machine, collector, finished_at); raises on failure."""
    def write(batch: list[dict[str, Any]]) -> None:
        # 5 s, not 15: usage is already delivered, so an unreachable sink should not hold the exit.
        write_many(dsn, _SCAN_SQL, [_params(r, SCAN_COLUMNS) for r in batch], timeout=5)

    try:
        write(rows)
    except Exception as exc:
        # A CHECK with only ok and error refuses a degraded row, and with it the whole delivery:
        # every collector of the scan would look dead. By name: the driver is an optional extra.
        if type(exc).__name__ != "CheckViolation" or all(r["status"] != "degraded" for r in rows):
            raise
        write([{**r, "status": "error"} if r["status"] == "degraded" else r for r in rows])


def deliver_scan_rows(cfg: Config, rows: list[dict[str, Any]], what: str = "scan") -> None:
    """The heartbeat is best effort: whatever goes wrong here must not touch the job it reports
    on, and that includes a missing driver's AutError. No pg_dsn: skipped with a note."""
    if not rows:
        return
    if not cfg.pg_dsn:
        # shortcut: webhook path for scan rows not built; upgrade when a machine runs webhook-only.
        print(f"aut: note: {what} heartbeat not sent (needs transport.pg_dsn)", file=sys.stderr)
        return
    try:
        send_scan_rows(cfg.pg_dsn, rows)
    except Exception as exc:
        print(f"aut: warning: {what} heartbeat not delivered: {redacted_error(exc)}", file=sys.stderr)


@dataclass(slots=True)
class JobBeat:
    """What a one-row job's heartbeat will say; the job's body fills it in."""
    status: ScanStatus = "ok"
    error: str | None = None
    events: int = 0

    def fail(self, exc: BaseException) -> None:
        self.status, self.error = "error", error_type_name(exc)


@contextmanager
def job_heartbeat(cfg: Config, name: str, started: datetime, *,
                  dry_run: bool) -> Iterator[JobBeat]:
    """One heartbeat row when the body ends: a failure still reports and re-raises, Ctrl-C leaves
    at once and sends nothing, a dry run stays silent. run_scan keeps its own: it sends one row per
    collector in a single delivery."""
    beat = JobBeat()
    interrupted = False
    try:
        yield beat
    except BaseException as exc:
        beat.fail(exc)
        interrupted = isinstance(exc, KeyboardInterrupt)
        raise
    finally:
        if not dry_run and not interrupted:
            deliver_scan_rows(cfg, [scan_row(cfg, name, started, status=beat.status,
                                             error=beat.error, events=beat.events)], name)
