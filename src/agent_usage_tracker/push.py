"""Transport: push normalized events to the sink.

Default = n8n webhook (one auth story from any machine, behind Cloudflare Access).
Fallback = direct Postgres over Tailscale (only used if the webhook is unavailable and
transport.pg_dsn is set). Both are idempotent at the sink via event_id + ON CONFLICT, so
re-delivery is harmless. A sender that cannot deliver RAISES, so the watermark is not
committed and the events are re-sent on the next scan.

Delivery is strict on purpose: redirects are NOT followed (a 3xx that turns the POST into a
GET could otherwise look like success and advance the watermark over data the sink never
stored), and any non-2xx is a hard failure.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlparse

from .config import Config
from .event import UsageEvent

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
_SESSION_NOTE_SHOWN = False
_ERROR_MAX = 300


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # do not follow; urllib raises HTTPError for the 3xx instead of silently GETting


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post_webhook(url: str, secret: str, batch: list[UsageEvent],
                  extra_headers: dict[str, str] | None = None) -> None:
    body = json.dumps({"events": [e.to_dict() for e in batch]}).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-AUT-Secret": secret}
    if extra_headers:                    # e.g. CF-Access-Client-Id/Secret to clear Cloudflare Access
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as exc:            # 3xx (no-follow) / 4xx / 5xx
        raise RuntimeError(f"webhook HTTP {exc.code}") from exc
    if not (200 <= status < 300):
        raise RuntimeError(f"webhook HTTP {status}")


def _insert_sql(table: str, columns: tuple[str, ...], conflict: str) -> str:
    return (f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
            f"({', '.join(['%s'] * len(columns))}) ON CONFLICT ({conflict}) DO NOTHING")


def _session_rows(batch: list[UsageEvent]) -> list[tuple]:
    return [tuple(r[c] for c in SESSION_COLUMNS) for r in map(UsageEvent.session_row, batch) if r]


def _insert_pg(dsn: str, batch: list[UsageEvent]) -> None:
    """Usage rows and their session rows in ONE transaction: either both land or neither does,
    so a raise leaves the watermark where it was and the batch is re-sent whole."""
    import psycopg  # optional dependency; install with the [postgres] extra

    rows = [tuple(e.to_dict()[c] for c in COLUMNS) for e in batch]
    sessions = _session_rows(batch)
    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.executemany(_insert_sql(TABLE, COLUMNS, "event_id, ts"), rows)
            if sessions:
                cur.executemany(_insert_sql(SESSION_TABLE, SESSION_COLUMNS, "event_id"), sessions)
        conn.commit()


def send_session_rows(dsn: str, rows: list[dict]) -> None:
    """Session rows only (the sessions-only backfill). Idempotent on event_id; raises on failure."""
    if not rows:
        return
    import psycopg  # optional dependency; install with the [postgres] extra

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.executemany(_insert_sql(SESSION_TABLE, SESSION_COLUMNS, "event_id"),
                            [tuple(r[c] for c in SESSION_COLUMNS) for r in rows])
        conn.commit()


def _note_sessions_skipped(batch: list[UsageEvent]) -> None:
    global _SESSION_NOTE_SHOWN
    if _SESSION_NOTE_SHOWN or not any(e.session for e in batch):
        return
    _SESSION_NOTE_SHOWN = True
    # shortcut: the webhook carries usage only; upgrade when a machine runs webhook-only (until
    # then `aut backfill --sessions-only` over pg_dsn fills the gap).
    print("aut: note: session rows not sent over the webhook (needs transport.pg_dsn)",
          file=sys.stderr)


def make_sender(cfg: Config) -> Callable[[list[UsageEvent]], None]:
    """Return a send(batch) that tries the webhook, then the PG fallback. Raises (before any
    send) if nothing is configured or a configured webhook is missing its secret."""
    if not cfg.has_webhook and not cfg.has_pg:
        raise SystemExit(
            "no transport configured: set [transport].webhook_url (and/or pg_dsn) in your "
            "config, or run with --dry-run to preview without sending."
        )
    if cfg.has_webhook and not cfg.webhook_secret:
        raise SystemExit(
            "transport.webhook_url is set but webhook_secret is missing or unresolved; "
            "refusing to send unauthenticated. Set webhook_secret (or its env: value)."
        )
    if cfg.has_webhook:
        parsed = urlparse(cfg.webhook_url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        local = host in ("localhost", "127.0.0.1", "::1")
        # Allowlist, not blocklist: only https, or http to localhost for testing. Anything else
        # (http to a remote host, or ftp:/file:/no-scheme) could leak the secret or misfire.
        if not (scheme == "https" or (scheme == "http" and local)):
            raise SystemExit(
                f"refusing to send the webhook secret to '{cfg.webhook_url}': only https:// "
                f"(or http:// to localhost for testing) is allowed, not '{scheme or 'no'}-scheme'."
            )

    def send(batch: list[UsageEvent]) -> None:
        if not batch:
            return
        webhook_err: Exception | None = None
        if cfg.has_webhook:
            try:
                _post_webhook(cfg.webhook_url, cfg.webhook_secret, batch, cfg.cf_access_headers)
                _note_sessions_skipped(batch)
                return
            except (urllib.error.URLError, RuntimeError, OSError) as exc:
                webhook_err = exc
        if cfg.has_pg:
            _insert_pg(cfg.pg_dsn, batch)
            return
        raise RuntimeError(f"webhook delivery failed and no PG fallback: {webhook_err}")

    return send


def scrub_error(exc: BaseException) -> str:
    """The error stored in a scan row: the exception TYPE only. Messages can carry a file path
    (which names a project) or part of a credential, and no redaction pattern catches every
    form, so the full message stays on this machine's stderr and never reaches the sink."""
    return type(exc).__name__[:_ERROR_MAX]


def send_scan_rows(cfg: Config, rows: list[dict]) -> None:
    """Deliver scan heartbeat rows over the PG DSN. Idempotent on (machine, collector,
    finished_at). Raises on failure; the caller decides that a heartbeat never blocks a scan."""
    if not rows:
        return
    if not cfg.has_pg:
        # shortcut: webhook path for scan rows not built; upgrade when a machine runs webhook-only.
        print("aut: note: scan heartbeat not sent (needs transport.pg_dsn)", file=sys.stderr)
        return
    import psycopg  # optional dependency; install with the [postgres] extra

    cols = ", ".join(SCAN_COLUMNS)
    placeholders = ", ".join(["%s"] * len(SCAN_COLUMNS))
    sql = (
        f"INSERT INTO {SCAN_TABLE} ({cols}) VALUES ({placeholders}) "
        "ON CONFLICT (machine, collector, finished_at) DO NOTHING"
    )
    # 5 s, not 15: usage is already delivered, so an unreachable sink should not hold the exit.
    with psycopg.connect(cfg.pg_dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, [tuple(r[c] for c in SCAN_COLUMNS) for r in rows])
        conn.commit()
