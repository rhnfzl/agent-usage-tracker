"""Activity helpers shared by the collectors: commit detection and SHA parsing, repo keys, and the
per-turn tool aggregates. attrs built from these carry counts, flags, short enums and hashes only.

The turn aggregates' retry and runaway rules are ported from AgentsView
(github.com/kenn-io/agentsview, MIT licence, (c) its authors).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlparse

from ..state import _state_path

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


def h(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def ms_between(start: object, end: object) -> int | None:
    """Milliseconds from one ISO-8601 timestamp to another; None when either is unusable."""
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return int((b - a).total_seconds() * 1000)
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


def commit_call(cmd: str) -> tuple[bool, bool] | None:
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


def commit_shas(text: str, err: bool, before: bool, structured: str | None = None) -> list[str]:
    """SHAs a commit call printed: git's `[branch sha]` lines; failing that, for a successful
    call only, the agent's structured sha, else the condensed proxy form where it can be the
    commit's own (the first line, or the last `ok <sha>` when an earlier segment may have
    printed)."""
    found = [m["sha"] for m in _COMMIT_OUT.finditer(text)]
    if found or err:
        return found
    if structured:
        return [structured]
    if before:
        return [m["sha"] for m in _OK_LINE.finditer(text)][-1:]
    m = _CONDENSED.match(text.split("\n", 1)[0])
    return [m["sha"]] if m else []


def blank_agg() -> dict:
    return {"calls": 0, "errors": 0, "hooks": 0, "by": {}, "last": None, "run": 0, "max": 0}


def add_call(agg: dict, name: str, args: object) -> None:
    """Count one tool call; a run of the same tool with the same input feeds max_identical_run."""
    agg["calls"] += 1
    agg["by"][tool_key(name)] = agg["by"].get(tool_key(name), 0) + 1
    key = name + ":" + h(json.dumps(args, sort_keys=True, default=str))[:16]
    agg["run"] = agg["run"] + 1 if key == agg["last"] else 1
    agg["last"], agg["max"] = key, max(agg["max"], agg["run"])


def turn_attrs(agg: dict) -> dict:
    top = max(agg["by"].values(), default=0)
    return {
        "tool_calls": agg["calls"], "tool_errors": agg["errors"], "by_tool": dict(agg["by"]),
        "max_identical_run": agg["max"],
        # AgentsView (MIT): a retry loop is 3+ identical calls in a row; a runaway turn is
        # 6+ errors, or 3+ errors while one tool was called 10+ times.
        "retry_flag": agg["max"] >= 3,
        "runaway_flag": agg["errors"] >= 6 or (agg["errors"] >= 3 and top >= 10),
    }
