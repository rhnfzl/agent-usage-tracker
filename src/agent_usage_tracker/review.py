"""`aut episodes review [--month YYYY-MM]`: hand-check one month's episodes on a local page.

The page lists the month's episodes (agents.episode / episode_member) with each seed session's
titles and branch names read from THIS machine's logs. That text exists only in the page served
from 127.0.0.1 under a random token; nothing but session ids and repo keys is written back, as
agents.episode_correction rows: right -> confirm, should split -> split (per chosen session),
should merge -> merge (with the chosen episode), not an episode -> reject.
"""

from __future__ import annotations

import glob
import html
import json
import re
import secrets
import sys
import threading
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .config import Config

_TITLE = re.compile(r'"(?:aiTitle|customTitle)":\s*"((?:[^"\\]|\\.)*)"')
_BRANCH = re.compile(r'"(?:gitBranch|worktreeBranch)":\s*"((?:[^"\\]|\\.)*)"')
_MAX_BODY = 1 << 20
_ACTIONS = {"right": "confirm", "split": "split", "merge": "merge", "reject": "reject"}


def _uniq(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(x for x in xs if x))


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def local_info(home: Path, sessions: set[str]) -> dict[str, dict]:
    """session id -> {titles, branches} from the Claude Code and Codex logs on this machine.
    shortcut: Claude Code and Codex only; Pi and Cursor sessions show no title. Upgrade when
    their sessions seed many episodes."""
    out: dict[str, dict] = {}
    for f in glob.glob(str(home / ".claude" / "projects" / "**" / "*.jsonl"), recursive=True):
        sid = Path(f).stem
        if sid not in sessions or "subagents" in Path(f).parts:
            continue
        titles, branches = [], []
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                titles += map(_unescape, _TITLE.findall(line))
                branches += map(_unescape, _BRANCH.findall(line))
        d = out.setdefault(sid, {"titles": [], "branches": []})
        d["titles"] = _uniq(d["titles"] + titles)
        d["branches"] = _uniq(d["branches"] + branches)
    names: dict[str, str] = {}
    try:
        with open(home / ".codex" / "session_index.jsonl", encoding="utf-8") as fh:
            for line in fh:
                o = json.loads(line)
                names[o.get("id")] = o.get("thread_name") or ""
    except (OSError, ValueError):
        pass
    for f in glob.glob(str(home / ".codex" / "sessions" / "**" / "rollout-*.jsonl"),
                       recursive=True):
        sid = next((s for s in sessions if f.endswith(f"-{s}.jsonl")), None)
        if not sid:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                meta = json.loads(fh.readline()).get("payload") or {}
        except (OSError, ValueError):
            meta = {}
        git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
        out[sid] = {"titles": _uniq([names.get(sid, "")]), "branches": _uniq([git.get("branch")])}
    return out


def _seeds(ep: dict) -> list[dict]:
    return [m for m in ep["members"] if m["role"] == "seed"] or ep["members"]


def render(episodes: list[dict], info: dict, token: str, month: str) -> str:
    e = html.escape
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Episode review</title><style>
:root {{ color-scheme: light dark; --bg: #fafafa; --fg: #1a1a1a; --mut: #666; --line: #ddd; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161616; --fg: #eee; --mut: #aaa;
  --line: #333; }} }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.45 system-ui, sans-serif;
  max-width: 60rem; margin: 0 auto; padding: 1rem; }}
fieldset {{ border: 1px solid var(--line); border-radius: 6px; margin: 0 0 1rem; }}
.meta, .none {{ color: var(--mut); font-size: 13px; }}
ul {{ margin: .3rem 0; padding-left: 1.2rem; }} label {{ margin-right: 1rem; }}
</style></head><body><h1>Episodes of {e(month)}</h1>
<p>Private page on this machine only. Only your answers (session ids and repo keys) are sent.</p>
<form method="post" action="/{e(token)}">"""]
    for i, ep in enumerate(episodes):
        parts.append(f"<fieldset><legend>#{i} {ep['first_ts']:%Y-%m-%d} to "
                     f"{ep['last_ts']:%Y-%m-%d}, {ep['outcome']}</legend>"
                     f"<div class=meta>{ep['n_sessions']} sessions, {ep['n_commits']} commits, "
                     f"{ep['n_prs']} PRs</div><ul>")
        for m in _seeds(ep):
            d = info.get(m["session_id"], {})
            titles = "; ".join(d.get("titles", [])[-3:]) or "(no local title)"
            branches = ", ".join(d.get("branches", [])[:5])
            parts.append(f"<li>{e(titles)}" + (f" <span class=meta>[{e(branches)}]</span>"
                                                if branches else "") +
                         f' <label><input type=checkbox name="split{i}" '
                         f'value="{e(m["session_id"])}"> take out</label></li>')
        kids = len(ep["members"]) - len(_seeds(ep))
        parts.append(f"</ul><div class=none>{kids} child sessions</div>" if kids else "</ul>")
        for v, label in (("right", "right"), ("split", "should split"),
                         ("merge", "should merge with #"), ("reject", "not an episode")):
            parts.append(f'<label><input type=radio name="a{i}" value="{v}"> {label}</label>')
        parts.append(f'<input name="merge{i}" size=3 aria-label="episode number to merge with">'
                     "</fieldset>")
    parts.append("<button type=submit>Save answers</button></form></body></html>")
    return "".join(parts)


def corrections(form: dict[str, list[str]], episodes: list[dict],
                now: datetime) -> tuple[list[dict], dict]:
    """episode_correction rows for the answers, and {reviewed, right}. Answers that name a
    session or episode the page did not show are dropped."""
    rows: list[dict] = []
    reviewed = right = 0

    def row(action, a, b=None):
        attrs = {"repo_keys": a["repo_keys"]}
        if b:
            attrs["b_repo_keys"] = b["repo_keys"]
        return {"id": str(uuid.uuid4()), "ts": now, "action": action, "a_session": a["session_id"],
                "b_session": b["session_id"] if b else None, "attrs": attrs}

    for i, ep in enumerate(episodes):
        ans = (form.get(f"a{i}") or [""])[0]
        seed = _seeds(ep)[0]
        if ans == "split":
            by_id = {m["session_id"]: m for m in _seeds(ep)}
            new = [row("split", by_id[s]) for s in form.get(f"split{i}", []) if s in by_id]
        elif ans == "merge":
            j = (form.get(f"merge{i}") or [""])[0].strip().lstrip("#")
            ok = j.isdigit() and int(j) < len(episodes) and int(j) != i
            new = [row("merge", seed, _seeds(episodes[int(j)])[0])] if ok else []
        elif ans in ("right", "reject"):
            new = [row(_ACTIONS[ans], seed)]
        else:
            new = []
        rows += new
        reviewed += bool(new)
        right += bool(new) and ans == "right"
    return rows, {"reviewed": reviewed, "right": right}


def serve(episodes: list[dict], info: dict, month: str, write, open_browser: bool = True,
          on_ready=None, now: datetime | None = None) -> dict:
    """Serve the page on 127.0.0.1 (random port) until the answers are posted; write(rows)
    stores them. Returns {reviewed, right}."""
    token = secrets.token_urlsafe(16)
    page = render(episodes, info, token, month).encode()
    result: dict = {"reviewed": 0, "right": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):          # keep titles and paths out of the terminal
            pass

        def _reply(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            ok = self.path == f"/{token}"
            self._reply(200 if ok else 404, page if ok else b"not found")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if self.path != f"/{token}" or not 0 < n <= _MAX_BODY:
                self._reply(404, b"not found")
                return
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            rows, summary = corrections(form, episodes, now or datetime.now(timezone.utc))
            write(rows)
            result.update(summary)
            self._reply(200, f"<p>Saved. Marked right: {summary['right']} of "
                             f"{summary['reviewed']} reviewed. You can close this tab.</p>"
                        .encode())
            threading.Thread(target=self.server.shutdown).start()

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/{token}"
    if on_ready:
        on_ready(url)
    if open_browser:
        print(f"aut: review page at {url} (Ctrl-C to stop without saving)", file=sys.stderr)
        webbrowser.open(url)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
    return result


def _month_bounds(month: str | None) -> tuple[str, datetime, datetime]:
    """(label, start, end) of a local calendar month; default the last completed one."""
    if month:
        try:
            start = datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise SystemExit("episodes review: --month is YYYY-MM") from None
    else:
        start = (datetime.now().replace(day=1) - timedelta(days=1)).replace(day=1)
    start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + start.month // 12, month=start.month % 12 + 1)
    return f"{start:%Y-%m}", start.astimezone(), end.astimezone()


def load_month(dsn: str, start: datetime, end: datetime) -> list[dict]:
    import psycopg  # optional dependency; install with the [postgres] extra

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        rows = conn.execute(
            "SELECT e.episode_id::text, e.first_ts, e.last_ts, e.n_sessions, e.n_seed_sessions, "
            "e.n_commits, e.n_prs, e.outcome, m.session_id, m.role, m.repo_keys "
            "FROM agents.episode e JOIN agents.episode_member m USING (episode_id) "
            "WHERE e.first_ts >= %s AND e.first_ts < %s "
            "ORDER BY e.first_ts, e.episode_id, m.role DESC, m.session_id",
            (start, end)).fetchall()
    eps: dict[str, dict] = {}
    for eid, f, last, ns, nseed, nc, npr, out, sid, role, keys in rows:
        ep = eps.setdefault(eid, {"id": eid, "first_ts": f.astimezone(), "last_ts":
                                  last.astimezone(), "n_sessions": ns, "n_seed_sessions": nseed,
                                  "n_commits": nc, "n_prs": npr, "outcome": out, "members": []})
        ep["members"].append({"session_id": sid, "role": role, "repo_keys": list(keys or [])})
    return list(eps.values())


def insert_corrections(dsn: str, rows: list[dict]) -> None:
    if not rows:
        return
    import psycopg  # optional dependency; install with the [postgres] extra

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO agents.episode_correction (id, ts, action, a_session, b_session, "
                "attrs) VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                [(r["id"], r["ts"], r["action"], r["a_session"], r["b_session"],
                  json.dumps(r["attrs"], sort_keys=True)) for r in rows])
        conn.commit()


def run_review(cfg: Config, month: str | None) -> dict:
    if not cfg.has_pg:
        raise SystemExit("episodes review needs transport.pg_dsn")
    label, start, end = _month_bounds(month)
    episodes = load_month(cfg.pg_dsn, start, end)
    if not episodes:
        raise SystemExit(f"episodes review: no episodes start in {label}")
    info = local_info(cfg.home, {m["session_id"] for ep in episodes for m in _seeds(ep)})
    summary = serve(episodes, info, label, lambda rows: insert_corrections(cfg.pg_dsn, rows))
    share = summary["right"] / summary["reviewed"] if summary["reviewed"] else 0.0
    print(f"episodes review {label}: {summary['reviewed']} of {len(episodes)} episodes reviewed, "
          f"{summary['right']} marked right ({share:.0%})")
    return summary
