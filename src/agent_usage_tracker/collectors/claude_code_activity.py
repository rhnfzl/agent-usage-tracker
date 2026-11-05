"""Claude Code activity rows (agents.activity_event): how a person steers, what a turn did.

Called record by record from the claude_code collector. attrs carry counts, flags, short enums,
ids and hashes only, never prompt text, tool output, paths, titles or commit messages. It has its
own module only because of its size: this per-record machine is the same shape as the _Activity
classes in codex.py, pi.py and cursor_cli.py, which live next to their collectors.

The turn aggregates and the retry and runaway rules live in activity_common (from AgentsView,
github.com/kenn-io/agentsview, MIT licence, (c) its authors).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, TypedDict
from urllib.parse import urlparse

from ..event import ActivityKind, ActivityEvent, EventSession, RunOrigin, make_event_id
from ..repo_keys import RepoKeys
from .activity_common import (IDLE_S, Link, TurnAgg, add_call, best_effort, blank_tool_tally, ms_between,
                              pop_pending, record_commits, text_of, text_sha256, tool_key,
                              turn_attrs)
from .commits import commit_call, commit_dir, commit_shas
from ._util import MS_PER_S, dict_or_empty, str_or_none

_INTERRUPT = "[Request interrupted"
_REJECTED = "The user doesn't want to proceed"
_SEEN_MAX = 20     # open-turn tool ids carried across scans (re-logs land within ~40 lines)


class ActivityState(TypedDict, total=False):
    """What rides in the file's watermark entry; a key with nothing to carry is left out."""
    intr: bool             # the last user-side record was an interrupt
    agg: TurnAgg
    pend: dict[str, list[Any]]  # commit tool_use_id -> pop_pending's list
    seen: list[str]        # the open turn's newest tool block ids
    link: dict[str, Any]
    wt: list[Any]               # [repo_key, branch_key] of the worktree the session is in
    lo: dict[str, Any]               # the open link turn: last record's uid, ts, session and origin


class Activity:
    """Per-file, per-chunk extractor over `carried`, state from the private copy of the file's
    watermark entry. state() is what must ride in that entry."""

    def __init__(self, agent: str, path: str, subagent: bool, carried: ActivityState | None,
                 keys: RepoKeys) -> None:
        s: ActivityState = carried if isinstance(carried, dict) else {}
        self.agent, self.path, self.subagent, self.keys = agent, path, subagent, keys
        self.events: list[ActivityEvent] = []
        self.errors: Counter[str] = Counter()                       # records whose extraction raised (skipped)
        self.intr = bool(s.get("intr"))      # the last user-side record was an interrupt
        self.after_intr = False
        # Turn aggregates and unanswered commit calls are carried across scans in the watermark,
        # so a turn split by a scan boundary still gets one complete row.
        self.agg: TurnAgg = s.get("agg") or blank_tool_tally()
        self.pend: dict[str, Any] = s.get("pend") or {}
        # Tool block ids already counted. Within a chunk the set spans turns (Claude Code re-logs
        # history after a turn ends). Across scans only the open turn's newest _SEEN_MAX ids are
        # carried, which is where a re-log split by a scan boundary lands; this keeps the state small.
        # shortcut: subagent files carry none (they never close a turn, so the ids would never be
        # dropped); a subagent block re-logged across a scan boundary can count twice.
        carried_ids = [i for i in (s.get("seen") or []) if isinstance(i, str)]
        self.seen: dict[str, None] = dict.fromkeys(carried_ids)
        self.open_ids: list[str] = carried_ids
        # shortcut: per chunk only. An enqueue in an earlier scan is not seen; upgrade if the
        # queued split drifts from a full re-read.
        self.queued: set[str] = set()
        self.link = Link(keys, s.get("link") or {})
        self.wt: list[Any] | None = s.get("wt")
        self.lo: dict[str, Any] | None = s.get("lo")
        self._at3: tuple[Any, ...] | None = None     # the (cwd, gitBranch) last placed in this turn

    def state(self) -> ActivityState:
        s: ActivityState = {}
        if self.intr:
            s["intr"] = True
        # A subagent file never gets a turn_duration, so its aggregates would never be used.
        if not self.subagent and (self.agg["calls"] or self.agg["errors"] or self.agg["hooks"]):
            s["agg"] = self.agg
        if self.pend:
            s["pend"] = self.pend
        if self.open_ids and not self.subagent:
            s["seen"] = self.open_ids[-_SEEN_MAX:]
        if self.link.s:
            s["link"] = self.link.s
        if self.wt:
            s["wt"] = self.wt
        if self.lo:
            s["lo"] = self.lo
        return s

    def _emit(self, kind: ActivityKind, o: dict[str, Any], seed: object, sess: EventSession, attrs: dict[str, Any]) -> None:
        self.events.append(ActivityEvent(
            event_id=make_event_id(self.agent, kind, seed), ts=str_or_none(o.get("timestamp")),
            agent=self.agent, kind=kind, session_id=sess.session_id, turn_id=sess.turn_id,
            attrs=attrs))

    def _uid(self, o: dict[str, Any]) -> str:
        return o.get("uuid") or f"{self.path}:{o.get('timestamp')}"

    def finish(self) -> None:
        """The file is complete (read to its end and idle for IDLE_S): close its open link turn.
        A subagent or scripted file never gets a turn_duration to close it."""
        best_effort(self.errors, self._close_open_link)

    def _close_open_link(self) -> None:
        if self.lo:
            lo = self.lo
            self._close_link(lo["uid"], lo["ts"], lo["sid"], lo["turn"], lo["origin"])

    def _close_link(self, seed: str, ts: str | None, sid: str, turn: str | None,
                    origin: RunOrigin) -> None:
        if self.wt:
            self.link.s.setdefault("at", {}).setdefault("1", self.wt)
        self.events.append(ActivityEvent(
            event_id=make_event_id(self.agent, "link", seed), ts=ts, agent=self.agent,
            kind="link", session_id=sid, turn_id=turn, attrs=self.link.attrs(origin)))
        self.link.reset()
        self.lo, self._at3 = None, None

    def record(self, o: dict[str, Any], sess: EventSession) -> None:
        typ = o.get("type")
        self._close_link_after_gap(o)
        if typ in ("queue-operation", "attachment"):
            self._note_queued(o)
        elif typ == "pr-link":
            self._pr(o, sess)
        elif typ == "worktree-state":
            self._worktree(o)
        elif typ in ("ai-title", "custom-title"):
            self.link.note_tickets(o.get("aiTitle") or o.get("customTitle"))
        elif typ == "system":
            self._system(o, sess)
        elif typ in ("assistant", "user"):
            self._note_link_turn(o, sess)
            m = o.get("message")
            content = m.get("content") if isinstance(m, dict) else None
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            if typ == "assistant":
                self._assistant(o, blocks, sess)
            else:
                self._user(o, content, blocks, sess)

    def _close_link_after_gap(self, o: dict[str, Any]) -> None:
        # A record IDLE_S or more after the open link turn's last one: that turn closed there,
        # as a live scan that saw the file idle in between closed it, so any split of scans and
        # one full read cut the same place. shortcut: assumes a record reaches the file within
        # IDLE_S of its timestamp (a synced log that lags longer can split differently); upgrade
        # if synced files show split-vs-full drift.
        gap = ms_between(self.lo["ts"], o.get("timestamp")) if self.lo else None
        if gap is not None and gap >= IDLE_S * MS_PER_S:
            self.finish()

    def _note_link_turn(self, o: dict[str, Any], sess: EventSession) -> None:
        self.lo = {"uid": self._uid(o), "ts": str_or_none(o.get("timestamp")), "sid": sess.session_id,
                   "turn": sess.turn_id, "origin": sess.run_origin}
        at3 = (o.get("cwd"), o.get("gitBranch"))
        if at3 != self._at3:
            self._at3 = at3
            self.link.place(3, self.keys.of_cwd(at3[0]), at3[1])

    def _note_queued(self, o: dict[str, Any]) -> None:
        if o.get("type") == "queue-operation":
            if o.get("operation") == "enqueue" and isinstance(o.get("content"), str):
                self.queued.add(text_sha256(o["content"]))
            return
        a = o.get("attachment")
        if isinstance(a, dict) and a.get("type") == "queued_command":
            self.queued.add(text_sha256(text_of(a.get("prompt"))))

    def _worktree(self, o: dict[str, Any]) -> None:
        ws = o.get("worktreeSession")
        if isinstance(ws, dict):
            rk = self.keys.of_cwd(ws.get("originalCwd")) \
                or self.keys.of_cwd(ws.get("worktreePath"))
            self.wt = self.link.source(rk, ws.get("worktreeBranch"))
        else:                        # the session left its worktree
            self.wt = None

    def _system(self, o: dict[str, Any], sess: EventSession) -> None:
        if o.get("subtype") == "turn_duration":
            self._turn(o, sess)
        elif o.get("subtype") == "compact_boundary":
            meta = o.get("compactMetadata")
            trig = meta.get("trigger") if isinstance(meta, dict) else None
            self._emit("compaction", o, self._uid(o), sess,
                       {"trigger": trig} if isinstance(trig, str) else {})

    def _assistant(self, o: dict[str, Any], blocks: list[dict[str, Any]], sess: EventSession) -> None:
        for b in blocks:
            if b.get("type") == "tool_use":
                self._tool_use(o, b, sess)
            elif b.get("type") == "text":
                # shortcut: a re-logged text block is read again (its SHAs and PRs are the
                # session's own, so only the next turn's lists grow); dedup by uuid if that skews
                # the link lists
                self.link.note_text(b.get("text"))

    def _user(self, o: dict[str, Any], content: object, blocks: list[dict[str, Any]], sess: EventSession) -> None:
        self.link.note_text(text_of(content))
        for b in blocks:
            if b.get("type") == "tool_result":
                self._tool_result(o, b, sess)
        if o.get("isMeta"):
            return
        is_intr = text_of(content).lstrip().startswith(_INTERRUPT)
        self.after_intr, self.intr = self.intr, is_intr
        # A subagent's copy of the interrupt is the same keypress the parent already logged.
        if is_intr and not (self.subagent or o.get("isSidechain")):
            self._emit("interrupt", o, self._uid(o), sess,
                       {"during_tool": "for tool use" in text_of(content)})

    def steer(self, o: dict[str, Any], uid: str, text: str, sess: EventSession) -> ActivityEvent | None:
        """The steer row for a prompt the collector counted as human (None if it raised). Call after
        record(o)."""
        return best_effort(self.errors, self._steer, o, uid, text, sess)

    def _steer(self, o: dict[str, Any], uid: str, text: str, sess: EventSession) -> ActivityEvent:
        s, ps = text.lstrip(), o.get("promptSource")
        if s.startswith("<bash-input>"):
            via = "bash_input"
        elif s.startswith("<skill>"):
            via = "skill"
        elif s.startswith(("<command-name>", "<command-message>")):
            via = "slash_command"
        elif ps == "suggestion_accepted":
            via = "suggestion_accepted"
        # promptSource says so on newer files; older ones only show the earlier enqueue.
        elif ps == "queued" or (ps is None and text_sha256(text) in self.queued):
            via = "queued"
        else:
            via = "typed"
        return ActivityEvent(
            event_id=make_event_id(self.agent, "steer", uid), ts=str_or_none(o.get("timestamp")),
            agent=self.agent, kind="steer", session_id=sess.session_id, turn_id=sess.turn_id,
            attrs={"via": via, "words": len(text.split()), "chars": len(text),
                   "after_interrupt": self.after_intr})

    def _tool_use(self, o: dict[str, Any], b: dict[str, Any], sess: EventSession) -> None:
        tid, name = b.get("id"), b.get("name")
        if not isinstance(tid, str) or not isinstance(name, str) or "u" + tid in self.seen:
            return                       # a re-logged block counts once
        self.seen["u" + tid] = None
        self.open_ids.append("u" + tid)
        inp = dict_or_empty(b.get("input"))
        add_call(self.agg, name, inp)
        if name == "Skill" and isinstance(inp.get("skill"), str):
            self._emit("invoke", o, tid, sess, {"skill": tool_key(inp["skill"])})
        cmd = inp.get("command")
        if name == "Bash" and isinstance(cmd, str) and (call := commit_call(cmd)):
            self.pend[tid] = [*call, commit_dir(cmd)]

    def _tool_result(self, o: dict[str, Any], b: dict[str, Any], sess: EventSession) -> None:
        tid = b.get("tool_use_id")
        if not isinstance(tid, str) or "r" + tid in self.seen:
            return
        self.seen["r" + tid] = None
        self.open_ids.append("r" + tid)
        err = b.get("is_error") is True
        text = text_of(b.get("content")).lstrip()
        self.link.note_text(text)
        if err:
            self.agg["errors"] += 1
            if text.startswith(_REJECTED):
                self._emit("rejection", o, tid, sess, {})
            elif text.startswith("PreToolUse:") and "hook error" in text[:200]:
                self.agg["hooks"] += 1          # a hook denied it, not the user
        if tid in self.pend:
            amend, before, target = pop_pending(self.pend, tid)
            self._commits(o, tid, text, sess, target, amend=amend, before=before, err=err)

    def _commits(self, o: dict[str, Any], tid: str, text: str, sess: EventSession, target: object, *,
                 amend: bool, before: bool, err: bool) -> None:
        tr = o.get("toolUseResult")
        g = tr.get("gitOperation") if isinstance(tr, dict) else None
        c = g.get("commit") if isinstance(g, dict) else None
        sha = c.get("sha") if isinstance(c, dict) and isinstance(c.get("sha"), str) else None
        found = commit_shas(text, err, before, sha)
        for sha, attrs in record_commits(self.link, o.get("cwd"), target, text, found, amend=amend,
                                         fallback=self.wt[0] if self.wt else None):
            self._emit("commit", o, f"{tid}|{sha}", sess, attrs)

    def _pr(self, o: dict[str, Any], sess: EventSession) -> None:
        num, url, repo = o.get("prNumber"), o.get("prUrl"), o.get("prRepository")
        if not isinstance(num, int) or isinstance(num, bool):
            return
        try:
            host = (urlparse(url).hostname or "") if isinstance(url, str) else ""
        except ValueError:               # a malformed URL ("https://[x/...")
            host = ""
        kind = "github" if "github" in host else "bitbucket" if "bitbucket" in host else "other"
        key = self.keys.of(f"{host}/{repo.strip('/')}".lower()) \
            if host and isinstance(repo, str) else None
        self.link.note_pr(host, repo, num)
        # One row per pr-link record (its ts never depends on how much of the file a scan read);
        # distinct PRs are counted in SQL. Real pr-link records carry no uuid, so the fallback seed
        # is session + number + the record's own ts: a re-logged copy maps to the same row.
        seed = o.get("uuid") or f"{sess.session_id}|{num}|{o.get('timestamp')}"
        self._emit("pr", o, seed, sess, {"number": num, "host": kind, "repo_key": key})

    def _turn(self, o: dict[str, Any], sess: EventSession) -> None:
        self._emit("turn", o, self._uid(o), sess, {
            "duration_ms": o.get("durationMs") if isinstance(o.get("durationMs"), int) else None,
            "message_count": o.get("messageCount") if isinstance(o.get("messageCount"), int)
            else None,
            "hook_blocks": self.agg["hooks"], **turn_attrs(self.agg),
        })
        self.agg = blank_tool_tally()
        self.pend.clear()                # a commit call still unanswered at turn end never will be
        self.open_ids = []
        self._close_link(self._uid(o), o.get("timestamp"), sess.session_id, sess.turn_id,
                         sess.run_origin)
