"""What the activity extractors share: the per-turn tool aggregates, the per-turn link fields and
small record helpers. Commit detection lives in commits.py. attrs built from these carry counts,
flags, short enums and hashes only.

The turn aggregates' retry and runaway rules are ported from AgentsView
(github.com/kenn-io/agentsview, MIT licence, (c) its authors).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from typing import Any, Callable, NotRequired, TypedDict, TypeVar

from ..event import EventSession, UsageEvent, make_event_id
from ..repo_keys import RepoKeys, repo_identity
from ._util import MS_PER_S
from .commits import COMMIT_LINE, commit_repo

_T = TypeVar("_T")
_HEX_SHA = re.compile(r"[0-9a-f]{7,40}")
# link fields: a commit-like SHA (7 to 40 hex, not inside a longer word, a uuid or a 64-hex hash),
# a strict ticket id, a GitHub or Bitbucket PR URL
_SHA_REF = re.compile(r"(?<![\w-])[0-9a-f]{7,40}(?![\w-])")
_TICKET = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}-\d+(?![A-Za-z0-9])")
_NOT_TICKET = {"UTF", "SHA", "ISO", "RFC", "CVE", "PEP", "HTTP", "TLS", "SSL", "WCAG"}
_PR_URL = re.compile(r"https?://(?:www\.)?(?:(github\.com)/([\w.-]+)/([\w.-]+)/pull"
                     r"|(bitbucket\.org)/([\w.-]+)/([\w.-]+)/pull-requests)/(\d+)")
_DEFAULT_BRANCHES = {"main", "master", "develop", "trunk", "HEAD"}
# 20 SHAs (first come) filled up on 7% of Claude Code and 26% of Cursor turns that quote any,
# pushing out a commit quoted late in the turn; 50 is cheap at one key each
MAX_SHAS, MAX_PRS, MAX_TICKETS = 50, 10, 5
IDLE_S = 3600        # a file untouched this long is complete: its open link turn closes


def text_of(content: object) -> str:
    """A message's text: the string itself, or its text blocks joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict)
                         and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def tool_key(name: str) -> str:
    """MCP tools are `mcp__<server>__<tool>`, and a server label is free text (it can name an
    employer or client), so they all count as `mcp`."""
    return "mcp" if name.startswith("mcp__") else name


def text_sha256(text: str) -> str:
    """Hex SHA-256 of the stripped text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def ms_between(start: object, end: object) -> int | None:
    """Milliseconds from one ISO-8601 timestamp to another; None when either is unusable."""
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return int((b - a).total_seconds() * MS_PER_S)
    except (TypeError, ValueError):
        return None


def first_record(path: str) -> dict | None:
    """The file's first JSON line, for a header a resumed scan no longer reads."""
    try:
        with open(path, "rb") as fh:
            o = json.loads(fh.readline())
    except (OSError, ValueError):
        return None
    return o if isinstance(o, dict) else None


def branch_key(keys: RepoKeys, repo_key: str | None, branch: object) -> str | None:
    """HMAC(repo_key || branch) for a non-default branch; a branch name is never stored."""
    if not repo_key or not isinstance(branch, str) or not branch \
            or branch in _DEFAULT_BRANCHES:
        return None
    return keys.of(repo_key + branch)


def pr_key(keys: RepoKeys, host: str, repo: str, number: object) -> str | None:
    try:
        identity = repo_identity(f"https://{host}/{repo.strip('/')}")
    except ValueError:
        return None
    return keys.of(f"{identity}|{number}")


def sha_key(keys: RepoKeys, sha: str) -> str | None:
    """HMAC of a SHA's first 7 hex: a quoted SHA and a printed commit meet on it, and a hex secret
    that passes the SHA filter (a 32-hex API key, a 40-hex token) is never kept, not even a
    prefix of it. None without a salt, and None for anything that is not 7 to 40 hex (a structured
    sha of '1' would otherwise key every session that printed it into one episode)."""
    sha = sha.lower()
    return keys.of("sha7:" + sha[:7]) if _HEX_SHA.fullmatch(sha) else None


class Link:
    """One turn's link fields (activity kind `link`). The state dict `s` is mutated in place and
    rides in the watermark, so it holds keys only, never a SHA, branch, ticket, URL or path.
    s["at"]: source rank -> [repo_key, branch_key]; rank 0 the commit (`[branch sha]` line and the
    commit's own directory), 1 the worktree, 2 the session meta (Codex git), 3 the record.
    branch_key is False where the source names a default branch: it has no key, and a lower rank
    may not stand in for it."""

    def __init__(self, keys: RepoKeys, s: dict) -> None:
        self.keys, self.s = keys, s

    def source(self, repo_key: str | None, branch: object = None) -> list[str | bool | None]:
        """[repo_key, branch_key] of one source (its branch's tickets go to this turn)."""
        self.note_tickets(branch)
        named = repo_key and isinstance(branch, str) and branch in _DEFAULT_BRANCHES
        return [repo_key, False if named else branch_key(self.keys, repo_key, branch)]

    def place(self, rank: int, repo_key: str | None, branch: object = None) -> None:
        self.s.setdefault("at", {})[str(rank)] = self.source(repo_key, branch)

    def _add(self, field: str, value: str | None, cap: int) -> bool:
        got = self.s.setdefault(field, [])
        if value and value not in got and len(got) < cap:
            got.append(value)
        return len(got) < cap

    def note_tickets(self, text: object) -> None:
        if isinstance(text, str) and "-" in text:
            for m in _TICKET.finditer(text):
                if m[0].split("-")[0] not in _NOT_TICKET \
                        and not self._add("tix", self.keys.of(m[0]), MAX_TICKETS):
                    break

    def note_commit(self, repo_key: str | None, text: str, shas: list[str]) -> None:
        """A commit call's output: its repo, the commits it printed (`commit_shas`), and the
        branch and subject of git's commit line."""
        for sha in shas:
            self._add("cks", sha_key(self.keys, sha), MAX_SHAS)
        m = COMMIT_LINE.search(text)
        self.place(0, repo_key, m["br"] if m else None)
        for m in COMMIT_LINE.finditer(text):
            self.note_tickets(m["subj"])

    def note_text(self, text: object) -> None:
        """Tool output or message text: SHAs it quotes and the PRs it links."""
        if not isinstance(text, str):
            return
        if len(self.s.get("shas", ())) < MAX_SHAS:
            for m in _SHA_REF.finditer(text):
                v = m[0]
                if not v.isdigit() and not v.isalpha() \
                        and not self._add("shas", sha_key(self.keys, v), MAX_SHAS):
                    break
        if "/pull" in text and len(self.s.get("prs", ())) < MAX_PRS:
            for m in _PR_URL.finditer(text):
                host, o, r = (m[1], m[2], m[3]) if m[1] else (m[4], m[5], m[6])
                if not self._add("prs", pr_key(self.keys, host, f"{o}/{r}", int(m[7])), MAX_PRS):
                    break

    def note_pr(self, host: str, repo: object, number: object) -> None:
        if host and isinstance(repo, str):
            self._add("prs", pr_key(self.keys, host, repo, number), MAX_PRS)

    def attrs(self, origin: str | None) -> dict[str, Any]:
        at = [self.s["at"][k] for k in sorted(self.s.get("at", {}))]
        # the repo of the first source that has one; its branch from that source, or from a lower
        # one in the SAME repo (a branch edge must never join two repos)
        rk = next((r for r, _b in at if r), None)
        bk = next((b for r, b in at if r == rk and b is not None), None) if rk else None
        return {"repo_key": rk, "branch_key": bk or None,
                "ticket_keys": list(self.s.get("tix", ())), "sha_refs": list(self.s.get("shas", ())),
                "commit_keys": list(self.s.get("cks", ())), "pr_keys": list(self.s.get("prs", ())),
                # subagents and scripted runs attach to an episode, never start one
                "role": "child" if origin in ("subagent", "automated") else "seed"}

    def merge(self, other: dict) -> None:
        """Add a session-level source (ranks it lacks, its tickets) to this turn."""
        for k, v in (other.get("at") or {}).items():
            self.s.setdefault("at", {}).setdefault(k, v)
        for t in other.get("tix") or ():
            self._add("tix", t, MAX_TICKETS)

    def reset(self) -> None:
        self.s.clear()


def record_commits(link: Link, cwd: object, target: object, text: str, shas: list[str], *,
                   amend: bool, fallback: str | None = None) -> list[tuple[str, dict]]:
    """Tell the turn's link about a commit call's printed `shas`; (sha, attrs) of each commit row."""
    if not shas:
        return []
    key = commit_repo(link.keys, cwd, target, fallback)
    link.note_commit(key, text, shas)
    return [(sha, {"sha": sha, "repo_key": key, "amend": amend}) for sha in shas]


def pad_pending(act: dict | None) -> None:
    """State upgrade: a pending commit call saved before its target was kept gains target None."""
    for call in ((act or {}).get("pend") or {}).values():
        call[2:] = call[2:] or [None]


def drop_raw_shas(act: dict | None) -> None:
    """State upgrade: a link saved before sha_refs were keyed held raw hex; only the keys stay."""
    link = (act or {}).get("link") or {}
    if link.get("shas"):
        link["shas"] = [v for v in link["shas"] if len(v) == 64]


def pop_pending(pend: dict, tid: str) -> tuple[bool, bool, object]:
    """Take one pending commit call. It rides in the watermark as the JSON list
    [is_amend, printed_before, target]; this is the one place that knows the order."""
    amend, before, target = pend.pop(tid)
    return bool(amend), bool(before), target


def best_effort(errors: Counter, fn: Callable[..., _T], *args: object) -> _T | None:
    """Run one activity step. One that raises is counted by exception type and skipped (None), so
    activity can never block the usage and session rows of the same chunk."""
    try:
        return fn(*args)
    except Exception as exc:
        errors[f"activity:{type(exc).__name__}"] += 1
        return None


def human_event(agent: str, id_parts: tuple, ts: str | None, session: EventSession | None = None,
                project: str | None = None) -> UsageEvent:
    """The zero-token usage row of a prompt a person typed."""
    return UsageEvent(event_id=make_event_id(agent, "human", *id_parts), ts=ts, agent=agent,
                      model=None, provider=None, kind="human", project=project, session=session)


class TurnAgg(TypedDict):
    """One turn's tool aggregates. A plain dict because it rides in the watermark as JSON."""
    calls: int
    errors: int
    hooks: int
    by: dict[str, int]
    last: str | None       # the previous call's tool and input hash
    run: int               # length of the current run of identical calls
    max: int               # longest such run
    mcp_ids: NotRequired[list]   # codex: MCP call ids, to match the item Desktop logs again


def blank_tool_tally() -> TurnAgg:
    return {"calls": 0, "errors": 0, "hooks": 0, "by": {}, "last": None, "run": 0, "max": 0}


def add_call(agg: TurnAgg, name: str, args: object) -> None:
    """Count one tool call; a run of the same tool with the same input feeds max_identical_run."""
    agg["calls"] += 1
    agg["by"][tool_key(name)] = agg["by"].get(tool_key(name), 0) + 1
    key = name + ":" + text_sha256(json.dumps(args, sort_keys=True, default=str))[:16]
    agg["run"] = agg["run"] + 1 if key == agg["last"] else 1
    agg["last"], agg["max"] = key, max(agg["max"], agg["run"])


def turn_attrs(agg: TurnAgg) -> dict[str, Any]:
    top = max(agg["by"].values(), default=0)
    return {
        "tool_calls": agg["calls"], "tool_errors": agg["errors"], "by_tool": dict(agg["by"]),
        "max_identical_run": agg["max"],
        # AgentsView (MIT): a retry loop is 3+ identical calls in a row; a runaway turn is
        # 6+ errors, or 3+ errors while one tool was called 10+ times.
        "retry_flag": agg["max"] >= 3,
        "runaway_flag": agg["errors"] >= 6 or (agg["errors"] >= 3 and top >= 10),
    }
