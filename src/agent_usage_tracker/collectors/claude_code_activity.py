"""Claude Code activity rows (agents.activity_event): how a person steers, what a turn did.

Called record by record from the claude_code collector. attrs carry counts, flags, short enums,
ids and hashes only, never prompt text, tool output, paths, titles or commit messages.

The turn aggregates and the retry and runaway rules live in _activity_common (from AgentsView,
github.com/kenn-io/agentsview, MIT licence, (c) its authors).
"""

from __future__ import annotations

import copy
from urllib.parse import urlparse

from ..event import ActivityEvent, EventSession, make_event_id
# repo_identity and _GIT_COMMIT are re-exported for the tests that lock them
from ._activity_common import _GIT_COMMIT, RepoKeys, repo_identity  # noqa: F401
from ._activity_common import add_call, blank_agg, commit_call, commit_shas, turn_attrs
from ._activity_common import h as _h
from ._activity_common import text_of as _text
from ._activity_common import tool_key as _tool

_INTERRUPT = "[Request interrupted"
_REJECTED = "The user doesn't want to proceed"
_SEEN_MAX = 20     # open-turn tool ids carried across scans (re-logs land within ~40 lines)


class Activity:
    """Per-file, per-chunk extractor. state() is what must ride in the file's watermark entry."""

    def __init__(self, agent: str, path: str, subagent: bool, carried: dict | None,
                 keys: RepoKeys) -> None:
        # A copy: only the collector's commit() may publish new state into the watermark, or a
        # failed delivery would save this chunk's counts next to the old offset.
        s = copy.deepcopy(carried) if isinstance(carried, dict) else {}
        self.agent, self.path, self.subagent, self.keys = agent, path, subagent, keys
        self.events: list[ActivityEvent] = []
        self.errors = 0                       # records whose extraction raised (skipped)
        self.intr = bool(s.get("intr"))      # the last user-side record was an interrupt
        self.after_intr = False
        # Turn aggregates and unanswered commit calls are carried across scans in the watermark,
        # so a turn split by a scan boundary still gets one complete row.
        self.agg = s.get("agg") or blank_agg()
        self.pend: dict = s.get("pend") or {}  # commit tool_use_id -> [is_amend, printed_before]
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

    def state(self) -> dict:
        s: dict = {}
        if self.intr:
            s["intr"] = True
        # A subagent file never gets a turn_duration, so its aggregates would never be used.
        if not self.subagent and (self.agg["calls"] or self.agg["errors"] or self.agg["hooks"]):
            s["agg"] = self.agg
        if self.pend:
            s["pend"] = self.pend
        if self.open_ids and not self.subagent:
            s["seen"] = self.open_ids[-_SEEN_MAX:]
        return s

    def _emit(self, kind: str, o: dict, seed: object, sess: EventSession, attrs: dict) -> None:
        self.events.append(ActivityEvent(
            event_id=make_event_id(self.agent, kind, seed), ts=o.get("timestamp"),
            agent=self.agent, kind=kind, session_id=sess.session_id, turn_id=sess.turn_id,
            attrs=attrs))

    def _uid(self, o: dict) -> str:
        return o.get("uuid") or f"{self.path}:{o.get('timestamp')}"

    def record(self, o: dict, sess: EventSession) -> None:
        """Best effort: a record whose extraction raises is counted and skipped, so activity can
        never block the usage and session rows of the same chunk."""
        try:
            self._record(o, sess)
        except Exception:
            self.errors += 1

    def _record(self, o: dict, sess: EventSession) -> None:
        typ = o.get("type")
        if typ == "queue-operation":
            if o.get("operation") == "enqueue" and isinstance(o.get("content"), str):
                self.queued.add(_h(o["content"]))
        elif typ == "attachment":
            a = o.get("attachment")
            if isinstance(a, dict) and a.get("type") == "queued_command":
                self.queued.add(_h(_text(a.get("prompt"))))
        elif typ == "pr-link":
            self._pr(o, sess)
        elif typ == "system" and o.get("subtype") == "turn_duration":
            self._turn(o, sess)
        elif typ == "system" and o.get("subtype") == "compact_boundary":
            meta = o.get("compactMetadata")
            trig = meta.get("trigger") if isinstance(meta, dict) else None
            self._emit("compaction", o, self._uid(o), sess,
                       {"trigger": trig} if isinstance(trig, str) else {})
        elif typ in ("assistant", "user"):
            m = o.get("message")
            content = m.get("content") if isinstance(m, dict) else None
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            if typ == "assistant":
                for b in blocks:
                    if b.get("type") == "tool_use":
                        self._tool_use(o, b, sess)
                return
            for b in blocks:
                if b.get("type") == "tool_result":
                    self._tool_result(o, b, sess)
            if o.get("isMeta"):
                return
            is_intr = _text(content).lstrip().startswith(_INTERRUPT)
            self.after_intr, self.intr = self.intr, is_intr
            # A subagent's copy of the interrupt is the same keypress the parent already logged.
            if is_intr and not (self.subagent or o.get("isSidechain")):
                self._emit("interrupt", o, self._uid(o), sess,
                           {"during_tool": "for tool use" in _text(content)})

    def steer(self, o: dict, uid: str, text: str, sess: EventSession) -> ActivityEvent | None:
        """The steer row for a prompt the collector counted as human (None if it raised). Call after
        record(o)."""
        try:
            return self._steer(o, uid, text, sess)
        except Exception:
            self.errors += 1
            return None

    def _steer(self, o: dict, uid: str, text: str, sess: EventSession) -> ActivityEvent:
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
        elif ps == "queued" or (ps is None and _h(text) in self.queued):
            via = "queued"
        else:
            via = "typed"
        return ActivityEvent(
            event_id=make_event_id(self.agent, "steer", uid), ts=o.get("timestamp"),
            agent=self.agent, kind="steer", session_id=sess.session_id, turn_id=sess.turn_id,
            attrs={"via": via, "words": len(text.split()), "chars": len(text),
                   "after_interrupt": self.after_intr})

    def _tool_use(self, o: dict, b: dict, sess: EventSession) -> None:
        tid, name = b.get("id"), b.get("name")
        if not isinstance(tid, str) or not isinstance(name, str) or "u" + tid in self.seen:
            return                       # a re-logged block counts once
        self.seen["u" + tid] = None
        self.open_ids.append("u" + tid)
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        add_call(self.agg, name, inp)
        if name == "Skill" and isinstance(inp.get("skill"), str):
            self._emit("invoke", o, tid, sess, {"skill": _tool(inp["skill"])})
        cmd = inp.get("command")
        call = commit_call(cmd) if name == "Bash" and isinstance(cmd, str) else None
        if call:
            self.pend[tid] = list(call)

    def _tool_result(self, o: dict, b: dict, sess: EventSession) -> None:
        tid = b.get("tool_use_id")
        if not isinstance(tid, str) or "r" + tid in self.seen:
            return
        self.seen["r" + tid] = None
        self.open_ids.append("r" + tid)
        err = b.get("is_error") is True
        text = _text(b.get("content")).lstrip()
        if err:
            self.agg["errors"] += 1
            if text.startswith(_REJECTED):
                self._emit("rejection", o, tid, sess, {})
            elif text.startswith("PreToolUse:") and "hook error" in text[:200]:
                self.agg["hooks"] += 1          # a hook denied it, not the user
        if tid in self.pend:
            amend, before = self.pend.pop(tid)
            self._commits(o, tid, bool(amend), bool(before), text, err, sess)

    def _commits(self, o: dict, tid: str, amend: bool, before: bool, text: str, err: bool,
                 sess: EventSession) -> None:
        tr = o.get("toolUseResult")
        g = tr.get("gitOperation") if isinstance(tr, dict) else None
        c = g.get("commit") if isinstance(g, dict) else None
        sha = c.get("sha") if isinstance(c, dict) and isinstance(c.get("sha"), str) else None
        found = commit_shas(text, err, before, sha)
        key = self.keys.of_cwd(o.get("cwd")) if found else None
        for sha in found:
            self._emit("commit", o, f"{tid}|{sha}", sess,
                       {"sha": sha, "repo_key": key, "amend": amend})

    def _pr(self, o: dict, sess: EventSession) -> None:
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
        # One row per pr-link record (its ts never depends on how much of the file a scan read);
        # distinct PRs are counted in SQL. Real pr-link records carry no uuid, so the fallback seed
        # is session + number + the record's own ts: a re-logged copy maps to the same row.
        seed = o.get("uuid") or f"{sess.session_id}|{num}|{o.get('timestamp')}"
        self._emit("pr", o, seed, sess, {"number": num, "host": kind, "repo_key": key})

    def _turn(self, o: dict, sess: EventSession) -> None:
        self._emit("turn", o, self._uid(o), sess, {
            "duration_ms": o.get("durationMs") if isinstance(o.get("durationMs"), int) else None,
            "message_count": o.get("messageCount") if isinstance(o.get("messageCount"), int)
            else None,
            "hook_blocks": self.agg["hooks"], **turn_attrs(self.agg),
        })
        self.agg = blank_agg()
        self.pend.clear()                # a commit call still unanswered at turn end never will be
        self.open_ids = []
