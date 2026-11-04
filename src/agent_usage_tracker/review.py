"""`aut episodes review [--month YYYY-MM]`: hand-check one month's episodes on a local page.

The page lists the month's episodes (agents.episode / episode_member). Each seed session shows
what THIS machine's logs (Claude Code, Codex, Pi, Cursor CLI) and their git commands say: the
agent, the project folder, when it ran and for how long, how many prompts, the first and last
prompt (redacted, about 200 characters), the commit subjects and PR titles it made, the files it
edited most, its titles and branch names. That text is read when the page is built and exists
only in the page served from 127.0.0.1 under a random token; it is never written to Postgres, a
log, the terminal or a file.

To ask less, the page shows a sample of 30 episodes spread over outcome and size (a link shows
all), and leaves out episodes already confirmed or rejected. Nothing but session ids and repo
keys is written back, as agents.episode_correction rows: right -> confirm, should split -> split
(per chosen session), should merge -> merge (with the chosen episode), not building work ->
reject. "don't know", the default, writes nothing.
"""

from __future__ import annotations

import glob
import html
import json
import os
import random
import re
import secrets
import sys
import threading
import uuid
import webbrowser
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePath
from urllib.parse import parse_qs

from .collectors import claude_code as _cc
from .collectors import codex as _cx
from .collectors import cursor as _cursor
from .collectors import cursor_cli as _cu
from .collectors import pi as _pi
from .collectors.activity_common import (COMMIT_LINE, commit_call, first_record, ms_between,
                                          text_of)
from .collectors._util import epoch_ms_to_iso, safe_int
from .config import Config, load_psycopg
from .redact import clean_text

_TITLE = re.compile(r'"(?:aiTitle|customTitle)":\s*"((?:[^"\\]|\\.)*)"')
_BRANCH = re.compile(r'"(?:gitBranch|worktreeBranch)":\s*"((?:[^"\\]|\\.)*)"')
_MAX_BODY = 1 << 20
_ACTIONS = {"right": "confirm", "split": "split", "merge": "merge", "reject": "reject"}
SAMPLE = 30
_TEXT_MAX = 200
_TAG = re.compile(r"</?[\w:-]+[^<>]{0,200}>")
_WS = re.compile(r"\s+")
_Q = r"""(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+))"""
_GIT_COMMIT = re.compile(r"\bgit\b[^\n;&|]*?\scommit\b")
_MSG = re.compile(rf"\s(?:-m|--message)(?:=|\s+){_Q}")
_PR_TITLE = re.compile(rf"\s(?:-t|--title)(?:=|\s+){_Q}")
# a command up to its first unquoted `&&`, `||`, `;`, `|` or newline
_SEGMENT = re.compile(r"""(?:"(?:[^"\\]|\\.)*"|'[^']*'|&(?!&)|[^"'&;|\n])*""")
# Cursor text with no <user_query>: every tagged block in it is harness context
_BLOCK = re.compile(r"<([\w-]+)[^<>]*>.*?</\1>", re.S)
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "StrReplace", "edit", "write"}


def _uniq(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(x for x in xs if x))


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def clip(text: str, n: int = _TEXT_MAX) -> str:
    """Local text as one short line for the page: context blocks and tags out, secrets redacted,
    then cut to about n characters."""
    t = _WS.sub(" ", _TAG.sub(" ", _cc.CONTEXT_BLOCK_RE.sub("", text))).strip()
    t = clean_text(t)
    return t if len(t) <= n else t[:n - 1].rstrip() + "\u2026"


def _quoted(rx: re.Pattern, cmd: str, at: int) -> str | None:
    """First line of the quoted argument rx finds after `at`; for -m "$(cat <<EOF ...)" the
    heredoc's first line. Only the command segment at `at` is searched."""
    m = rx.search(cmd[:_SEGMENT.match(cmd, at).end()], at)
    if not m:
        return None
    v = next(g for g in m.groups() if g is not None)
    lines = v.split("\n")
    v = lines[1] if v.startswith("$(") and len(lines) > 1 else lines[0]
    return v.strip() or None


def _when(ts: object) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


class _Digest:
    """What one session's local log says, for the page only."""

    def __init__(self, agent: str) -> None:
        self.agent, self.cwd = agent, None
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.prompts: list[str] = []
        self.commits: list[str] = []
        self.prs: list[str] = []
        self.files: Counter = Counter()
        self.titles: list[str] = []
        self.branches: list[str] = []

    def at(self, ts: object, cwd: object = None) -> None:
        d = _when(ts)
        if d:
            self.first = min(self.first or d, d)
            self.last = max(self.last or d, d)
        if isinstance(cwd, str) and cwd and not self.cwd:
            self.cwd = cwd

    def shell(self, cmd: object, out: object, err: bool) -> None:
        """A shell call that succeeded: the commits it made (git's `[branch sha] subject` line,
        else the -m message) and the PR it opened (gh pr create --title)."""
        if not isinstance(cmd, str) or err:
            return
        out = out if isinstance(out, str) else ""
        if commit_call(cmd):
            subj = [m["subj"] for m in COMMIT_LINE.finditer(out)]
            g = _GIT_COMMIT.search(cmd)
            self.commits += subj or [_quoted(_MSG, cmd, g.end()) if g else None]
        i = cmd.find("gh pr create")
        if i >= 0:
            self.prs.append(_quoted(_PR_TITLE, cmd, i))

    def edit(self, name: object, args: object) -> None:
        if name in _EDIT_TOOLS and isinstance(args, dict):
            p = args.get("file_path") or args.get("path") or args.get("notebook_path")
            if isinstance(p, str) and p:
                self.files[p] += 1

    def _file(self, p: str) -> str:
        base = (self.cwd or "").rstrip("/\\")
        if base and p.startswith(base) and p[len(base):len(base) + 1] in ("/", "\\"):
            return p[len(base) + 1:]
        return "/".join(PurePath(p).parts[-2:])

    def view(self) -> dict:
        mins = int((self.last - self.first).total_seconds() // 60) if self.first else None
        return {"agent": self.agent,
                "folder": "/".join(PurePath(self.cwd).parts[-2:]) if self.cwd else "",
                "start": self.first.astimezone() if self.first else None, "minutes": mins,
                "n_prompts": len(self.prompts),
                "first": clip(self.prompts[0]) if self.prompts else "",
                "last": clip(self.prompts[-1]) if len(self.prompts) > 1 else "",
                "commits": [clip(c, 120) for c in _uniq(self.commits)][:5],
                "prs": [clip(t, 120) for t in _uniq(self.prs)][:3],
                "files": [clean_text(self._file(p)) for p, _n in self.files.most_common(3)],
                "titles": [clean_text(t) for t in _uniq(self.titles)],
                "branches": [clean_text(b) for b in _uniq(self.branches)]}


def _claude(f: str, d: _Digest) -> None:
    pend: dict = {}
    seen: set = set()
    loops = 0
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            d.titles += map(_unescape, _TITLE.findall(line))
            d.branches += map(_unescape, _BRANCH.findall(line))
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            d.at(o.get("timestamp"), o.get("cwd"))
            text = _cc.human_text(o)
            if text is not None:
                loops += _cc.is_old_loop(o, text)
                if loops <= 1 or not _cc.is_old_loop(o, text):
                    d.prompts.append(text)
                continue
            m = o.get("message")
            content = m.get("content") if isinstance(m, dict) else None
            for b in content if isinstance(content, list) else ():
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("id") not in seen:
                    seen.add(b.get("id"))       # Claude Code re-logs a message's blocks
                    d.edit(b.get("name"), b.get("input"))
                    if b.get("name") == "Bash" and isinstance(b.get("input"), dict):
                        pend[b.get("id")] = b["input"].get("command")
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pend:
                    d.shell(pend.pop(b["tool_use_id"]), text_of(b.get("content")),
                            b.get("is_error") is True)


def _codex(f: str, d: _Digest) -> None:
    recs = []
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict) and isinstance(o.get("payload"), dict):
                recs.append(o)
    if not recs:
        return
    meta = recs[0]["payload"] if recs[0].get("type") == "session_meta" else {}
    git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
    d.branches.append(git.get("branch"))
    human = _cx.run_origin(meta) not in ("subagent", "automated")
    # a fork opens with a copy of its parent's history (the collector's rule)
    skip = max(safe_int(meta.get("subagent_history_start_ordinal")) - 1, 0) \
        if meta.get("forked_from_id") else 0
    has_um = any(o.get("type") == "event_msg" and o["payload"].get("type") == "user_message"
                 for o in recs)
    for i, o in enumerate(recs):
        if 0 < i <= skip:
            continue
        t, p = o.get("type"), o["payload"]
        d.at(o.get("timestamp"), p.get("cwd") if t in ("session_meta", "turn_context") else None)
        pr = _cx.prompt_of(t, p, has_um)
        if pr is not None:
            if human and not _cx.is_scheduled_automation(pr[0]):
                d.prompts.append(pr[0])
            continue
        it = p.get("item")
        if t != "event_msg" or p.get("type") != "item_completed" or not isinstance(it, dict):
            continue
        if it.get("type") == "CommandExecution":
            code = it.get("exit_code")
            d.shell(_cx.script_of(it.get("command")), it.get("aggregated_output"),
                    isinstance(code, int) and code != 0)
        elif it.get("type") == "FileChange" and isinstance(it.get("changes"), dict):
            d.files.update(k for k in it["changes"] if isinstance(k, str))


def _pi_log(f: str, hdr: dict, d: _Digest) -> None:
    d.at(hdr["ts"], hdr["cwd"])
    pend: dict = {}
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m = o.get("message") if isinstance(o, dict) else None
            if not isinstance(m, dict) or o.get("type") != "message" or (
                    hdr["fork"] and (ms_between(o.get("timestamp"), hdr["ts"]) or 0) > 0):
                continue                # a fork's copy of its parent's entries
            d.at(o.get("timestamp"))
            role, content = m.get("role"), m.get("content")
            if role == "user":
                text = text_of(content)
                sk = _pi.SKILL.match(text)     # a skill invocation: its name, not SKILL.md
                d.prompts.append(f"/{sk[1]} {text.partition('</skill>')[2]}".strip() if sk
                                 else text)
            elif role == "assistant" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        d.edit(b.get("name"), b.get("arguments"))
                        args = b.get("arguments")
                        if b.get("name") == "bash" and isinstance(args, dict):
                            pend[b.get("id")] = args.get("command")
            elif role == "toolResult" and m.get("toolCallId") in pend:
                d.shell(pend.pop(m["toolCallId"]), text_of(content), m.get("isError") is True)


def _cursor_chat(f: str, d: _Digest) -> None:
    con = _cursor.connect_ro(f)
    if con is None:
        return
    try:
        meta = _cu.chat_meta(con) or {}
        msgs = _cu.messages(con, {i for seq in _cu.root_lists(con) for i in seq})
    finally:
        con.close()
    created = epoch_ms_to_iso(meta.get("createdAt"))
    d.at(created)
    prompts: dict = {}
    calls: dict = {}
    for m in msgs.values():
        r = _cu.request_id(m)
        if r and r not in prompts:
            text = text_of(m.get("content"))
            q = _cu.USER_QUERY.search(text)
            ts = _cu.prompt_ts(text, created)
            d.at(ts)
            prompts[r] = (_when(ts) or datetime.min.replace(tzinfo=timezone.utc),
                          q[1] if q else _BLOCK.sub(" ", text))
        for b in m.get("content") if isinstance(m.get("content"), list) else ():
            if isinstance(b, dict) and b.get("type") == "tool-call":
                calls[b.get("toolCallId")] = b.get("args")
                d.edit(b.get("toolName"), b.get("args"))
    d.prompts = [t for _ts, t in sorted(prompts.values(), key=lambda x: x[0])]
    for m in msgs.values():
        content = m.get("content") if m.get("role") == "tool" else None
        for b in content if isinstance(content, list) else ():
            args = calls.get(b.get("toolCallId")) if isinstance(b, dict) else None
            if isinstance(args, dict) and b.get("type") == "tool-result" \
                    and b.get("toolName") == "Shell":
                res = b.get("result") if isinstance(b.get("result"), str) else ""
                ex = _cu.EXIT_LINE.match(res)
                d.at(None, args.get("working_directory"))
                d.shell(args.get("command"), res[ex.end():] if ex else res,
                        not ex or ex[1] != "0")


def local_info(home: Path, sessions: set[str]) -> dict[str, dict]:
    """session id -> what the logs on this machine say about it (see _Digest.view). Best effort:
    a file that cannot be read is skipped without a word, so no local text reaches the terminal.
    shortcut: Cursor IDE chats (state.vscdb) are not read; their sessions show no local text."""
    digests: dict[str, _Digest] = {}

    def read(sid: str, agent: str, fn, *args) -> None:
        d = _Digest(agent)
        try:
            fn(*args, d)
        except Exception:       # noqa: BLE001  (a traceback could print local text)
            return
        digests[sid] = d

    # a main session is <project>/<id>.jsonl; subagents sit deeper (and number tens of thousands)
    for f in glob.glob(str(home / ".claude" / "projects" / "*" / "*.jsonl")):
        if Path(f).stem in sessions:
            read(Path(f).stem, "Claude Code", _claude, f)
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
        if sid:
            read(sid, "Codex", _codex, f)
            if sid in digests:
                digests[sid].titles.insert(0, names.get(sid, ""))
    for f in glob.glob(str(home / ".pi" / "agent" / "sessions" / "**" / "*.jsonl"),
                       recursive=True):
        u = _pi.uuid_in(f)
        if u and u not in sessions:
            continue
        first = first_record(f) or {}
        hdr = _pi.header(first if first.get("type") == "session" else {}, f)
        if hdr["id"] in sessions:
            read(hdr["id"], "Pi", _pi_log, f, hdr)
    # shortcut: a chat is found by its directory name, which is its agentId in every store seen
    for f in glob.glob(str(home / ".cursor" / "chats" / "*" / "*" / "store.db")):
        sid = os.path.basename(os.path.dirname(f))
        if sid in sessions:
            read(sid, "Cursor", _cursor_chat, f)
    return {sid: d.view() for sid, d in digests.items()}


def _seeds(ep: dict) -> list[dict]:
    return [m for m in ep["members"] if m["role"] == "seed"] or ep["members"]


def sample(episodes: list[dict], n: int = SAMPLE) -> list[int]:
    """Indices of up to n episodes, taken in turn from each (outcome, size) group so every kind
    shows up; the same month gives the same sample."""
    groups: dict = {}
    for i, ep in enumerate(episodes):
        k = ep["n_sessions"] or 0
        groups.setdefault((str(ep["outcome"]), 0 if k <= 1 else 1 if k <= 3 else 2), []).append(i)
    rng = random.Random(0)
    queues = [rng.sample(g, len(g)) for _k, g in sorted(groups.items())]
    picked: list[int] = []
    while len(picked) < n and any(queues):
        for q in queues:
            if q and len(picked) < n:
                picked.append(q.pop())
    return sorted(picked)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _summary(ep: dict, views: list[dict]) -> str:
    days = (ep["last_ts"].date() - ep["first_ts"].date()).days + 1
    when = f"on {ep['first_ts']:%a %d %b}" if days == 1 else f"over {days} days"
    folders = Counter(v["folder"] for v in views if v.get("folder"))
    where = f" in {folders.most_common(1)[0][0]}" if folders else ""
    return (f"{_plural(ep['n_sessions'], 'session')} {when}{where}, "
            f"{_plural(ep['n_commits'], 'commit')}, {_plural(ep['n_prs'], 'PR')}")


def _ran(v: dict) -> str:
    if not v.get("start"):
        return ""
    m = v["minutes"] or 0
    return f"{v['start']:%a %d %b %H:%M}, ran " + (f"{m // 60}h {m % 60}m" if m >= 60
                                                     else f"{m}m")


def _session_html(v: dict, sid: str, i: int) -> str:
    e = html.escape
    head = "; ".join(v.get("titles", [])[-3:]) or v.get("first") or "(nothing in the local logs)"
    meta = [x for x in (v.get("agent"), v.get("folder"), _ran(v),
                        _plural(v["n_prompts"], "prompt") if v.get("agent") else "",
                        ", ".join(v.get("branches", [])[:3])) if x]
    out = [f"<li><b>{e(head)}</b>"
           f" <span class=meta>{e(' · '.join(meta))}</span>"]
    for label, key in (("First prompt", "first"), ("Last prompt", "last")):
        if v.get(key):
            out.append(f"<div><span class=meta>{label}:</span> {e(v[key])}</div>")
    for label, key in (("Commits", "commits"), ("PRs", "prs"), ("Files", "files")):
        if v.get(key):
            out.append(f"<div class=meta>{label}: {e('; '.join(v[key]))}</div>")
    out.append(f'<label><input type=checkbox name="split{i}" value="{e(sid)}"> take out</label>'
               "</li>")
    return "".join(out)


_ANSWERS = (("skip", "don't know"), ("right", "right"), ("split", "should split"),
            ("merge", "should merge with #"),
            ("reject", "not building work (admin, personal, a quick question)"))


def render(episodes: list[dict], info: dict, token: str, month: str,
           shown: list[int] | None = None, answered: int = 0) -> str:
    """The page. `shown`: indices of the episodes on it (default all); form fields keep each
    episode's index in `episodes`, so the sample and the full page post the same answers."""
    e = html.escape
    shown = list(range(len(episodes))) if shown is None else shown
    whole = len(shown) == len(episodes)
    note = (f"All {len(episodes)} open episodes." if whole else
            f"A sample of {len(shown)} of {len(episodes)} open episodes, spread over outcome "
            f'and size. <a href="/{e(token)}?all=1">Show all</a>.')
    if answered:
        note += f" {answered} already answered (not shown)."
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Episode review</title><style>
:root {{ color-scheme: light dark; --bg: #fafafa; --fg: #1a1a1a; --mut: #666; --line: #ddd; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161616; --fg: #eee; --mut: #aaa;
  --line: #333; }} }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.45 system-ui, sans-serif;
  max-width: 60rem; margin: 0 auto; padding: 1rem; overflow-wrap: anywhere; }}
fieldset {{ border: 1px solid var(--line); border-radius: 6px; margin: 0 0 1rem; }}
.meta, .none {{ color: var(--mut); font-size: 13px; }}
ul {{ margin: .3rem 0; padding-left: 1.2rem; }} li {{ margin-bottom: .5rem; }}
label {{ margin-right: 1rem; display: inline-block; }}
</style></head><body><h1>Episodes of {e(month)}</h1>
<p>Private page on this machine only. Only your answers (session ids and repo keys) are sent.
{note}</p>
<form method="post" action="/{e(token)}">"""]
    for i in shown:
        ep = episodes[i]
        views = [info.get(m["session_id"], {}) for m in _seeds(ep)]
        parts.append(f"<fieldset><legend>#{i} {ep['first_ts']:%Y-%m-%d} to "
                     f"{ep['last_ts']:%Y-%m-%d}, {e(str(ep['outcome']))}</legend>"
                     f"<div>{e(_summary(ep, views))}</div><ul>")
        parts += [_session_html(v, m["session_id"], i) for m, v in zip(_seeds(ep), views)]
        kids = len(ep["members"]) - len(_seeds(ep))
        parts.append(f"</ul><div class=none>{kids} child sessions</div>" if kids else "</ul>")
        for v, label in _ANSWERS:
            parts.append(f'<label><input type=radio name="a{i}" value="{v}"'
                         f'{" checked" if v == "skip" else ""}> {label}</label>')
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
          on_ready=None, now: datetime | None = None, answered: int = 0, front=None) -> dict:
    """Serve the page on 127.0.0.1 (random port) until the answers are posted; write(rows)
    stores them. /<token> is the sample, /<token>?all=1 every episode. With front(token) -> html
    (`aut inspect`), that page is served at /<token> and the form for every shown episode at
    /<token>/correct. Returns {reviewed, right}."""
    token = secrets.token_urlsafe(16)
    if front:
        pages = {f"/{token}": front(token),
                 f"/{token}/correct": render(episodes, info, token, month, None, answered)}
    else:
        pages = {f"/{token}": render(episodes, info, token, month, sample(episodes), answered),
                 f"/{token}?all=1": render(episodes, info, token, month, None, answered)}
    pages = {k: v.encode() for k, v in pages.items()}
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
            page = pages.get(self.path)
            self._reply(200 if page else 404, page or b"not found")

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
        print(f"aut: local page at {url} (Ctrl-C to stop without saving)", file=sys.stderr)
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
    psycopg = load_psycopg()

    with psycopg.connect(dsn, connect_timeout=15) as conn:
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
    psycopg = load_psycopg()

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        rows = conn.execute(
            "SELECT DISTINCT a_session FROM agents.episode_correction "
            "WHERE action IN ('confirm', 'reject') AND a_session = ANY(%s)",
            (sessions,)).fetchall()
    return {r[0] for r in rows}


def insert_corrections(dsn: str, rows: list[dict]) -> None:
    if not rows:
        return
    psycopg = load_psycopg()

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
    done = answered_sessions(cfg.pg_dsn, [_seeds(ep)[0]["session_id"] for ep in episodes])
    left = [ep for ep in episodes if _seeds(ep)[0]["session_id"] not in done]
    if not left:
        raise SystemExit(f"episodes review: all {len(episodes)} episodes of {label} are answered")
    info = local_info(cfg.home, {m["session_id"] for ep in left for m in _seeds(ep)})
    summary = serve(left, info, label, lambda rows: insert_corrections(cfg.pg_dsn, rows),
                    answered=len(episodes) - len(left))
    share = summary["right"] / summary["reviewed"] if summary["reviewed"] else 0.0
    print(f"episodes review {label}: {summary['reviewed']} of {len(left)} episodes reviewed "
          f"({len(episodes) - len(left)} answered before), {summary['right']} marked right "
          f"({share:.0%})")
    return summary
