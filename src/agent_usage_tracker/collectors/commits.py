"""Commit detection: which shell commands run `git commit`, what SHAs they printed, and which repo
they ran in."""

from __future__ import annotations

import os
import re

from ..repo_keys import RepoKeys

_SEGMENTS = re.compile(r"&&|\|\||[;|\n]")
_HEREDOC = re.compile(r"""<<-?\s*(['"]?)(\w+)\1([^\n]*)\n.*?\n\s*\2[ \t]*(?=\n|$)""", re.S)
_ARG = r"""(?:[^\s"']|"[^"]*"|'[^']*')+"""
# optional wrappers (VAR=x, env, command, timeout N, rtk [proxy]), then `git`, then only global
# options (-C <dir>, -c <k=v>, --flag[=v]), then the commit subcommand itself
GIT_COMMIT = re.compile(
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
# git's commit line with its branch and subject, for the link row (both are text: hashed or dropped)
COMMIT_LINE = re.compile(
    r"^\[(?P<br>[^\]\s]+)(?: \(root-commit\))? [0-9a-f]{7,40}\] ?(?P<subj>[^\n]*)", re.M)
_CD = re.compile(rf"^\s*[({{]?\s*(?:cd|pushd)\s+({_ARG})\s*$")
_DASH_C = re.compile(rf"\s-C\s+({_ARG})")


def commit_call(cmd: str) -> tuple[bool, bool] | None:
    """(is_amend, printed_before) when a command segment runs `git commit` (not --dry-run);
    printed_before: a segment ahead of the commit may have printed, so the output's first line is
    not necessarily the commit's."""
    # a heredoc body is data (a file, a commit message), not commands: drop it before splitting
    segs = [x for x in _SEGMENTS.split(_HEREDOC.sub(r"\3", cmd)) if x.strip()]
    hits = [i for i, x in enumerate(segs) if GIT_COMMIT.match(x) and "--dry-run" not in x.split()]
    if not hits:
        return None
    return (any("--amend" in segs[i].split() for i in hits),
            not all(_SILENT.match(x) for x in segs[:hits[0]]))


def commit_args_at(cmd: str) -> int | None:
    """Where the arguments of the segment commit_call found start in `cmd`. Heredoc bodies are
    blanked, not dropped, so the offset also holds in the original text."""
    text = _HEREDOC.sub(lambda m: " " * (m.start(3) - m.start()) + m[3]
                        + " " * (m.end() - m.end(3)), cmd)
    starts = [0, *(m.end() for m in _SEGMENTS.finditer(text))]
    for at, x in zip(starts, _SEGMENTS.split(text)):
        g = GIT_COMMIT.match(x)
        if g and "--dry-run" not in x.split():
            return at + g.end() - 1     # the space GIT_COMMIT ate; a -m search needs it
    return None


def commit_dir(cmd: str) -> str | None:
    """Where the first commit call runs when the command moves there itself (`cd X && git commit`,
    `git -C X commit`), relative to the record's cwd unless absolute; None when it does not move
    or the move is not knowable (a variable, a command substitution)."""
    d = None
    for x in [x for x in _SEGMENTS.split(_HEREDOC.sub(r"\3", cmd)) if x.strip()]:
        m, g = _CD.match(x), GIT_COMMIT.match(x)
        # a cd moves the shell for the segments after it; a -C only the commit itself
        if m:
            args = [m[1]]
        elif g and "--dry-run" not in x.split():
            args = [c[1] for c in _DASH_C.finditer(g[0])]
        else:
            continue
        for arg in args:
            if "$" in arg or "`" in arg:
                return None
            d = os.path.join(d or "", os.path.expanduser(arg.strip("\"'")))
        if g:
            return d
    return None


def commit_repo(keys: RepoKeys, cwd: object, target: object, fallback: str | None) -> str | None:
    """A commit's repo_key: the directory the command moved to, else the record's cwd, else
    `fallback` (the worktree's original checkout, for a cwd that is gone). A relative target
    with no cwd is not resolved: it would be relative to aut's own directory."""
    if isinstance(target, str) and (isinstance(cwd, str) or os.path.isabs(target)):
        k = keys.of_cwd(os.path.join(cwd, target) if isinstance(cwd, str) else target)
        if k:
            return k
    return keys.of_cwd(cwd) or fallback


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
