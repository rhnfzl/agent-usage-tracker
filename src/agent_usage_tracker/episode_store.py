"""The episode rows the local pages read and the corrections they write."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TypedDict

from .sink import connect, write_many


class EpisodeMember(TypedDict):
    session_id: str
    role: str                 # seed, or a non-seed member
    repo_keys: list[str]


class Episode(TypedDict):
    """One agents.episode row and its members, timestamps in local time."""
    id: str
    first_ts: datetime
    last_ts: datetime
    n_sessions: int
    n_seed_sessions: int
    n_commits: int
    n_prs: int
    outcome: str
    n_steers: int
    outcome_source: str
    confirmed: bool
    members: list[EpisodeMember]


def load_month(dsn: str, start: datetime, end: datetime) -> list[Episode]:
    with connect(dsn) as conn:
        return load_episodes(conn, "e.first_ts >= %s AND e.first_ts < %s", (start, end))


def load_since(dsn: str, since: datetime) -> list[Episode]:
    """The episodes whose last activity is at or after `since`."""
    with connect(dsn) as conn:
        return load_episodes(conn, "e.last_ts >= %s", (since,))


def load_episodes(conn: Any, where: str, params: tuple[Any, ...]) -> list[Episode]:
    """Episodes matching `where` (over agents.episode e), each with its members."""
    rows = conn.execute(
        "SELECT e.episode_id::text, e.first_ts, e.last_ts, e.n_sessions, e.n_seed_sessions, "
        "e.n_commits, e.n_prs, e.outcome, m.session_id, m.role, m.repo_keys, e.n_steers, "
        "e.outcome_source, e.confirmed "
        "FROM agents.episode e JOIN agents.episode_member m USING (episode_id) "
        f"WHERE {where} "  # nosec B608 # every caller passes constant SQL with %s params
        "ORDER BY e.first_ts, e.episode_id, m.role DESC, m.session_id", params).fetchall()
    eps: dict[str, Episode] = {}
    for eid, f, last, ns, nseed, nc, npr, out, sid, role, keys, nst, src, conf in rows:
        ep = eps.setdefault(eid, {"id": eid, "first_ts": f.astimezone(), "last_ts":
                                  last.astimezone(), "n_sessions": ns, "n_seed_sessions": nseed,
                                  "n_commits": nc, "n_prs": npr, "outcome": out, "n_steers": nst,
                                  "outcome_source": src, "confirmed": conf, "members": []})
        ep["members"].append({"session_id": sid, "role": role, "repo_keys": list(keys or [])})
    return list(eps.values())


def answered_sessions(dsn: str, sessions: list[str]) -> set[str]:
    """Seed sessions that already have a confirm or reject correction (the episodes to hide)."""
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT DISTINCT a_session FROM agents.episode_correction "
            "WHERE action IN ('confirm', 'reject') AND a_session = ANY(%s)",
            (sessions,)).fetchall()
    return {r[0] for r in rows}


def insert_corrections(dsn: str, rows: list[dict[str, Any]]) -> None:
    write_many(dsn, "INSERT INTO agents.episode_correction (id, ts, action, a_session, b_session, "
               "attrs) VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
               [(r["id"], r["ts"], r["action"], r["a_session"], r["b_session"],
                 json.dumps(r["attrs"], sort_keys=True)) for r in rows])
