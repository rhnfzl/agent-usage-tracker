"""The episode rows the local pages read and the corrections they write."""

from __future__ import annotations

import json
from datetime import datetime

from .sink import connect, write_many


def load_month(dsn: str, start: datetime, end: datetime) -> list[dict]:
    with connect(dsn) as conn:
        return load_episodes(conn, "e.first_ts >= %s AND e.first_ts < %s", (start, end))


def load_episodes(conn, where: str, params: tuple) -> list[dict]:
    """Episodes matching `where` (over agents.episode e), each with its members."""
    rows = conn.execute(
        "SELECT e.episode_id::text, e.first_ts, e.last_ts, e.n_sessions, e.n_seed_sessions, "
        "e.n_commits, e.n_prs, e.outcome, m.session_id, m.role, m.repo_keys, e.n_steers, "
        "e.outcome_source, e.confirmed "
        "FROM agents.episode e JOIN agents.episode_member m USING (episode_id) "
        f"WHERE {where} "
        "ORDER BY e.first_ts, e.episode_id, m.role DESC, m.session_id", params).fetchall()
    eps: dict[str, dict] = {}
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


def insert_corrections(dsn: str, rows: list[dict]) -> None:
    write_many(dsn, "INSERT INTO agents.episode_correction (id, ts, action, a_session, b_session, "
               "attrs) VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
               [(r["id"], r["ts"], r["action"], r["a_session"], r["b_session"],
                 json.dumps(r["attrs"], sort_keys=True)) for r in rows])
