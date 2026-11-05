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

import html
import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import episode_store, local_page
from .episode_store import Episode, EpisodeMember
from .collectors.session_digest import SessionView, local_info
from .config import AutError, Config

_ACTIONS = {"right": "confirm", "reject": "reject"}
SAMPLE = 30
_CSS = """body { overflow-wrap: anywhere; }
fieldset { border: 1px solid var(--line); border-radius: 6px; margin: 0 0 1rem; }
.meta, .none { color: var(--mut); font-size: 13px; }
ul { margin: .3rem 0; padding-left: 1.2rem; } li { margin-bottom: .5rem; }
label { margin-right: 1rem; display: inline-block; }
"""


def _seeds(ep: Episode) -> list[EpisodeMember]:
    return [m for m in ep["members"] if m["role"] == "seed"] or ep["members"]


def sample(episodes: list[Episode], n: int = SAMPLE) -> list[int]:
    """Indices of up to n episodes, taken in turn from each (outcome, size) group so every kind
    shows up; the same month gives the same sample."""
    groups: dict[tuple[str, int], list[int]] = {}
    for i, ep in enumerate(episodes):
        k = ep["n_sessions"] or 0
        groups.setdefault((str(ep["outcome"]), 0 if k <= 1 else 1 if k <= 3 else 2), []).append(i)
    rng = random.Random(0)  # nosec B311 # a repeatable review sample, not security
    queues = [rng.sample(g, len(g)) for _k, g in sorted(groups.items())]
    picked: list[int] = []
    while len(picked) < n and any(queues):
        for q in queues:
            if q and len(picked) < n:
                picked.append(q.pop())
    return sorted(picked)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _summary(ep: Episode, views: list[SessionView]) -> str:
    days = (ep["last_ts"].date() - ep["first_ts"].date()).days + 1
    when = f"on {ep['first_ts']:%a %d %b}" if days == 1 else f"over {days} days"
    folders = Counter(v["folder"] for v in views if v.get("folder"))
    where = f" in {folders.most_common(1)[0][0]}" if folders else ""
    return (f"{_plural(ep['n_sessions'], 'session')} {when}{where}, "
            f"{_plural(ep['n_commits'], 'commit')}, {_plural(ep['n_prs'], 'PR')}")


def _start_and_duration(v: SessionView) -> str:
    start, m = v.get("start"), v.get("minutes")
    if start is None or m is None:      # a session with no local log: both unset
        return ""
    return f"{start:%a %d %b %H:%M}, ran " + (f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m")


def _session_html(v: SessionView, sid: str, i: int) -> str:
    e = html.escape
    head = "; ".join(v.get("titles", [])[-3:]) or v.get("first") or "(nothing in the local logs)"
    meta = [x for x in (v.get("agent"), v.get("folder"), _start_and_duration(v),
                        _plural(v["n_prompts"], "prompt") if v.get("agent") else "",
                        ", ".join(v.get("branches", [])[:3])) if x]
    out = [f"<li><b>{e(head)}</b>"
           f" <span class=meta>{e(' · '.join(meta))}</span>"]
    for label, said in (("First prompt", v.get("first")), ("Last prompt", v.get("last"))):
        if said:
            out.append(f"<div><span class=meta>{label}:</span> {e(said)}</div>")
    for label, items in (("Commits", v.get("commits")), ("PRs", v.get("prs")),
                         ("Files", v.get("files"))):
        if items:
            out.append(f"<div class=meta>{label}: {e('; '.join(items))}</div>")
    out.append(f'<label><input type=checkbox name="split{i}" value="{e(sid)}"> take out</label>'
               "</li>")
    return "".join(out)


_ANSWERS = (("skip", "don't know"), ("right", "right"), ("split", "should split"),
            ("merge", "should merge with #"),
            ("reject", "not building work (admin, personal, a quick question)"))


def render(episodes: list[Episode], info: dict[str, SessionView], token: str, heading: str,
           shown: list[int] | None = None, answered: int = 0, kind: str = "episodes") -> str:
    """The page. `shown`: indices of the episodes on it (default all); form fields keep each
    episode's index in `episodes`, so the sample and the full page post the same answers. `kind`
    names them in the note."""
    e = html.escape
    shown = list(range(len(episodes))) if shown is None else shown
    whole = len(shown) == len(episodes)
    note = (f"All {len(episodes)} {kind}." if whole else
            f"A sample of {len(shown)} of {len(episodes)} {kind}, spread over outcome "
            f'and size. <a href="/{e(token)}?all=1">Show all</a>.')
    if answered:
        note += f" {answered} already answered (not shown)."
    parts = [local_page.head("Episode review", "60rem", _CSS) + f"""<h1>Episodes of {e(heading)}</h1>
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


def _correction(now: datetime, action: str, a: EpisodeMember,
                b: EpisodeMember | None = None) -> dict[str, Any]:
    attrs = {"repo_keys": a["repo_keys"]}
    if b:
        attrs["b_repo_keys"] = b["repo_keys"]
    return {"id": str(uuid.uuid4()), "ts": now, "action": action, "a_session": a["session_id"],
            "b_session": b["session_id"] if b else None, "attrs": attrs}


def _field(form: dict[str, list[str]], name: str) -> str:
    return (form.get(name) or [""])[0]


def _answer_rows(form: dict[str, list[str]], i: int, episodes: list[Episode],
                 now: datetime) -> list[dict[str, Any]]:
    """The rows of episode i's answer."""
    ans = _field(form, f"a{i}")
    seeds = _seeds(episodes[i])
    if ans == "split":
        by_id = {m["session_id"]: m for m in seeds}
        return [_correction(now, "split", by_id[s]) for s in form.get(f"split{i}", [])
                if s in by_id]
    if ans == "merge":
        j = _field(form, f"merge{i}").strip().lstrip("#")
        ok = j.isdecimal() and len(j) < 10 and int(j) < len(episodes) and int(j) != i
        return [_correction(now, "merge", seeds[0], _seeds(episodes[int(j)])[0])] if ok else []
    return [_correction(now, _ACTIONS[ans], seeds[0])] if ans in _ACTIONS else []


def corrections(form: dict[str, list[str]], episodes: list[Episode],
                now: datetime) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """episode_correction rows for the answers, and {reviewed, right, saved}: saved counts the rows,
    since a split writes one per session. Answers that name a
    session or episode the page did not show are dropped."""
    rows: list[dict[str, Any]] = []
    reviewed = right = 0
    for i in range(len(episodes)):
        new = _answer_rows(form, i, episodes, now)
        rows += new
        reviewed += bool(new)
        right += bool(new) and _field(form, f"a{i}") == "right"
    return rows, {"reviewed": reviewed, "right": right, "saved": len(rows)}


def month_pages(episodes: list[Episode], info: dict[str, SessionView], heading: str,
                answered: int = 0) -> Callable[[str], dict[str, str]]:
    """The sample at /<token>, every episode at /<token>?all=1."""
    return lambda token: {
        "": render(episodes, info, token, heading, sample(episodes), answered, "open episodes"),
        "?all=1": render(episodes, info, token, heading, None, answered, "open episodes")}


def serve(episodes: list[Episode], write: Callable[[list[dict[str, Any]]], None],
          pages: Callable[[str], dict[str, str]], *, open_browser: bool = True,
          on_ready: Callable[[str], None] | None = None,
          now: datetime | None = None) -> dict[str, int]:
    """Serve pages(token) (what follows /<token> in the path -> html) until the answers are posted;
    write(rows) stores them. Returns {reviewed, right, saved}."""
    token = local_page.new_token()
    built = {k: v.encode() for k, v in pages(token).items()}
    result = {"reviewed": 0, "right": 0, "saved": 0}

    def on_post(form: dict[str, list[str]]) -> bytes:
        rows, summary = corrections(form, episodes, now or datetime.now(timezone.utc))
        write(rows)
        result.update(summary)
        return (f"<p>Saved. Marked right: {summary['right']} of {summary['reviewed']} reviewed. "
                "You can close this tab.</p>").encode()

    local_page.serve(token, built.get, on_post, "local page at {url} (Ctrl-C to stop without saving)",
                     open_browser=open_browser, on_ready=on_ready)
    return result


def _month_bounds(month: str | None) -> tuple[str, datetime, datetime]:
    """(label, start, end) of a local calendar month; default the last completed one."""
    if month:
        try:
            start = datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise AutError("episodes review: --month is YYYY-MM") from None
    else:
        start = (datetime.now().replace(day=1) - timedelta(days=1)).replace(day=1)
    start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + start.month // 12, month=start.month % 12 + 1)
    return f"{start:%Y-%m}", start.astimezone(), end.astimezone()


def run_episode_review(cfg: Config, month: str | None) -> dict[str, Any]:
    dsn = cfg.require_pg("episodes review")
    label, start, end = _month_bounds(month)
    episodes = episode_store.load_month(dsn, start, end)
    if not episodes:
        raise AutError(f"episodes review: no episodes start in {label}")
    done = episode_store.answered_sessions(dsn, [_seeds(ep)[0]["session_id"] for ep in episodes])
    left = [ep for ep in episodes if _seeds(ep)[0]["session_id"] not in done]
    if not left:
        raise AutError(f"episodes review: all {len(episodes)} episodes of {label} are answered")
    info = local_info(cfg.home, {m["session_id"] for ep in left for m in _seeds(ep)})
    summary = serve(left, lambda rows: episode_store.insert_corrections(dsn, rows),
                    month_pages(left, info, label, len(episodes) - len(left)))
    return {**summary, "month": label, "open": len(left),
            "answered_before": len(episodes) - len(left)}


def print_review(report: dict[str, Any]) -> None:
    share = report["right"] / report["reviewed"] if report["reviewed"] else 0.0
    print(f"episodes review {report['month']}: {report['reviewed']} of {report['open']} episodes "
          f"reviewed ({report['answered_before']} answered before), {report['right']} marked "
          f"right ({share:.0%})")
