"""The label tasks: closed candidate lists, questions, and the units each one labels.

Units come from the sink's non-text rows (steers, turns, episodes: ids, times, counts) joined with
this machine's own text (Claude Code prompts, ActivityWatch titles), redacted on read. A unit
whose text is not on this machine is skipped: model labels cover this machine's work only.
Changing a candidate or a definition bumps that task's `ver`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from ..collectors import activitywatch as aw
from ..collectors import claude_code
from ..config import AutError, Config
from ..episode_store import Episode, load_since
from ..event import CollectorUnavailable, make_event_id
from ..redact import redact
from ..repo_keys import RepoKeys
from ..sink import connect
from .core import Request, local_days
from . import context

TEXT_MAX = 1500              # chars of one steer text a prompt carries
REPLY_MAX = 1500             # chars of one agent reply a prompt carries: its end, where it concludes
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

    def request(self, unit: str, unit_id: str, ts: datetime, facts: dict[str, Any],
                rev: str | None = None) -> Request:
        return Request(task=self.name, task_ver=self.ver, unit=unit, unit_id=unit_id,
                       ts=ts.astimezone(timezone.utc).isoformat(), facts=facts,
                       question=self.question, decision=self.decision,
                       candidates=[{"id": i, "desc": d} for i, d in self.candidates],
                       none_fits=self.none_fits, rev=rev)


_OTHER = ("other", "none of the above fits")
TASKS: dict[str, Task] = {t.name: t for t in (
    Task("work_category", 2, "Which kind of work was this time mostly?", "choice", (
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
    Task("steer_intent", 2, "What is this input mainly doing to the agent's work?", "choice", (
        ("framing", "sets up goal and context for new work"),
        ("instruction", "tells the agent the next thing to do inside the current goal"),
        ("clarification", "answers the agent's question or adds missing facts, same direction"),
        ("correction", "says the last output is wrong and how"),
        ("redirection", "drops or changes the current approach or goal"),
        ("review", "asks the agent to check or judge work"),
        ("acceptance", "approves, confirms, or ends happily"),
        ("stop", "halts work without a new direction"),
        _OTHER), "other", "steer"),
    Task("soft_outcome", 2, "Where does this piece of work stand now?", "choice", (
        ("ruled_out", "tried and dropped on purpose"),
        ("blocked", "cannot go on until something outside it changes"),
        ("still_open", "there is positive evidence it will continue"),
        ("resolved", "the question or task was answered or done")), None, "recent"),
    Task("aw_title_relevance", 1, "Is this window part of building, learning or collaboration "
         "work (employer or personal), rather than leisure or private admin?", "boolean", (
        ("relevant", "building, learning or collaboration work, employer or personal"),
        ("not_relevant", "leisure or private admin")), None, "day", cached=True),
)}


def writing_task() -> Task:
    """Draft version 3 for explicit private evaluation; never registered as a live task."""
    descriptions = {
        "building": "producing executable code, configuration, or a structured software implementation",
        "maintenance": "dependency/configuration upkeep and cleanup, excluding prose authorship",
        "collaboration": "communicating with people: messages, meetings, or tickets for others",
    }
    candidates = []
    for label, description in TASKS["work_category"].candidates:
        candidates.append((label, descriptions.get(label, description)))
        if label == "review":
            candidates.append(("writing", "authoring or revising a prose deliverable, including "
                                "articles, reports, and documentation; not messages for people"))
    return replace(TASKS["work_category"], ver=3, candidates=tuple(candidates))


def bounded_outcome_task() -> Task:
    """Draft version 3 for episode-bounded prompts and an attributable final reply.

    Private evaluation must use a local source with require_outcome_reply enabled. The live
    registry and recipe validation remain version 2 until an explicit contract promotion.
    """
    return replace(TASKS["soft_outcome"], ver=3)


class SourceUnavailable(AutError):
    """aw-server could not be read: the task's watermark must not move. A sink read's own error
    (a raw psycopg one) is not this: the sink being down stops the whole run."""


def _sql(dsn: str, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    with connect(dsn) as conn:
        return conn.execute(sql, params).fetchall()


class Source:
    """Everything a unit builder reads. Tests replace the sink reads; the text reads are local."""

    def __init__(self, cfg: Config, *, local_context: bool = False) -> None:
        self.cfg, self.keys = cfg, RepoKeys(cfg.repo_key_salt)
        # Teacher/export paths retain their Claude-only boundary. Local estimates opt in.
        self.context_agents = context.AGENTS if local_context else frozenset({"claude_code"})
        self.require_outcome_reply = local_context
        self.mature_outcomes = local_context and cfg.label_mature_outcomes
        self.incomplete_outcome_units: list[datetime] = []
        self._context_key: tuple[frozenset[tuple[str, str, str]], datetime | None] | None = None
        self._context_cache: dict[str, context.Context] = {}
        self._path_cache: dict[str, dict[str, list[tuple[int, str]]]] = {}
        self.skipped: Counter[str] = Counter()        # units left out, by reason (the run reports it)
        self.missing_text_units: list[datetime] = []
        self.partial_text_units: list[datetime] = []

    def steers(self, since: datetime) -> list[dict[str, Any]]:
        """Every steer of each session that has one since `since` (for index and previous), in ts
        order: the unit builders rely on it."""
        rows = _sql(self.cfg.require_pg("label"),
                    "SELECT event_id, ts, agent, session_id, attrs FROM agents.activity_event "
                    "WHERE kind = 'steer' AND session_id IN (SELECT session_id FROM "
                    "agents.activity_event WHERE kind = 'steer' AND ts >= %s) ORDER BY ts",
                    (since,))
        return [dict(zip(("event_id", "ts", "agent", "session_id", "attrs"), r)) for r in rows]

    def episodes(self, since: datetime) -> list[Episode]:
        return load_since(self.cfg.require_pg("label"), since)

    def outcome_revisions(self, requests: list[Request]) -> dict[str, str]:
        """Recheck current no-code eligibility before delivery; reopened work must not be sent."""
        if not requests:
            return {}
        since = min(datetime.fromisoformat(r.ts) for r in requests)
        ids = {r.unit_id for r in requests}
        return {ep["id"]: ep["last_ts"].astimezone(timezone.utc).isoformat()
                for ep in self.episodes(since) if ep["id"] in ids and ep["outcome"] == "no_code"}

    def episodes_ready(self) -> datetime | None:
        """Up to when agents.episode holds this machine's data: the start of this machine's last
        Claude Code scan delivered before the last episode rebuild (a collector_scan row with
        collector 'episode_link', written by the sink's nightly job). Without that row, the newest
        episode activity. None on an empty sink."""
        dsn = self.cfg.require_pg("label")
        rows = _sql(dsn, "SELECT max(s.started_at) FROM agents.collector_scan s "
                    "WHERE s.machine = %s AND s.collector = 'claude_code' AND s.status = 'ok' "
                    "AND s.finished_at <= (SELECT max(finished_at) FROM agents.collector_scan "
                    "WHERE collector = 'episode_link' AND status = 'ok')", (self.cfg.machine,))
        if rows and rows[0][0] is not None:
            return rows[0][0]
        rows = _sql(dsn, "SELECT max(last_ts) FROM agents.episode", ())
        return rows[0][0] if rows else None

    def activity(self, sessions: list[str], since: datetime, until: datetime) -> list[dict[str, Any]]:
        rows = _sql(self.cfg.require_pg("label"),
                    "SELECT session_id, ts, kind, attrs FROM agents.activity_event WHERE kind IN "
                    "('turn', 'interrupt', 'rejection', 'commit') AND session_id = ANY(%s) "
                    "AND ts >= %s AND ts < %s", (sessions, since, until))
        return [dict(zip(("session_id", "ts", "kind", "attrs"), r)) for r in rows]

    def labels(self, since: datetime, until: datetime) -> list[tuple[Any, ...]]:
        """(event_id, ts, attrs) of each label row: what core.current_labels reads."""
        return _sql(self.cfg.require_pg("label"), "SELECT event_id, ts, attrs FROM "
                    "agents.activity_event WHERE kind = 'label' AND ts >= %s AND ts < %s ORDER BY ts",
                    (since, until))

    def texts(self, steers: list[dict[str, Any]], until: datetime | None = None) -> dict[str, str]:
        """steer event_id -> redacted local text, restricted to this source's allowed agents."""
        want = {s["event_id"] for s in steers if s["agent"] == "claude_code"}
        sessions = {s["session_id"] for s in steers if s["agent"] == "claude_code"}
        texts = claude_code.steer_texts(self.cfg.home, sessions, want,
                                       paths=self._paths("claude_code", sessions))
        for eid, value in self._other_context(steers, until).items():
            texts[eid] = value.text
        return {eid: redact(text)[:TEXT_MAX] for eid, text in texts.items() if text.strip()}

    def _paths(self, agent: str, sessions: set[str]) -> list[str]:
        if not sessions:
            return []
        if agent not in self._path_cache:
            self._path_cache[agent] = context.session_paths(self.cfg.home, agent)
        index = self._path_cache[agent]
        # Preserve cross-file overwrite and ambiguity semantics from canonical discovery order.
        return [path for order, path in sorted(p for sid in sessions for p in index.get(sid, []))]

    def _other_context(self, steers: list[dict[str, Any]], until: datetime | None,
                       ) -> dict[str, context.Context]:
        out: dict[str, context.Context] = {}
        key = (frozenset((s["agent"], s["session_id"], s["event_id"]) for s in steers
                         if s["agent"] in self.context_agents - {"claude_code"}), until)
        if key == self._context_key:
            return self._context_cache
        for agent in self.context_agents - {"claude_code"}:
            mine = [s for s in steers if s["agent"] == agent]
            if not mine:
                continue
            sessions = {s["session_id"] for s in mine}
            want = {s["event_id"] for s in mine}
            try:
                paths = self._paths(agent, sessions)
                if agent == "cursor_cli":
                    values = context.cursor_context(self.cfg.home, sessions, want, until, self.keys,
                                                    paths=paths)
                elif agent == "codex":
                    values = context.codex_context(self.cfg.home, sessions, want, until, self.keys,
                                                   paths=paths)
                else:
                    values = context.pi_context(self.cfg.home, sessions, want, until, paths=paths)
            except (OSError, CollectorUnavailable) as exc:
                raise SourceUnavailable(f"{agent}: local context unavailable") from exc
            out.update(values)
        self._context_key, self._context_cache = key, out
        return out

    def replies(self, steers: list[dict[str, Any]], until: datetime | None = None,
                ) -> dict[str, tuple[str, str]]:
        """steer event_id -> (agent reply before it, agent reply after it), redacted and cut to
        their last REPLY_MAX chars, from this source's supported local agent logs."""
        want = {s["event_id"] for s in steers if s["agent"] == "claude_code"}
        sessions = {s["session_id"] for s in steers if s["agent"] == "claude_code"}
        replies = claude_code.agent_replies(self.cfg.home, sessions, want, until,
                                           paths=self._paths("claude_code", sessions))
        replies.update({eid: (value.before, value.after)
                        for eid, value in self._other_context(steers, until).items()})
        return {eid: (redact(a)[-REPLY_MAX:], redact(b)[-REPLY_MAX:])
                for eid, (a, b) in replies.items()}

    def windows(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """This machine's ActivityWatch window rows in [since, until), through the collector's own
        guards (idle, deny-list, redaction, title settings), one day per query. No rows where
        activitywatch is not enabled; SourceUnavailable when it is but aw-server cannot be read."""
        if aw.NAME not in self.cfg.agents:
            return []
        try:
            rows = aw.intervals(self.cfg, since, until)
        except CollectorUnavailable as exc:
            raise SourceUnavailable(str(exc)) from exc
        res = []
        for t in rows:
            s, e = datetime.fromisoformat(t.ts), min(datetime.fromisoformat(t.end_ts), until)
            if s < until and e > s:
                res.append({"start": s, "end": e, "app": t.app or "", "title": t.title,
                            "category": t.category, "rule": t.attrs.get("rule", "")})
        return sorted(res, key=lambda w: w["start"])


def _steer_units(task: Task, src: Source, since: datetime, until: datetime | None) -> list[Request]:
    rows = src.steers(since)
    text = src.texts(rows, until)
    reply = src.replies(rows, until)
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)
    out = []
    for rs in by_session.values():
        for i, r in enumerate(rs):
            prev = rs[i - 1] if i else None
            if r["ts"] < since or (until and r["ts"] >= until):
                continue
            if r["event_id"] not in text:
                src.skipped["no_text"] += r["agent"] in src.context_agents
                if r["agent"] in src.context_agents:
                    src.missing_text_units.append(r["ts"])
                continue
            a = r["attrs"] or {}
            out.append(task.request("steer", r["event_id"], r["ts"], {
                "text": text[r["event_id"]],
                "agent_reply_before": reply.get(r["event_id"], ("", ""))[0],
                "previous_text": text.get(prev["event_id"], "") if prev else "",
                "index_in_session": i,
                "seconds_since_previous": int((r["ts"] - prev["ts"]).total_seconds())
                if prev else None,
                "via": a.get("via"), "after_interrupt": bool(a.get("after_interrupt"))}))
    return out


def _aw_blocks(task: Task, wins: list[dict[str, Any]], steer_ts: list[datetime]) -> list[Request]:
    """Contiguous same-category window time of AW_BLOCK_MIN_S or more with no steer inside.
    shortcut: 'no steer inside' stands in for 'overlaps no steer interval'; upgrade by reading
    agents.steer_interval once the writer may."""
    blocks: list[list[dict[str, Any]]] = []
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


def _context_steers(src: Source, steers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Local opt-in covers supported agents; teacher sources retain their original boundary."""
    return [r for r in steers if r["agent"] in src.context_agents]


def _episode_days(mine: list[dict[str, Any]], acts: list[dict[str, Any]], of_session: dict[str, list[str]],
                  day_of: set[date]) -> dict[tuple[str, date], dict[str, Any]]:
    """(episode, local day) -> its steers, sessions and activity rows that day."""
    days: dict[tuple[str, date], dict[str, Any]] = {}
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
    return days


def _episode_day_unit(task: Task, ep: str, d: date, u: dict[str, Any], text: dict[str, str],
                      reply: dict[str, tuple[str, str]]) -> Request | None:
    """The unit of one episode day; None when none of its steers has local text."""
    st = u["steers"]
    said = [text[r["event_id"]] for r in st if r["event_id"] in text]
    if not said:
        return None
    tools: Counter[str] = Counter()
    kinds = Counter(a["kind"] for a in u["acts"])
    for a in u["acts"]:
        if a["kind"] == "turn":
            tools.update((a["attrs"] or {}).get("by_tool") or {})
    return task.request("episode_day", f"{ep}:{d.isoformat()}", st[0]["ts"], {
        "first_steer": said[0], "last_steers": said[1:][-3:], "steers": len(st),
        "last_agent_reply": reply.get(st[-1]["event_id"], ("", ""))[1],
        "sessions": len(u["sessions"]), "tool_calls_by_tool": dict(tools),
        "tool_errors": sum((a["attrs"] or {}).get("tool_errors") or 0 for a in u["acts"]
                           if a["kind"] == "turn"),
        "interrupts": kinds["interrupt"], "rejections": kinds["rejection"],
        "commits": kinds["commit"]})


def _category_units(task: Task, src: Source, since: datetime, until: datetime | None) -> list[Request]:
    until = until or datetime.now(timezone.utc)
    steers = [r for r in src.steers(since) if r["ts"] < until]
    text = src.texts(steers, until)
    reply = src.replies(steers, until)
    mine = _context_steers(src, steers)
    context_sessions = {r["session_id"] for r in mine}
    eps = src.episodes(since)
    of_session: dict[str, list[str]] = defaultdict(list)
    for episode in eps:
        for m in episode["members"]:
            of_session[m["session_id"]].append(episode["id"])
    sids = sorted(context_sessions & set(of_session))
    acts = src.activity(sids, since, until) if sids else []
    day_of = set(local_days(since, until))
    days = _episode_days(mine, acts, of_session, day_of)
    out = []
    for (ep, d), u in days.items():
        req = _episode_day_unit(task, ep, d, u, text, reply)
        if req is None:
            src.missing_text_units.append(u["steers"][0]["ts"])
        else:
            out.append(req)
            if any(r["event_id"] not in text for r in u["steers"]):
                src.partial_text_units.append(u["steers"][0]["ts"])
    wins = src.windows(since, until)
    out += _aw_blocks(task, wins, [r["ts"] for r in steers])
    return out


def _outcome_units(task: Task, src: Source, since: datetime, until: datetime | None) -> list[Request]:
    """Episodes without commits (git outcome no_code) touched in the last OUTCOME_DAYS, as they
    stood at `until`: started before it, and only what was said before it."""
    if task.ver >= 3 and not src.require_outcome_reply:
        raise AutError("label: outcome contract 3 requires an opted-in bounded local source")
    now = until or datetime.now(timezone.utc)
    start = datetime(1970, 1, 1, tzinfo=timezone.utc) if src.mature_outcomes else since
    eps = [e for e in src.episodes(start) if e["outcome"] == "no_code" and e["first_ts"] < now]
    text_since = min([start, *(e["first_ts"] for e in eps)])
    steers = [r for r in _context_steers(src, src.steers(text_since)) if r["ts"] < now] if eps else []
    text = src.texts(steers, until)
    out = []
    for ep in eps:
        mine = {m["session_id"] for m in ep["members"]}
        st = [r for r in steers if r["session_id"] in mine
              and ep["first_ts"] <= r["ts"] <= ep["last_ts"]]
        said = [text[r["event_id"]] for r in st if r["event_id"] in text]
        if not said:
            src.missing_text_units.append(ep["first_ts"])
            continue
        if len(said) < len(st):
            src.partial_text_units.append(ep["first_ts"])
        # Session membership can span multiple episodes. Later replies and copied earlier
        # prompts must not turn this episode into a resolved task.
        reply = src.replies(st, min(now, ep["last_ts"] + timedelta(microseconds=1)))
        last_reply = reply.get(st[-1]["event_id"], ("", ""))[1]
        if src.require_outcome_reply and (len(said) < len(st) or not last_reply.strip()):
            src.incomplete_outcome_units.append(ep["first_ts"])
            continue
        # ts = the episode start, so the row's day (and its fold) never moves; rev =
        # its last activity, so a changed episode is re-asked and an unchanged one is a no-op
        out.append(task.request("episode", ep["id"], ep["first_ts"], {
            "last_steers": said[-5:],
            "last_agent_reply": last_reply,
            "days_idle": max((now - ep["last_ts"]).days, 0),
            "sessions": len({r["session_id"] for r in st}), "steers": len(st)},
            rev=ep["last_ts"].astimezone(timezone.utc).isoformat()))
    return out


def _title_units(task: Task, src: Source, since: datetime, until: datetime | None) -> list[Request]:
    """One unit per distinct browser (app, title); unit_id is an HMAC under the install salt, so
    it cannot confirm a guessed title. None without a salt: then nothing is labelled."""
    seen: dict[str, dict[str, Any]] = {}
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


UNITS: dict[str, Callable[[Task, Source, datetime, datetime | None], list[Request]]] = {
    "steer_intent": _steer_units, "work_category": _category_units,
    "soft_outcome": _outcome_units, "aw_title_relevance": _title_units,
}


def units(name: str, src: Source, since: datetime, until: datetime | None) -> list[Request]:
    """The task's requests in [since, until), oldest first. An episode unit belongs by its
    activity, so its ts (the episode start) can be before `since`."""
    return sorted(UNITS[name](TASKS[name], src, since, until), key=lambda r: (r.ts, r.unit_id))
