"""The one way to reach the sink: every Postgres connection goes through connect(). Reads and
feature tables keep their SQL next to their feature; the scan's rows are written by push.py.
"""

from __future__ import annotations

import functools
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@functools.lru_cache(maxsize=None)     # once per DSN per run: a scan connects many times
def _warn_if_plaintext(dsn: str) -> None:
    """libpq's default sslmode=prefer falls back to plaintext without a word. Warn, never refuse:
    a Tailscale or tunnelled sink without TLS works today. An explicit sslmode=disable is the
    opt-out. The warning names the host only: the DSN can hold the password."""
    if "://" in dsn:
        try:
            uri = urlparse(dsn)
        except ValueError:
            return                          # let libpq report a DSN it cannot parse either
        query = parse_qs(uri.query)
        host = query.get("host", [uri.hostname])[0]
        mode = query.get("sslmode", [None])[0]
    else:
        keys = dict(re.findall(r"(\w+)\s*=\s*('[^']*'|\S+)", dsn))
        host = (keys.get("host") or keys.get("hostaddr") or "").strip("'")
        mode = keys.get("sslmode", "").strip("'")
    host = host or os.environ.get("PGHOST") or ""
    mode = mode or os.environ.get("PGSSLMODE") or ""
    if not host or host.startswith("/") or host in LOCAL_HOSTS:
        return                              # a local socket or loopback, e.g. an SSH tunnel
    if mode not in ("disable", "require", "verify-ca", "verify-full"):
        print(f"aut: warning: transport.pg_dsn reaches {host} without requiring TLS, so rows and "
              "the password can travel in plaintext. Add sslmode=require (or sslmode=disable on a "
              "private link such as Tailscale).", file=sys.stderr)


def connect(dsn: str, timeout: int = 15):
    """A Postgres connection, or the operator's error naming the extra that installs the driver."""
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("this needs the Postgres driver, which is not usable here: install "
                         f"agent-usage-tracker[postgres] ({exc})") from None
    _warn_if_plaintext(dsn)
    return psycopg.connect(dsn, connect_timeout=timeout)


def write_many(dsn: str, sql: str, rows: list[tuple], timeout: int = 15) -> None:
    """One statement over many rows in one transaction. Raises on failure."""
    if not rows:
        return
    with connect(dsn, timeout) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
