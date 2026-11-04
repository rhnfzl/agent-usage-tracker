"""The label tasks: closed candidate lists, questions, and the units each one labels.

Units come from the sink's non-text rows (steers, turns, episodes: ids, times, counts) joined with
this machine's own text (Claude Code prompts, ActivityWatch titles), redacted on read. A unit
whose text is not on this machine is skipped: model labels cover this machine's work only.
Changing a candidate or a definition bumps that task's `ver`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterator

from ..collectors import activitywatch as aw, drain
from ..collectors import claude_code
from ..config import AutError, Config
from ..event import CollectorUnavailable, TimeInterval, make_event_id
from ..redact import redact
from ..repo_keys import RepoKeys
from ..sink import connect
from .core import Request, local_midnight

TEXT_MAX = 1500              # chars of one steer text a prompt carries
AW_BLOCK_MIN_S = 300
AW_GAP_S = 60                # windows of one category this close form one block
OUTCOME_DAYS = 14


@dataclass(frozen=True, slots=True)
class Task:
    name: str
    ver: int
    question: str
    decision: str                                   # choice | boolean
    candidates: tuple[tuple[str, str], ...]         # (id, definition)
    none_fits: str | None
    window: str                                     # steer: since a ts | day: whole past days
    #                                                 | recent: the last OUTCOME_DAYS, no watermark
    cached: bool = False                            # skip a unit_id already labelled (aw titles)

    def request(self, unit: str, unit_id: str, ts: datetime, state: dict,
                rev: str | None = None) -> Request:
        return Request(task=self.name, task_ver=self.ver, unit=unit, unit_id=unit_id,
                       ts=ts.astimezone(timezone.utc).isoformat(), state=state,
                       question=self.question, decision=self.decision,
                       candidates=[{"id": i, "desc": d} for i, d in self.candidates],
                       none_fits=self.none_fits, rev=rev)


_OTHER = ("other", "none of the above fits")
TASKS: dict[str, Task] = {t.name: t for t in (
    Task("work_category", 1, "Which kind of work was this time mostly?", "choice", (
        ("planning", "deciding what to do: specs, plans ('write the plan for the export step')"),
        ("research", "finding out about an external fact or system to decide something "
                     "('compare these two queue libraries')"),
        ("building", "producing new code or content ('add the CSV export command')"),
        ("debugging", "finding why something is wrong and fixing it "
                      "('the test fails on timezone, find out why')"),
        ("review", "judging work already made, own or others' ('review this PR for edge cases')"),
        ("learning", "studying to understand, no immediate deliverable "
                     "('explain how hypertable chunks work')"),
        ("maintenance", "upkeep: deps, config, docs refresh, cleanup "
                        "('bump the deps and fix lints')"),
        ("collaboration", "communicating with people: meetings, chat replies, tickets for others"),
        _OTHER), "other", "day"),
    Task("steer_intent", 1, "What is this input mainly doing to the agent's work?", "choice", (
        ("framing", "sets up goal and context for new work"),
        ("instruction", "tells the agent the next thing to do inside the current goal"),
        ("clarification", "answers the agent's question or adds missing facts, same direction"),
        ("correction", "says the last output is wrong and how"),
        ("redirection", "drops or changes the current approach or goal"),
        ("review", "asks the agent to check or judge work"),
        ("acceptance", "approves, confirms, or ends happily"),
        ("stop", "halts work without a new direction"),
        _OTHER), "other", "steer"),
    Task("soft_outcome", 1, "Where does this piece of work stand now?", "choice", (
        ("ruled_out", "tried and dropped on purpose"),
        ("blocked", "cannot go on until something outside it changes"),
        ("still_open", "there is positive evidence it will continue"),
        ("resolved", "the question or task was answered or done")), None, "recent"),
    Task("aw_title_relevance", 1, "Is this window part of building, learning or collaboration "
         "work (employer or personal), rather than leisure or private admin?", "boolean", (
        ("relevant", "building, learning or collaboration work, employer or personal"),
        ("not_relevant", "leisure or private admin")), None, "day", cached=True),
)}


class SourceUnavailable(AutError):
    """aw-server could not be read: the task's watermark must not move. A sink read's own error
    (a raw psycopg one) is not this: the sink being down stops the whole run."""


def _sql(dsn: str, sql: str, params: tuple) -> list[tuple]:
    with connect(dsn) as conn:
        return conn.execute(sql, params).fetchall()


class Source:
    """Everything a unit builder reads. Tests replace the sink reads; the text reads are local."""

    def __init__(self, cfg: Config) -> None:
        self.cfg, self.keys = cfg, RepoKeys(cfg.repo_key_salt)

    def steers(self, since: datetime) -> list[dict]:
        """Every steer of each session that has one since `since` (for index and previous)."""
        rows = _sql(self.cfg.require_pg("label"),
                    "SELECT event_id, ts, agent, session_id, attrs FROM agents.activity_event "
                    "WHERE kind = 'steer' AND session_id IN (SELECT session_id FROM "
                    "agents.activity_event WHERE kind = 'steer' AND ts >= %s) ORDER BY ts",
                    (since,))
        return [dict(zip(("event_id", "ts", "agent", "session_id", "attrs"), r)) for r in rows]

    def episodes(self, since: datetime) -> list[dict]:
        rows = _sql(self.cfg.require_pg("label"),
                    "SELECT e.episode_id::text, e.first_ts, e.last_ts, e.n_sessions, e.n_steers, "
                    "e.n_commits, e.outcome, array_agg(m.session_id) FROM agents.episode e "
                    "JOIN agents.episode_member m USING (episode_id) WHERE e.last_ts >= %s "
                    "GROUP BY e.episode_id", (since,))
        return [dict(zip(("episode_id", "first_ts", "last_ts", "n_sessions", "n_steers",
                          "n_commits", "outcome", "sessions"), r)) for r in rows]

    def activity(self, sessions: list[str], since: datetime, until: datetime) -> list[dict]:
        rows = _sql(self.cfg.require_pg("label"),
                    "SELECT session_id, ts, kind, attrs FROM agents.activity_event WHERE kind IN "
                    "('turn', 'interrupt', 'rejection', 'commit') AND session_id = ANY(%s) "
                    "AND ts >= %s AND ts < %s", (sessions, since, until))
        return [dict(zip(("session_id", "ts", "kind", "attrs"), r)) for r in rows]

    def labels(self, since: datetime, until: datetime) -> list[dict]:
        rows = _sql(self.cfg.require_pg("label"), "SELECT attrs FROM agents.activity_event WHERE kind = "
                    "'label' AND ts >= %s AND ts < %s ORDER BY ts", (since, until))
        return [r[0] for r in rows]

    def texts(self, steers: list[dict]) -> dict[str, str]:
        """steer event_id -> its redacted text, from this machine's Claude Code logs.
        shortcut: Claude Code only; Codex and Pi seed theirs from position, so their steers stay
        unlabelled. Upgrade when they carry many steers."""
        want = {s["event_id"] for s in steers if s["agent"] == "claude_code"}
        sessions = {s["session_id"] for s in steers if s["agent"] == "claude_code"}
        return {eid: redact(text)[:TEXT_MAX]
                for eid, text in claude_code.steer_texts(self.cfg.home, sessions, want).items()}

    def windows(self, since: datetime, until: datetime) -> list[dict]:
        """This machine's ActivityWatch window rows in [since, until), through the collector's own
        guards (idle, deny-list, redaction, title settings), one day per query. No rows where
        activitywatch is not enabled; SourceUnavailable when it is but aw-server cannot be read."""
        if aw.NAME not in self.cfg.agents:
            return []
        out: list[TimeInterval] = []
        wm = {"end": since.astimezone(timezone.utc).isoformat()}
        for batches in drain(aw.collect, self.cfg, wm, aw.NAME):
            if datetime.fromisoformat(wm["end"]) >= until:
                break
            try:
                for rows, advance_watermark in batches:
                    out += [r for r in rows if isinstance(r, TimeInterval)]
                    advance_watermark()
            except CollectorUnavailable as exc:
                raise SourceUnavailable(str(exc)) from exc
        res = []
        for t in out:
            s, e = datetime.fromisoformat(t.ts), min(datetime.fromisoformat(t.end_ts), until)
            if s < until and e > s:
                res.append({"start": s, "end": e, "app": t.app or "", "title": t.title,
                            "category": t.category, "rule": t.attrs.get("rule", "")})
        return sorted(res, key=lambda w: w["start"])


def _days(since: datetime, until: datetime) -> Iterator[tuple[date, datetime, datetime]]:
    d, end = since.astimezone().date(), until.astimezone().date()
    while d < end:
        start = local_midnight(d)
        yield d, start, start + timedelta(days=1)
        d += timedelta(days=1)


def _steer_units(task: Task, src: Source, since: datetime, until: datetime | None) -> list:
    rows = src.steers(since)
    text = src.texts(rows)
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)
    out = []
    for rs in by_session.values():
        rs.sort(key=lambda r: r["ts"])
        for i, r in enumerate(rs):
            prev = rs[i - 1] if i else None
            if r["event_id"] not in text or r["ts"] < since or (until and r["ts"] >= until):
                continue
            a = r["attrs"] or {}
            out.append(task.request("steer", r["event_id"], r["ts"], {
                "text": text[r["event_id"]],
                "previous_text": text.get(prev["event_id"], "") if prev else "",
                "index_in_session": i,
                "seconds_since_previous": int((r["ts"] - prev["ts"]).total_seconds())
                if prev else None,
                "via": a.get("via"), "after_interrupt": bool(a.get("after_interrupt"))}))
    return out


def _aw_blocks(task: Task, wins: list[dict], steer_ts: list[datetime]) -> list:
    """Contiguous same-category window time of AW_BLOCK_MIN_S or more with no steer inside.
    shortcut: 'no steer inside' stands in for 'overlaps no steer interval'; upgrade by reading
    agents.steer_interval once the writer may."""
    blocks: list[list[dict]] = []
    for w in wins:
        b = blocks[-1] if blocks else None
        if b and b[-1]["category"] == w["category"] \
                and (w["start"] - b[-1]["end"]).total_seconds() <= AW_GAP_S:
            b.append(w)
        else:
            blocks.append([w])
    out = []
    for b in blocks:
        s, e = b[0]["start"], max(w["end"] for w in b)
        secs = sum((w["end"] - w["start"]).total_seconds() for w in b)
        if secs < AW_BLOCK_MIN_S or any(s <= t < e for t in steer_ts):
            continue
        titles = list(dict.fromkeys(w["title"] for w in b if w["title"]))[:5]
        out.append(task.request("aw_block", make_event_id("aw_block", s.isoformat(),
                                                          e.isoformat()), s, {
            "category": b[0]["category"], "apps": list(dict.fromkeys(w["app"] for w in b)),
            "minutes": round(secs / 60), "titles": titles, "hour": s.astimezone().hour}))
    return out


def _claude_steers(steers: list[dict]) -> list[dict]:
    """Claude Code steers only: an episode unit's facts may reach Sonnet (teach), and Sonnet
    inputs are built only from Claude Code fields."""
    return [r for r in steers if r["agent"] == "claude_code"]


def _category_units(task: Task, src: Source, since: datetime, until: datetime | None) -> list:
    until = until or datetime.now(timezone.utc)
    steers = [r for r in src.steers(since) if r["ts"] < until]
    text = src.texts(steers)
    mine = _claude_steers(steers)
    claude_sessions = {r["session_id"] for r in mine}
    eps = src.episodes(since)
    of_session: dict[str, list[str]] = defaultdict(list)
    for episode in eps:
        for sid in episode["sessions"]:
            of_session[sid].append(episode["episode_id"])
    sids = sorted(claude_sessions & set(of_session))
    acts = src.activity(sids, since, until) if sids else []
    day_of = {d for d, _s, _e in _days(since, until)}
    days: dict[tuple, dict] = {}
    for r in mine:
        d = r["ts"].astimezone().date()
        if d not in day_of:
            continue
        for ep in of_session.get(r["session_id"], ()):
            u = days.setdefault((ep, d), {"steers": [], "sessions": set(), "acts": []})
            u["steers"].append(r)
            u["sessions"].add(r["session_id"])
    for a in acts:
        d = a["ts"].astimezone().date()
        for ep in of_session.get(a["session_id"], ()):
            if (ep, d) in days:
                days[(ep, d)]["acts"].append(a)
    out = []
    for (ep, d), u in days.items():
        st = sorted(u["steers"], key=lambda r: r["ts"])
        said = [text[r["event_id"]] for r in st if r["event_id"] in text]
        if not said:
            continue
        tools: Counter = Counter()
        kinds = Counter(a["kind"] for a in u["acts"])
        for a in u["acts"]:
            if a["kind"] == "turn":
                tools.update((a["attrs"] or {}).get("by_tool") or {})
        out.append(task.request("episode_day", f"{ep}:{d.isoformat()}", st[0]["ts"], {
            "first_steer": said[0], "last_steers": said[1:][-3:], "steers": len(st),
            "sessions": len(u["sessions"]), "tool_calls_by_tool": dict(tools),
            "tool_errors": sum((a["attrs"] or {}).get("tool_errors") or 0 for a in u["acts"]
                               if a["kind"] == "turn"),
            "interrupts": kinds["interrupt"], "rejections": kinds["rejection"],
            "commits": kinds["commit"]}))
    wins = src.windows(since, until)
    out += _aw_blocks(task, wins, [r["ts"] for r in steers])
    return out


def _outcome_units(task: Task, src: Source, since: datetime, until: datetime | None) -> list:
    """Episodes without commits (git outcome no_code) touched in the last OUTCOME_DAYS."""
    now = until or datetime.now(timezone.utc)
    eps = [e for e in src.episodes(since) if e["outcome"] == "no_code"]
    steers = _claude_steers(src.steers(since)) if eps else []
    text = src.texts(steers)
    out = []
    for ep in eps:
        mine = set(ep["sessions"])
        st = [r for r in sorted(steers, key=lambda r: r["ts"]) if r["session_id"] in mine]
        said = [text[r["event_id"]] for r in st if r["event_id"] in text]
        if not said:
            continue
        # ts = the episode start, so the row's day (and its held-out side) never moves; rev =
        # its last activity, so a changed episode is re-asked and an unchanged one is a no-op
        out.append(task.request("episode", ep["episode_id"], ep["first_ts"], {
            "last_steers": said[-5:], "days_idle": (now - ep["last_ts"]).days,
            "sessions": len({r["session_id"] for r in st}), "steers": len(st)},
            rev=ep["last_ts"].astimezone(timezone.utc).isoformat()))
    return out


def _title_units(task: Task, src: Source, since: datetime, until: datetime | None) -> list:
    """One unit per distinct browser (app, title); unit_id is an HMAC under the install salt, so
    it cannot confirm a guessed title. None without a salt: then nothing is labelled."""
    seen: dict[str, dict] = {}
    for w in src.windows(since, until or datetime.now(timezone.utc)):
        if not w["title"] or not w["rule"].startswith("browser_"):
            continue
        key = src.keys.of(f"aw_title|{w['app'].lower()}|{w['title'].strip().lower()}")
        if key is None:
            return []
        u = seen.setdefault(key, {"ts": w["start"], "app": w["app"], "title": w["title"],
                                  "category": w["category"], "s": 0.0})
        u["s"] += (w["end"] - w["start"]).total_seconds()
    return [task.request("aw_title", k, u["ts"], {
        "app": u["app"], "title": u["title"], "title_rule": u["category"],
        "minutes": round(u["s"] / 60, 1)}) for k, u in seen.items()]


UNITS: dict[str, Callable] = {
    "steer_intent": _steer_units, "work_category": _category_units,
    "soft_outcome": _outcome_units, "aw_title_relevance": _title_units,
}


def units(name: str, src: Source, since: datetime, until: datetime | None) -> list[Request]:
    """The task's requests in [since, until), oldest first."""
    return sorted(UNITS[name](TASKS[name], src, since, until), key=lambda r: (r.ts, r.unit_id))
