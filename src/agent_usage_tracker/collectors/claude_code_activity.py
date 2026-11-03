"""Claude Code activity rows (agents.activity_event): how a person steers, what a turn did.

Called record by record from the claude_code collector. attrs carry counts, flags, short enums,
ids and hashes only, never prompt text, tool output, paths, titles or commit messages.

The turn aggregates' retry and runaway rules are ported from AgentsView
(github.com/kenn-io/agentsview, MIT licence, (c) its authors).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from urllib.parse import urlparse

from ..event import ActivityEvent, EventSession, make_event_id

_INTERRUPT = "[Request interrupted"
_REJECTED = "The user doesn't want to proceed"
_COMMIT_CMD = re.compile(r"\bgit\b[^|;&]*\bcommit\b(?!-tree)")
_COMMIT_OUT = re.compile(r"^\[(?P<branch>[^\]\s]+)(?: \(root-commit\))? (?P<sha>[0-9a-f]{7,40})\]",
                         re.M)
# The condensed form a command proxy prints instead of git's own "[branch sha]" line: "ok <sha>"
# or "<sha> <subject>" on the FIRST line. Most commits on a machine with such a proxy look like this.
_CONDENSED = re.compile(r"(?:ok )?(?P<sha>[0-9a-f]{7,40})(?:\s|$)")


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _h(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def repo_identity(url: str) -> str:
    """host/owner/repo, lowercased, with credentials, port and a .git suffix dropped, so the https
    and scp-style remotes of one repo (and a PR's host + repository) give the same key."""
    m = re.match(r"^[\w.-]+@([^:/]+):(.+)$", url)       # git@host:owner/repo
    if m:
        host, path = m.groups()
    else:
        p = urlparse(url)
        host, path = p.hostname or "", p.path
    return f"{host}/{path.strip('/').removesuffix('.git')}".lower()


def repo_key(cwd: object, cache: dict) -> str | None:
    """sha256 of the origin remote's identity for a working dir; None when the directory or the
    remote is gone. The URL itself is never stored."""
    if not isinstance(cwd, str):
        return None
    if cwd not in cache:
        cache[cwd] = None
        if os.path.isdir(cwd):
            try:
                r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                                   capture_output=True, text=True, timeout=2)
                if r.returncode == 0 and r.stdout.strip():
                    cache[cwd] = _h(repo_identity(r.stdout.strip()))
            except (OSError, subprocess.SubprocessError):
                pass
    return cache[cwd]


def _blank() -> dict:
    return {"calls": 0, "errors": 0, "hooks": 0, "by": {}, "last": None, "run": 0, "max": 0}


class Activity:
    """Per-file, per-chunk extractor. state() is what must ride in the file's watermark entry."""

    def __init__(self, agent: str, path: str, subagent: bool, carried: dict | None,
                 repo_cache: dict) -> None:
        s = carried or {}
        self.agent, self.path, self.subagent, self.repo_cache = agent, path, subagent, repo_cache
        self.events: list[ActivityEvent] = []
        self.intr = bool(s.get("intr"))      # the last user-side record was an interrupt
        self.after_intr = False
        # Turn aggregates and unanswered commit calls are carried across scans in the watermark,
        # so a turn split by a scan boundary still gets one complete row.
        self.agg = s.get("agg") or _blank()
        self.pend: dict = s.get("pend") or {}          # commit tool_use_id -> is_amend
        self.prs: set = set(s.get("prs") or [])        # "sessionId|prNumber" already emitted
        # shortcut: these two are per chunk only. A re-logged block or an enqueue in an earlier scan
        # is not seen; upgrade if the per-turn counts or the queued split drift from a full re-read.
        self.queued: set[str] = set()
        self.seen: set[str] = set()

    def state(self) -> dict:
        s: dict = {}
        if self.intr:
            s["intr"] = True
        if self.agg["calls"] or self.agg["errors"] or self.agg["hooks"]:
            s["agg"] = self.agg
        if self.pend:
            s["pend"] = self.pend
        if self.prs:
            s["prs"] = sorted(self.prs)
        return s

    def _emit(self, kind: str, o: dict, seed: object, sess: EventSession, attrs: dict) -> None:
        self.events.append(ActivityEvent(
            event_id=make_event_id(self.agent, kind, seed), ts=o.get("timestamp"),
            agent=self.agent, kind=kind, session_id=sess.session_id, turn_id=sess.turn_id,
            attrs=attrs))

    def _uid(self, o: dict) -> str:
        return o.get("uuid") or f"{self.path}:{o.get('timestamp')}"

    def record(self, o: dict, sess: EventSession) -> None:
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

    def steer(self, o: dict, uid: str, text: str, sess: EventSession) -> ActivityEvent:
        """The steer row for a prompt the collector counted as human. Call after record(o)."""
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
        self.seen.add("u" + tid)
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        a = self.agg
        a["calls"] += 1
        a["by"][name] = a["by"].get(name, 0) + 1
        key = name + ":" + _h(json.dumps(inp, sort_keys=True, default=str))[:16]
        a["run"] = a["run"] + 1 if key == a["last"] else 1
        a["last"], a["max"] = key, max(a["max"], a["run"])
        if name == "Skill" and isinstance(inp.get("skill"), str):
            self._emit("invoke", o, tid, sess, {"skill": inp["skill"]})
        cmd = inp.get("command")
        if name == "Bash" and isinstance(cmd, str) and _COMMIT_CMD.search(cmd) \
                and "--dry-run" not in cmd:
            self.pend[tid] = "--amend" in cmd

    def _tool_result(self, o: dict, b: dict, sess: EventSession) -> None:
        tid = b.get("tool_use_id")
        if not isinstance(tid, str) or "r" + tid in self.seen:
            return
        self.seen.add("r" + tid)
        err = b.get("is_error") is True
        text = _text(b.get("content")).lstrip()
        if err:
            self.agg["errors"] += 1
            if text.startswith(_REJECTED):
                self._emit("rejection", o, tid, sess, {})
            elif text.startswith("PreToolUse:") and "hook error" in text[:200]:
                self.agg["hooks"] += 1          # a hook denied it, not the user
        if tid in self.pend:
            self._commits(o, tid, self.pend.pop(tid), text, err, sess)

    def _commits(self, o: dict, tid: str, amend: bool, text: str, err: bool,
                 sess: EventSession) -> None:
        found = [(m["branch"], m["sha"]) for m in _COMMIT_OUT.finditer(text)]
        branch = o.get("gitBranch") if isinstance(o.get("gitBranch"), str) else None
        if not found and not err:
            tr = o.get("toolUseResult")
            g = tr.get("gitOperation") if isinstance(tr, dict) else None
            c = g.get("commit") if isinstance(g, dict) else None
            m = _CONDENSED.match(text.split("\n", 1)[0])
            if isinstance(c, dict) and isinstance(c.get("sha"), str):
                found = [(c.get("branch") if isinstance(c.get("branch"), str) else branch,
                          c["sha"])]
            elif m:
                found = [(branch, m["sha"])]
        key = repo_key(o.get("cwd"), self.repo_cache) if found else None
        for br, sha in found:
            self._emit("commit", o, f"{tid}|{sha}", sess,
                       {"sha": sha, "branch": br, "repo_key": key, "amend": amend})

    def _pr(self, o: dict, sess: EventSession) -> None:
        num, url, repo = o.get("prNumber"), o.get("prUrl"), o.get("prRepository")
        if not isinstance(num, int) or isinstance(num, bool):
            return
        k = f"{sess.session_id}|{num}"
        if k in self.prs:                # one row per (session, PR), however often it is linked
            return
        self.prs.add(k)
        host = (urlparse(url).hostname or "") if isinstance(url, str) else ""
        kind = "github" if "github" in host else "bitbucket" if "bitbucket" in host else "other"
        key = _h(f"{host}/{repo.strip('/')}".lower()) if host and isinstance(repo, str) else None
        self.events.append(ActivityEvent(
            event_id=make_event_id(self.agent, "pr", sess.session_id, num), ts=o.get("timestamp"),
            agent=self.agent, kind="pr", session_id=sess.session_id, turn_id=sess.turn_id,
            attrs={"number": num, "host": kind, "repo_key": key}))

    def _turn(self, o: dict, sess: EventSession) -> None:
        a = self.agg
        top = max(a["by"].values(), default=0)
        self._emit("turn", o, self._uid(o), sess, {
            "duration_ms": o.get("durationMs") if isinstance(o.get("durationMs"), int) else None,
            "message_count": o.get("messageCount") if isinstance(o.get("messageCount"), int)
            else None,
            "tool_calls": a["calls"], "tool_errors": a["errors"], "by_tool": dict(a["by"]),
            "hook_blocks": a["hooks"], "max_identical_run": a["max"],
            # AgentsView (MIT): a retry loop is 3+ identical calls in a row; a runaway turn is
            # 6+ errors, or 3+ errors while one tool was called 10+ times.
            "retry_flag": a["max"] >= 3,
            "runaway_flag": a["errors"] >= 6 or (a["errors"] >= 3 and top >= 10),
        })
        self.agg = _blank()
        self.pend.clear()                # a commit call still unanswered at turn end never will be
