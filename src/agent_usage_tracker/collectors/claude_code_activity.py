"""Claude Code activity rows (agents.activity_event): how a person steers, what a turn did.

Called record by record from the claude_code collector. attrs carry counts, flags, short enums,
ids and hashes only, never prompt text, tool output, paths, titles or commit messages.

The turn aggregates' retry and runaway rules are ported from AgentsView
(github.com/kenn-io/agentsview, MIT licence, (c) its authors).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
from urllib.parse import urlparse

from ..event import ActivityEvent, EventSession, make_event_id
from ..state import _state_path

_INTERRUPT = "[Request interrupted"
_REJECTED = "The user doesn't want to proceed"
_SEGMENTS = re.compile(r"&&|\|\||[;|\n]")
_HEREDOC = re.compile(r"""<<-?\s*(['"]?)(\w+)\1([^\n]*)\n.*?\n\s*\2[ \t]*(?=\n|$)""", re.S)
_ARG = r"""(?:[^\s"']|"[^"]*"|'[^']*')+"""
# optional wrappers (VAR=x, env, command, timeout N, rtk [proxy]), then `git`, then only global
# options (-C <dir>, -c <k=v>, --flag[=v]), then the commit subcommand itself
_GIT_COMMIT = re.compile(
    rf"^\s*[({{]?\s*(?:(?:\w+={_ARG}|(?:\S*/)?env|command|timeout\s+\S+|rtk(?:\s+proxy)?)\s+)*"
    rf"(?:\S*/)?git(?:\s+(?:-[Cc]\s+{_ARG}|--[\w-]+(?:={_ARG})?|-\w+))*\s+commit(?:\s|$)")
# segments that print nothing, so a commit after only these still owns the output's first line
_SILENT = re.compile(rf"^\s*(?:(?:cd|pushd|set|export|true|mkdir|rm)\b|cat\s+>|\w+={_ARG}\s*$"
                     rf"|(?:\S*/)?git(?:\s+-C\s+{_ARG})?\s+add\b)")
_COMMIT_OUT = re.compile(r"^\[[^\]\s]+(?: \(root-commit\))? (?P<sha>[0-9a-f]{7,40})\]", re.M)
# The condensed form a command proxy prints instead of git's own "[branch sha]" line: "ok <sha>"
# or "<sha> <subject>" on the FIRST line. Most commits on a machine with such a proxy look like this.
_CONDENSED = re.compile(r"(?:ok )?(?P<sha>[0-9a-f]{7,40})(?:\s|$)")
_OK_LINE = re.compile(r"^ok (?P<sha>[0-9a-f]{7,40})\s*$", re.M)
_SEEN_MAX = 20     # open-turn tool ids carried across scans (re-logs land within ~40 lines)


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict)
                         and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def _tool(name: str) -> str:
    """MCP tools are `mcp__<server>__<tool>`, and a server label is free text (it can name an
    employer or client), so they all count as `mcp`."""
    return "mcp" if name.startswith("mcp__") else name


def _h(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def repo_identity(url: str) -> str:
    """host/owner/repo, lowercased, with credentials, port and a .git suffix dropped, so the https
    and scp-style remotes of one repo (and a PR's host + repository) give the same key."""
    m = re.match(r"^[\w.-]+@([^:/]+):(.+)$", url)       # git@host:owner/repo
    if m:
        host, path = m.groups()
    else:
        p = urlparse(url)                 # raises ValueError on a malformed URL
        host, path = p.hostname or "", p.path
    return f"{host}/{path.strip('/').removesuffix('.git')}".lower()


def _load_salt(configured: str | None) -> bytes:
    """The config's repo_key_salt, else a random salt kept next to the state file (created 0600 on
    first use). b"" when neither can be had: repo keys are then NULL, never an unsalted hash."""
    if configured:
        return configured.encode("utf-8")
    p = _state_path().parent / "repo_key.salt"
    try:
        if not p.exists():
            # Written in full to a temp file, then linked into place: a crash or a second process
            # never sees a half-written salt, and the first link wins a race.
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
            try:
                os.link(tmp, p)
            except FileExistsError:
                pass
            finally:
                tmp.unlink(missing_ok=True)
        salt = p.read_text().strip()
        if os.name != "nt" and p.stat().st_mode & 0o077:     # Windows has no POSIX modes
            print(f"aut: warning: {p} is group/other-readable; run: chmod 600 {p}", file=sys.stderr)
    except OSError:
        return b""
    if len(salt) < 32:
        print(f"aut: warning: {p} is too short; repo_key stays NULL until it is fixed",
              file=sys.stderr)
        return b""
    return salt.encode("utf-8")


class RepoKeys:
    """repo_key = HMAC-SHA256(salt, host/owner/repo), so a list of candidate repo names cannot be
    tested against stored keys without the salt. Cached per cwd for one collect() pass."""

    def __init__(self, salt: str | None) -> None:
        self._configured, self._salt, self._cwd = salt, None, {}

    def of(self, identity: str) -> str | None:
        if self._salt is None:
            self._salt = _load_salt(self._configured)
        if not self._salt:
            return None
        return hmac.new(self._salt, identity.encode("utf-8"), hashlib.sha256).hexdigest()

    def of_cwd(self, cwd: object) -> str | None:
        """Key of the origin remote for a working dir; None when the directory or the remote is
        gone. The URL itself is never stored or logged."""
        if not isinstance(cwd, str):
            return None
        if cwd not in self._cwd:
            self._cwd[cwd] = None
            if os.path.isdir(cwd):
                try:
                    r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                                       capture_output=True, timeout=2)
                    url = r.stdout.decode("utf-8", "replace").strip()
                    if r.returncode == 0 and url:
                        self._cwd[cwd] = self.of(repo_identity(url))
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
        return self._cwd[cwd]


def _commit_call(cmd: str) -> tuple[bool, bool] | None:
    """(is_amend, printed_before) when a command segment runs `git commit` (not --dry-run);
    printed_before: a segment ahead of the commit may have printed, so the output's first line is
    not necessarily the commit's."""
    # a heredoc body is data (a file, a commit message), not commands: drop it before splitting
    segs = [x for x in _SEGMENTS.split(_HEREDOC.sub(r"\3", cmd)) if x.strip()]
    hits = [i for i, x in enumerate(segs) if _GIT_COMMIT.match(x) and "--dry-run" not in x.split()]
    if not hits:
        return None
    return (any("--amend" in segs[i].split() for i in hits),
            not all(_SILENT.match(x) for x in segs[:hits[0]]))


def _blank() -> dict:
    return {"calls": 0, "errors": 0, "hooks": 0, "by": {}, "last": None, "run": 0, "max": 0}


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
        self.agg = s.get("agg") or _blank()
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
        a = self.agg
        a["calls"] += 1
        a["by"][_tool(name)] = a["by"].get(_tool(name), 0) + 1
        key = name + ":" + _h(json.dumps(inp, sort_keys=True, default=str))[:16]
        a["run"] = a["run"] + 1 if key == a["last"] else 1
        a["last"], a["max"] = key, max(a["max"], a["run"])
        if name == "Skill" and isinstance(inp.get("skill"), str):
            self._emit("invoke", o, tid, sess, {"skill": _tool(inp["skill"])})
        cmd = inp.get("command")
        call = _commit_call(cmd) if name == "Bash" and isinstance(cmd, str) else None
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
        found = [m["sha"] for m in _COMMIT_OUT.finditer(text)]
        if not found and not err:
            tr = o.get("toolUseResult")
            g = tr.get("gitOperation") if isinstance(tr, dict) else None
            c = g.get("commit") if isinstance(g, dict) else None
            if isinstance(c, dict) and isinstance(c.get("sha"), str):
                found = [c["sha"]]
            elif before:     # an earlier command may have printed: only a late "ok <sha>" counts
                found = [m["sha"] for m in _OK_LINE.finditer(text)][-1:]
            elif m := _CONDENSED.match(text.split("\n", 1)[0]):
                found = [m["sha"]]
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
        self.open_ids = []
