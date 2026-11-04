"""Transport: push normalized rows to the sink.

A scan's usage rows go over the n8n webhook (behind Cloudflare Access) when one is configured, and
fall back to direct Postgres (transport.pg_dsn) when it fails, or go to Postgres alone without a
webhook. Everything else (session, activity, time, outcome, label and heartbeat rows) goes over
pg_dsn only. All of it is idempotent at the sink via ON CONFLICT, so re-delivery is harmless.

A sender that cannot deliver RAISES, so the watermark is not committed and the events are re-sent
on the next scan. The one exception is a webhook-only machine: it has no route for the non-usage
rows, so they are skipped with one stderr note and the watermark still moves.

Delivery is strict on purpose: redirects are NOT followed (a 3xx that turns the POST into a
GET could otherwise look like success and advance the watermark over data the sink never
stored), and any non-2xx is a hard failure.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

from . import __version__
from .config import AutError, Config
from .event import ActivityEvent, Row, TimeInterval, UsageEvent
from .net import HTTP_ERRORS, NoRedirect
from .redact import redact
from .sink import LOCAL_HOSTS, connect, write_many

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


def _post_webhook(url: str, secret: str, batch: list[UsageEvent],
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
    return (f"INSERT INTO {table} ({', '.join(columns)}) VALUES "  # nosec B608 # constants only
            f"({', '.join(['%s'] * len(columns))}) ON CONFLICT ({conflict}) DO NOTHING")


def _session_rows(batch: list[UsageEvent]) -> list[tuple]:
    return [tuple(r[c] for c in SESSION_COLUMNS) for r in map(UsageEvent.session_row, batch) if r]


def _activity_rows(acts: list[ActivityEvent]) -> list[tuple]:
    return [(a.event_id, a.ts, a.machine, a.agent, a.kind, a.session_id, a.turn_id,
             json.dumps(a.attrs, sort_keys=True)) for a in acts]


_SESSION_SQL = _insert_sql(SESSION_TABLE, SESSION_COLUMNS, "event_id")
_ACTIVITY_SQL = _insert_sql(ACTIVITY_TABLE, ACTIVITY_COLUMNS, "event_id, ts").replace(
    "%s)", "%s::jsonb)")                 # the last placeholder is attrs


_TIME_SQL = _insert_sql(TIME_TABLE, TIME_COLUMNS, "event_id, ts").replace("%s)", "%s::jsonb)")


def _time_rows(times: list[TimeInterval]) -> list[tuple]:
    return [(t.event_id, t.ts, t.end_ts, t.machine, t.source, t.app, t.title, t.category,
             t.work_relevant, json.dumps(t.attrs, sort_keys=True)) for t in times]


def _split(batch: list[Row]) -> tuple[list[UsageEvent], list[ActivityEvent], list[TimeInterval]]:
    usage = [e for e in batch if isinstance(e, UsageEvent)]
    acts = [e for e in batch if isinstance(e, ActivityEvent)]
    return usage, acts, [e for e in batch if isinstance(e, TimeInterval)]


def insert_pg(dsn: str, batch: list[Row], with_usage: bool = True) -> None:
    """Usage rows, their session rows, and the activity and time rows of the batch in ONE
    transaction: all land or none does, so a raise leaves the watermark where it was and the batch
    is re-sent. with_usage=False: the webhook already carried the usage rows."""
    usage, acts, times = _split(batch)
    rows = [tuple(e.to_dict()[c] for c in COLUMNS) for e in usage] if with_usage else []
    sessions = _session_rows(usage)
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            if rows:
                cur.executemany(_insert_sql(TABLE, COLUMNS, "event_id, ts"), rows)
            if sessions:
                cur.executemany(_SESSION_SQL, sessions)
            if acts:
                cur.executemany(_ACTIVITY_SQL, _activity_rows(acts))
            if times:
                cur.executemany(_TIME_SQL, _time_rows(times))
        conn.commit()


def send_activity_rows(dsn: str, acts: list[ActivityEvent]) -> None:
    """Activity rows only (the activity backfill). Idempotent on (event_id, ts); raises on failure."""
    write_many(dsn, _ACTIVITY_SQL, _activity_rows(acts))


def send_session_rows(dsn: str, rows: list[dict]) -> None:
    """Session rows only (the sessions-only backfill). Idempotent on event_id; raises on failure."""
    write_many(dsn, _SESSION_SQL, [tuple(r[c] for c in SESSION_COLUMNS) for r in rows])


def make_sender(cfg: Config) -> Callable[[list[Row]], None]:
    """Return a send(batch): usage over the webhook, or over pg_dsn without one or when it fails,
    and every other row over pg_dsn. Raises (before any send) if nothing is configured or a
    configured webhook is missing its secret."""
    url, secret, dsn = cfg.webhook_url, cfg.webhook_secret, cfg.pg_dsn
    if not url and not dsn:
        raise AutError(
            "no transport configured: set [transport].webhook_url (and/or pg_dsn) in your "
            "config, or run with --dry-run to preview without sending."
        )
    if url and not secret:
        raise AutError(
            "transport.webhook_url is set but webhook_secret is missing or unresolved; "
            "refusing to send unauthenticated. Set webhook_secret (or its env: value)."
        )
    if url:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        # Allowlist, not blocklist: only https, or http to localhost for testing. Anything else
        # (http to a remote host, or ftp:/file:/no-scheme) could leak the secret or misfire.
        if not (scheme == "https" or (scheme == "http" and host in LOCAL_HOSTS)):
            raise AutError(
                f"refusing to send the webhook secret to '{url}': only https:// "
                f"(or http:// to localhost for testing) is allowed, not '{scheme or 'no'}-scheme'."
            )
        # A warning, not a refusal: a webhook outside Access works today without these headers.
        if bool(cfg.cf_access_client_id) != bool(cfg.cf_access_client_secret):
            print("aut: warning: only one of cf_access_client_id and cf_access_client_secret is "
                  "set, so no Cloudflare Access headers are sent.", file=sys.stderr)

    note_shown = fallback_shown = False

    def send(batch: list[Row]) -> None:
        nonlocal note_shown, fallback_shown
        if not batch:
            return
        usage, acts, times = _split(batch)
        webhook_err: Exception | None = None
        if url and secret:
            try:
                if usage:
                    _post_webhook(url, secret, usage, cfg.cf_access_headers)
            except (*HTTP_ERRORS, RuntimeError) as exc:
                webhook_err = exc
                if dsn and not fallback_shown:      # printed first, so a PG failure shows both
                    fallback_shown = True
                    print(f"aut: warning: webhook delivery failed, using transport.pg_dsn: "
                          f"{_local_error(exc)}", file=sys.stderr)
            else:
                if acts or times or any(e.session for e in usage):
                    if dsn:
                        insert_pg(dsn, batch, with_usage=False)
                    elif not note_shown:
                        # shortcut: the webhook carries usage only; upgrade when a machine runs
                        # webhook-only (time rows have no backfill, so that machine loses them).
                        note_shown = True
                        print("aut: note: session/activity/time rows not sent over the webhook "
                              "(needs transport.pg_dsn)", file=sys.stderr)
                return
        if dsn:
            insert_pg(dsn, batch)
            return
        raise RuntimeError(f"webhook delivery failed and no PG fallback: {webhook_err}") \
            from webhook_err

    return send


def scrub_error(exc: BaseException) -> str:
    """The error stored in a scan row: the exception TYPE only. Messages can carry a file path
    (which names a project) or part of a credential, and no redaction pattern catches every
    form, so the full message stays on this machine's stderr and never reaches the sink."""
    return type(exc).__name__[:_ERROR_MAX]


def _local_error(exc: BaseException) -> str:
    """The error for this machine's stderr: type and message on one line, credentials redacted."""
    return f"{type(exc).__name__}: {redact(' '.join(str(exc).split()))}"


def scan_row(cfg: Config, collector: str, started_at: datetime, status: str = "ok",
             error: str | None = None, events: int = 0, human: int = 0,
             newest_ts: datetime | None = None) -> dict:
    """One agents.collector_scan row, finished now."""
    return {"machine": cfg.machine, "collector": collector, "started_at": started_at,
            "finished_at": datetime.now(timezone.utc), "status": status, "events": events,
            "human": human, "newest_ts": newest_ts, "error": error, "aut_version": __version__}


def send_scan_rows(dsn: str, rows: list[dict]) -> None:
    """Scan heartbeat rows. Idempotent on (machine, collector, finished_at); raises on failure."""
    # 5 s, not 15: usage is already delivered, so an unreachable sink should not hold the exit.
    write_many(dsn, _insert_sql(SCAN_TABLE, SCAN_COLUMNS, "machine, collector, finished_at"),
               [tuple(r[c] for c in SCAN_COLUMNS) for r in rows], timeout=5)


def deliver_scan_rows(cfg: Config, rows: list[dict], what: str = "scan") -> None:
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
        print(f"aut: warning: {what} heartbeat not delivered: {_local_error(exc)}", file=sys.stderr)
