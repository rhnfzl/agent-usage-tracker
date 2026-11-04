"""`aut inspect --episode ID | --day YYYY-MM-DD | --label UNIT_ID`: the evidence inspector.

Shows why an episode (or each episode of a local day, or the episode a label belongs to) exists:
its member sessions, the anchors by type that the linker joins on, the outcome rule that fired,
labels with probability and model, and hand corrections. Everything is read from the sink, ids
and keys only; titles and branch names come from THIS machine's logs (session_digest) and
exist only in the page served from 127.0.0.1 under a random token. The page is read-only and
links to the existing correction form of `aut episodes review` for the episodes it shows.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import episode_store, local_page, review
from .collectors.session_digest import local_info
from .config import AutError, Config
from .sink import connect

# (shown name, link row attrs): a commit key is a printed commit (commit_keys) or a quoted SHA
# (sha_refs), both HMACs of the first 7 hex, so they meet in one type
ANCHORS = (("commit key", ("commit_keys", "sha_refs")), ("pr key", ("pr_keys",)),
           ("branch key", ("branch_key",)), ("ticket key (a hint, never an edge)", ("ticket_keys",)))
RULES = {   # agents.episode_link(): the latest manual outcome wins, else git in this order
    "manual": "manual outcome: the latest `aut outcome` on a seed session wins over git",
    "reworked": "a commit was reverted or reworked",
    "shipped": "a commit merged (survival at least 0.5 where known), none reverted or reworked",
    "open": "no commit merged or reworked, and a commit is still open",
    "ruled_out": "every commit was abandoned",
    "undecided": "a commit still waits for an outcome (`aut outcomes`)",
    "no_code": "no commits: research or review work",
}
_SHA = re.compile(r"[0-9a-f]{7,40}")
_EP_PREFIX = re.compile(r"[0-9a-f-]{4,36}")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SHOW = 25   # rows per list on the page; a giant episode would otherwise render megabytes
_CSS = """section { border: 1px solid var(--line); border-radius: 6px; margin: 0 0 1rem;
  padding: 0 1rem; overflow-x: auto; }
table { border-collapse: collapse; font-size: 14px; }
td, th { border-bottom: 1px solid var(--line); padding: .2rem .5rem; text-align: left;
  vertical-align: top; }
.meta { color: var(--mut); font-size: 13px; } code { font-size: 13px; }
"""


def _day_bounds(day: str) -> tuple[datetime, datetime]:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise AutError("inspect: --day is YYYY-MM-DD") from None
    return d.astimezone(), (d + timedelta(days=1)).astimezone()   # local midnights, DST-safe


def _episode_prefix(episode: str) -> str:
    """An episode id or prefix: uuid characters only, so no LIKE wildcard gets through."""
    ep = episode.strip().lower()
    if not _EP_PREFIX.fullmatch(ep):
        raise AutError("inspect: --episode is an episode id or prefix (4 or more of 0-9a-f-)")
    return ep


def _check_one_selector(episode: str | None, day: str | None, label: str | None) -> None:
    if sum(x is not None for x in (episode, day, label)) != 1:
        raise ValueError("inspect: exactly one of episode, day and label")


def fetch(dsn: str, episode: str | None = None, day: str | None = None,
          label: str | None = None) -> dict:
    """Raw rows for one view. Read-only; every SELECT is ids, keys, enums and numbers."""
    _check_one_selector(episode, day, label)
    start = end = _EPOCH
    if episode is not None:
        where, params = "e.episode_id::text LIKE %s", (_episode_prefix(episode) + "%",)
    elif day is not None:
        start, end = _day_bounds(day)
        # a member row that day, as agents.episode_activity_day counts it (not span overlap)
        where = ("EXISTS (SELECT 1 FROM agents.episode_member em JOIN agents.activity_event a "
                 "ON a.session_id = em.session_id WHERE em.episode_id = e.episode_id AND a.kind "
                 "IN ('steer', 'link', 'commit', 'pr') AND a.ts >= %s AND a.ts < %s "
                 "AND a.ts BETWEEN e.first_ts AND e.last_ts)")
        params = (start, end)
    else:   # the label's own episode (episode, episode_day) or its steer's session's episodes
        where = ("e.episode_id::text = %s OR e.episode_id IN (SELECT m.episode_id FROM "
                 "agents.episode_member m JOIN agents.activity_event a ON a.session_id = "
                 "m.session_id WHERE a.kind = 'steer' AND a.event_id = %s)")
        params = (label.split(":")[0], label)
    with connect(dsn) as conn:
        def q(sql, p):
            return conn.execute(sql, p).fetchall()
        eps = episode_store.load_episodes(conn, where, params)
        sids = sorted({m["session_id"] for ep in eps for m in ep["members"]})
        acts = q("SELECT session_id, kind, attrs FROM agents.activity_event "
                 "WHERE session_id = ANY(%s) AND kind IN ('link', 'commit')", (sids,))
        sess = q("SELECT session_id, min(agent), count(*) FILTER (WHERE kind = 'steer'), "
                 "min(ts), max(ts) FROM agents.activity_event WHERE session_id = ANY(%s) "
                 "GROUP BY 1", (sids,))
        steers = q("SELECT event_id, session_id FROM agents.activity_event "
                   "WHERE kind = 'steer' AND session_id = ANY(%s)", (sids,))
        shas = sorted({str((a or {}).get("sha") or "").lower()[:7] for _s, k, a in acts
                       if k == "commit"})
        outcomes = q("SELECT DISTINCT ON (left(lower(sha), 7)) left(lower(sha), 7), state, "
                     "survival FROM agents.commit_outcome WHERE left(lower(sha), 7) = ANY(%s) "
                     "ORDER BY left(lower(sha), 7), decided_at DESC", (shas,))
        manual = q("SELECT session_id, outcome, ts FROM agents.manual_outcome "
                   "WHERE session_id = ANY(%s) ORDER BY ts DESC", (sids,))
        corr = q("SELECT ts, action, a_session, b_session FROM agents.episode_correction "
                 "WHERE a_session = ANY(%s) OR b_session = ANY(%s) ORDER BY ts", (sids, sids))
        eids = [ep["id"] for ep in eps]
        labels = q("SELECT event_id, ts, attrs FROM agents.activity_event WHERE kind = 'label' "
                   "AND (attrs->>'unit_id' = ANY(%s) OR split_part(attrs->>'unit_id', ':', 1) "
                   "= ANY(%s) OR attrs->>'unit_id' = %s OR (ts >= %s AND ts < %s AND "
                   "attrs->>'unit' IN ('aw_block', 'aw_title')))",
                   ([s for s, _ in steers] + eids, eids, label, start, end))
    return {"episodes": eps, "acts": acts, "sessions": sess, "steers": steers,
            "outcomes": outcomes, "manual": manual, "corrections": corr, "labels": labels}


def latest_labels(rows: list) -> list[dict]:
    """One current label per (task, unit, unit_id), picked as agents.label_latest picks it:
    newest task_ver, then newest labelled_at (a row without one loses), then event_id."""
    best: dict[tuple, tuple] = {}
    for eid, ts, a in rows:
        a = a or {}
        key = (a.get("task"), a.get("unit"), a.get("unit_id"))
        rank = (int(a.get("task_ver") or 0), a.get("labelled_at") or "", eid)
        if key not in best or rank > best[key][0]:
            below = a.get("below_tau")
            best[key] = (rank, {"task": key[0], "unit": key[1], "unit_id": key[2], "ts": ts,
                                "label": a.get("label"), "p": a.get("p"),
                                "below_tau": True if below is None else bool(below),
                                "model_id": a.get("model_id"), "task_ver": rank[0]})
    return sorted((v for _r, v in best.values()), key=lambda d: (d["ts"], d["task"] or ""))


def build(raw: dict) -> dict:
    """Assemble each episode's evidence from fetch()'s rows. Pure: tests feed it rows."""
    sess = {s: {"agent": ag, "steers": n, "first": f, "last": la}
            for s, ag, n, f, la in raw["sessions"]}
    steer_sid = dict(raw["steers"])
    states = {k: (st, sv) for k, st, sv in raw["outcomes"]}
    labels = latest_labels(raw["labels"])
    used: set[int] = set()
    out = []
    for ep in raw["episodes"]:
        mem = {m["session_id"]: m for m in ep["members"]}
        seeds = {s for s, m in mem.items() if m["role"] == "seed"}
        anchors: dict[str, dict[str, set]] = {name: {} for name, _f in ANCHORS}
        commits: dict[str, set] = {}
        for sid, kind, a in raw["acts"]:
            a = a or {}
            keys = {k for k in mem.get(sid, {}).get("repo_keys", []) if k}
            if sid not in mem or (keys and a.get("repo_key") and a["repo_key"] not in keys):
                continue    # a seed session's unit in another repo belongs to another episode
            if kind == "commit":
                sha = str(a.get("sha") or "").lower()
                if _SHA.fullmatch(sha):
                    commits.setdefault(sha[:7], set()).add(sid)
            elif sid in seeds:  # children attach to their parent and never join
                for name, fields in ANCHORS:
                    for f in fields:
                        v = a.get(f)
                        for x in v if isinstance(v, list) else [v] if v else []:
                            anchors[name].setdefault(str(x), set()).add(sid)
        man = [(s, o, ts) for s, o, ts in raw["manual"] if s in seeds]
        rule = RULES["manual"] if ep["outcome_source"] == "manual" else \
            RULES.get(ep["outcome"], ep["outcome"])
        if ep["outcome_source"] == "manual" and man:
            rule += f" ({man[0][1]}, {man[0][2].astimezone():%Y-%m-%d %H:%M})"
        own, steer_mix = [], Counter()
        for i, lab in enumerate(labels):
            uid, unit = lab["unit_id"] or "", lab["unit"]
            if (unit == "episode" and uid == ep["id"]) or \
                    (unit == "episode_day" and uid.split(":")[0] == ep["id"]):
                own.append(lab)
            elif unit == "steer" and steer_sid.get(uid) in mem:
                steer_mix[(lab["task"], "unknown" if lab["below_tau"] else lab["label"])] += 1
            else:
                continue
            used.add(i)
        out.append({**ep, "rule": rule, "anchors": anchors, "labels": own,
                    "steer_labels": sorted(steer_mix.items()),
                    "commits": [(k, *states.get(k, ("no outcome yet", None)), sorted(v))
                                for k, v in sorted(commits.items())],
                    "sessions": [{**m, **sess.get(m["session_id"], {})} for m in ep["members"]],
                    "corrections": [c for c in raw["corrections"]
                                    if c[2] in mem or (c[3] and c[3] in mem)]})
    rest = [lab for i, lab in enumerate(labels) if i not in used]
    return {"episodes": out, "labels": rest, "history": raw["labels"]}


def _short(x) -> str:
    return str(x)[:12]


def render(view: dict, info: dict, token: str, title: str, label: str | None = None) -> str:
    e = html.escape
    parts = [local_page.head("Evidence inspector", "64rem", _CSS) + f"""<h1>{e(title)}</h1>
<p>Private page on this machine only; read-only. Titles and branch names come from this
machine's logs and are never sent.</p>"""]
    if view["episodes"]:
        parts.append(f'<p><a href="/{e(token)}/correct">Correct these episodes</a> (the '
                     "<code>aut episodes review</code> form)</p>")

    def table(head: list[str], rows: list[list]) -> None:
        parts.append("<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr>" +
                     "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>"
                             for r in rows) + "</table>")

    def labels_table(labs: list[dict]) -> None:
        table(["task", "unit", "unit id", "label", "p", "model", "version"],
              [[x["task"], x["unit"], x["unit_id"],
                "unknown (below tau: " + str(x["label"]) + ")" if x["below_tau"] else x["label"],
                "" if x["p"] is None else f"{float(x['p']):.2f}", x["model_id"], x["task_ver"]]
               for x in labs])

    def more(n: int) -> None:
        if n > _SHOW:
            parts.append(f"<p class=meta>First {_SHOW} of {n}.</p>")

    for ep in view["episodes"]:
        parts.append(f"<section><h2>Episode <code>{e(ep['id'])}</code></h2><p class=meta>"
                     f"{ep['first_ts']:%Y-%m-%d %H:%M} to {ep['last_ts']:%Y-%m-%d %H:%M}; "
                     f"{ep['n_sessions']} sessions ({ep['n_seed_sessions']} seed), "
                     f"{ep['n_steers']} steers, {ep['n_commits']} commits, {ep['n_prs']} PRs; "
                     f"{'confirmed by hand' if ep['confirmed'] else 'not confirmed'}</p>"
                     f"<h3>Outcome: {e(ep['outcome'])}</h3><p>Rule that fired: "
                     f"{e(ep['rule'])}</p>")
        if ep["commits"]:
            table(["commit", "state", "survival", "sessions"],
                  [[k, st, "" if sv is None else f"{float(sv):.2f}",
                    ", ".join(_short(s) for s in ss)] for k, st, sv, ss in ep["commits"][:_SHOW]])
            more(len(ep["commits"]))
        parts.append("<h3>Member sessions</h3>")
        rows = []
        for s in ep["sessions"][:4 * _SHOW]:     # seeds first (load_episodes' order)
            d = info.get(s["session_id"], {})
            span = f"{s['first']:%m-%d %H:%M} to {s['last']:%m-%d %H:%M}" if s.get("first") \
                else ""
            rows.append([s["role"], s.get("agent", ""), _short(s["session_id"]),
                         s.get("steers", 0), span,
                         ", ".join(_short(k) for k in s["repo_keys"] if k),
                         "; ".join(d.get("titles", [])[-3:]) or "(no local title)",
                         ", ".join(d.get("branches", [])[:5])])
        table(["role", "agent", "session", "steers", "active", "repo keys", "local titles",
               "local branches"], rows)
        if len(ep["sessions"]) > 4 * _SHOW:
            parts.append(f"<p class=meta>First {4 * _SHOW} of {len(ep['sessions'])}.</p>")
        parts.append("<h3>Anchors by type</h3><p class=meta>Seed sessions only; a key held by "
                     "two or more sessions is what joined them.</p><ul>")
        for name, vals in ep["anchors"].items():
            shared = {v: ss for v, ss in vals.items() if len(ss) > 1}
            top = sorted(shared.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:_SHOW]
            parts.append(f"<li>{e(name)}: {len(vals)} keys, {len(shared)} shared" + "".join(
                f"<br><code>{e(_short(v))}</code> in " + ", ".join(e(_short(s)) for s in sorted(ss))
                for v, ss in top) + "</li>")
        parts.append("</ul><h3>Labels</h3>")
        if ep["labels"]:
            labels_table(ep["labels"])
        if ep["steer_labels"]:
            parts.append("<p>Steer labels: " + e(", ".join(
                f"{t} {lab} {n}" for (t, lab), n in ep["steer_labels"])) + "</p>")
        if not ep["labels"] and not ep["steer_labels"]:
            parts.append("<p class=meta>No labels yet.</p>")
        parts.append("<h3>Corrections</h3>")
        if ep["corrections"]:
            table(["when", "action", "session", "other session"],
                  [[f"{ts.astimezone():%Y-%m-%d %H:%M}", act, _short(a), _short(b or "")]
                   for ts, act, a, b in ep["corrections"]])
        else:
            parts.append("<p class=meta>None.</p>")
        parts.append("</section>")
    if view["labels"]:
        parts.append("<section><h2>Labels without an episode here</h2>")
        labels_table(view["labels"])
        parts.append("</section>")
    if label:
        hist = sorted((r for r in view["history"] if (r[2] or {}).get("unit_id") == label),
                      key=lambda r: r[1])
        parts.append(f"<section><h2>Every label row of <code>{e(label)}</code></h2>")
        table(["when", "task", "version", "label", "p", "below tau", "model", "labelled at"],
              [[f"{ts.astimezone():%Y-%m-%d %H:%M}", a.get("task"), a.get("task_ver"),
                a.get("label"), a.get("p"), a.get("below_tau"), a.get("model_id"),
                a.get("labelled_at") or ""] for _eid, ts, a in hist])
        parts.append("</section>")
    parts.append("</body></html>")
    return "".join(parts)


def pages(view: dict, info: dict, title: str, label: str | None = None) -> Callable[[str], dict]:
    """The evidence at /<token> and the correction form for its episodes at /<token>/correct."""
    return lambda token: {"": render(view, info, token, title, label),
                          "/correct": review.render(view["episodes"], info, token, title.lower())}


def run_inspect(cfg: Config, episode: str | None = None, day: str | None = None,
                label: str | None = None, fetch_fn=fetch, serve_fn=review.serve) -> dict:
    _check_one_selector(episode, day, label)
    dsn = cfg.require_pg("inspect")
    if episode is not None:
        episode = _episode_prefix(episode)
    title = f"Episode {episode}" if episode is not None else \
        f"Day {day}" if day is not None else f"Label {label}"
    view = build(fetch_fn(dsn, episode=episode, day=day, label=label))
    if not view["episodes"] and not view["labels"]:
        raise AutError(f"inspect: nothing found for {title.lower()}")
    info = local_info(cfg.home, {s["session_id"] for ep in view["episodes"]
                                        for s in ep["sessions"][:4 * _SHOW]
                                        if s["role"] == "seed"})   # children have no title
    return serve_fn(view["episodes"], lambda rows: episode_store.insert_corrections(dsn, rows),
                    pages(view, info, title, label))


def print_inspect(summary: dict) -> None:
    if summary["reviewed"]:
        print(f"inspect: {summary['reviewed']} corrections saved")
