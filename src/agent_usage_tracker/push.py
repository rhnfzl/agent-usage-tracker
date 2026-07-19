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
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlparse

from .config import Config
from .event import UsageEvent

# Column order shared with the homelab schema (agents.usage_event). Keep in sync.
COLUMNS = (
    "event_id", "ts", "machine", "agent", "model", "provider",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "total_tokens", "est_cost_usd", "project",
)
TABLE = "agents.usage_event"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # do not follow; urllib raises HTTPError for the 3xx instead of silently GETting


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post_webhook(url: str, secret: str, batch: list[UsageEvent]) -> None:
    body = json.dumps({"events": [e.to_dict() for e in batch]}).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-AUT-Secret": secret}
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as exc:            # 3xx (no-follow) / 4xx / 5xx
        raise RuntimeError(f"webhook HTTP {exc.code}") from exc
    if not (200 <= status < 300):
        raise RuntimeError(f"webhook HTTP {status}")


def _insert_pg(dsn: str, batch: list[UsageEvent]) -> None:
    import psycopg  # optional dependency; install with the [postgres] extra

    cols = ", ".join(COLUMNS)
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    sql = (
        f"INSERT INTO {TABLE} ({cols}) VALUES ({placeholders}) "
        "ON CONFLICT (event_id, ts) DO NOTHING"
    )
    rows = [tuple(e.to_dict()[c] for c in COLUMNS) for e in batch]
    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()


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
    if cfg.has_webhook and cfg.webhook_url.startswith("http://"):
        host = (urlparse(cfg.webhook_url).hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise SystemExit(
                f"refusing to send the webhook secret over plaintext http:// to '{host}'; "
                "use https:// (http is allowed only for localhost testing)."
            )

    def send(batch: list[UsageEvent]) -> None:
        if not batch:
            return
        webhook_err: Exception | None = None
        if cfg.has_webhook:
            try:
                _post_webhook(cfg.webhook_url, cfg.webhook_secret, batch)
                return
            except (urllib.error.URLError, RuntimeError, OSError) as exc:
                webhook_err = exc
        if cfg.has_pg:
            _insert_pg(cfg.pg_dsn, batch)
            return
        raise RuntimeError(f"webhook delivery failed and no PG fallback: {webhook_err}")

    return send
