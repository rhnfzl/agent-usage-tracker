"""What a local clone says about a commit, read with git (and, for squash merges, gh).

The read side of `aut outcomes`: finding the clones under outcome_repo_roots, fetching them, and
reading landings, patch-id twins, reverts and surviving lines. Every read says when git failed, so
a caller never mistakes a failed read for an empty answer. Clone paths stay in memory for the run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess  # nosec B404 # argv lists only, no shell in this module
import tempfile
import time
from dataclasses import dataclass, field
from glob import escape as glob_escape
from glob import glob
from typing import Any, NamedTuple

from .git_safe import git_argv, git_env
from .repo_keys import RepoKeys, repo_identity

DAY = 86400
DECIDE_S = 14 * DAY          # undecided until day 14; also the revert and survival window
FETCH_TIMEOUT_S = 30
PATCH_ID_TIMEOUT_S = 600     # one clone's `git log -p | git patch-id`; a giant repo takes minutes
BLAME_FILES = 30             # survival blames at most this many files per commit
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git", ".tox"}
_MAX_DEPTH = 6


def run_git(path: str, *args: str, inp: str | None = None, timeout: float = 300) -> str | None:
    """stdout of a git command, or None when it fails or times out."""
    try:
        r = subprocess.run(  # nosec B603 # no shell; refs and paths are vetted by the callers
            git_argv(path, *args), input=inp, capture_output=True, text=True,
            errors="replace", timeout=timeout, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


@dataclass(slots=True)
class Clone:
    path: str                 # a worktree of the repo (held in memory only)
    key: str | None           # repo_key of its origin
    github_slug: str | None   # owner/repo when origin is on github.com (for gh)
    default: str | None       # origin/HEAD's target ref, or an origin main/master/develop
    fetched_at: float = 0.0   # when origin was last fetched (FETCH_HEAD, or this run's fetch)
    readable: bool = True     # False: git failed reading its refs, so `default` says nothing


def _default_ref(path: str) -> tuple[str | None, bool]:
    """(origin's default branch, whether git answered); never a local branch, so unpushed work is
    never merged. for-each-ref exits 0 for an absent ref, so a None from it is a real failure.
    A clone's origin/HEAD can name anything: `ref: --ext-diff` would reach git log as a flag and run
    a repo-defined diff driver, so a target outside origin's refs is ignored."""
    head, *fallbacks = ("refs/remotes/origin/HEAD", "refs/remotes/origin/main",
                        "refs/remotes/origin/master", "refs/remotes/origin/develop")
    out = run_git(path, "for-each-ref", "--format=%(refname) %(symref)", head, *fallbacks)
    if out is None:
        return None, False
    refs = dict(line.partition(" ")[::2] for line in out.splitlines())
    target = refs.get(head, "")
    if target.startswith("refs/remotes/origin/"):
        return target, True
    return next((r for r in fallbacks if r in refs), None), True


def _common_dir(path: str) -> str | None:
    """The git common dir of the .git at path; "" when git says no repository is there (a stale
    .git holds nothing), None when git failed, so a repository there may hold commits."""
    try:
        r = subprocess.run(  # nosec B603 # constant args after -C path, no shell
            git_argv(path, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            capture_output=True, text=True, errors="replace", timeout=10,
            env={**git_env(), "LC_ALL": "C"})    # the message below is translated
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return r.stdout.strip()
    return "" if "not a git repository" in r.stderr else None


def _origin_ids(url: str, keys: RepoKeys) -> tuple[str | None, str | None]:
    """(repo_key, owner/repo when on github.com) of origin's URL; neither without a usable one."""
    if not url:
        return None, None
    try:
        ident = repo_identity(url)
    except ValueError:
        return None, None
    return keys.of(ident), (ident.removeprefix("github.com/")
                            if ident.startswith("github.com/") else None)


def _origin_url(path: str) -> str | None:
    """origin's URL, "" for a clone without one, None when git failed to say."""
    remotes = run_git(path, "remote", timeout=10)
    if remotes is None or "origin" not in remotes.split():
        return None if remotes is None else ""
    return run_git(path, "remote", "get-url", "origin", timeout=10)


def _clone_at(path: str, common: str, keys: RepoKeys) -> Clone:
    url = _origin_url(path)
    key, slug = _origin_ids((url or "").strip(), keys)
    heads = glob(os.path.join(glob_escape(common), "FETCH_HEAD")) + glob(
        os.path.join(glob_escape(common), "worktrees", "*", "FETCH_HEAD"))
    default, readable = _default_ref(path)
    return Clone(path, key, slug, default, max((os.path.getmtime(h) for h in heads), default=0.0),
                 readable and url is not None)


def find_clones(roots: list[str], keys: RepoKeys) -> list[Clone]:
    """One Clone per repository (worktrees of one repo share a git common dir). A repository git
    could not identify is an unreadable Clone: dropping it would make its commits look gone."""
    seen: set[str] = set()
    out: list[Clone] = []
    for root in roots:
        base = os.path.expanduser(root)
        for dirpath, dirnames, filenames in os.walk(base):
            depth = dirpath[len(base):].count(os.sep)
            if ".git" in dirnames or ".git" in filenames:
                common = _common_dir(dirpath)
                if common is None:
                    out.append(Clone(dirpath, None, None, None, readable=False))
                elif common and os.path.realpath(common) not in seen:
                    seen.add(os.path.realpath(common))
                    out.append(_clone_at(dirpath, common, keys))
            dirnames[:] = [] if depth >= _MAX_DEPTH else \
                [n for n in dirnames if n not in _SKIP_DIRS]
    return out


def _fetch(path: str) -> bool:
    """Fetch origin's refs and nothing else: no gc or maintenance (it could prune objects later
    runs need), no FETCH_HEAD, no submodules, no prompt. On timeout SIGTERM the process group so
    git removes its lock files, then SIGKILL."""
    cmd = git_argv(path, "fetch", "--quiet", "--no-tags", "--no-auto-maintenance",
                   "--no-write-fetch-head", "--no-recurse-submodules", "origin")
    try:
        p = subprocess.Popen(  # nosec B603 # constant args after -C path, no shell
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, env=git_env())
    except OSError:
        return False
    try:
        return p.wait(timeout=FETCH_TIMEOUT_S) == 0
    except subprocess.TimeoutExpired:
        # Windows has no process groups or SIGKILL: there kill() ends git itself
        kill = (lambda s: os.killpg(p.pid, s)) if hasattr(os, "killpg") else (lambda s: p.kill())
        for sig, wait in ((signal.SIGTERM, 5), (getattr(signal, "SIGKILL", signal.SIGTERM), None)):
            try:
                kill(sig)
                p.wait(timeout=wait)
                break
            except (OSError, subprocess.TimeoutExpired):
                pass        # already gone, or it ignored SIGTERM: on to SIGKILL
        return False


def fetch_clones(clones: list[Clone]) -> tuple[int, int]:
    """`git fetch` each clone, FETCH_TIMEOUT_S each; (ok, failed). A failure is skipped."""
    ok = failed = 0
    for c in clones:
        if _fetch(c.path):
            c.fetched_at = time.time()
            ok += 1
        else:
            failed += 1
    return ok, failed


def resolve_shas(clone: Clone, shas: list[str]) -> dict[str, ResolvedCommit] | None:
    """sha (as stored, possibly abbreviated) -> its full sha and committer time, for those it
    holds; None when git failed."""
    out = run_git(clone.path, "cat-file", "--batch-check",
               inp="".join(f"{s}^{{commit}}\n" for s in shas))
    if out is None:
        return None
    full = {}
    for s, line in zip(shas, out.splitlines()):
        p = line.split()
        if len(p) == 3 and p[1] == "commit":
            full[s] = p[0]
    if not full:
        return {}
    log = run_git(clone.path, "log", "--no-walk=unsorted", "--stdin", "--format=%H %ct",
               inp="".join(f"{f}\n" for f in set(full.values())))
    if log is None:
        return None
    ct = dict(line.split() for line in log.splitlines() if line.strip())
    return {s: ResolvedCommit(f, int(ct[f])) for s, f in full.items() if f in ct}


def _patch_ids(path: str, args: list[str], inp: str | None = None) -> dict[str, str] | None:
    """full sha -> stable patch id for the commits `git log -p <args>` prints; None when git failed
    or took over PATCH_ID_TIMEOUT_S, and then neither process stays behind."""
    procs: list[subprocess.Popen[Any]] = []
    try:
        with tempfile.TemporaryFile() as revs:  # a file, so writing the revs can never block on git
            revs.write((inp or "").encode())
            revs.seek(0)
            log = subprocess.Popen(  # nosec B603 # no shell; args are flags and a vetted default ref
                git_argv(path, "log", "-p", "--format=commit %H", *args),
                stdin=revs, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_env())
        procs.append(log)
        pid = subprocess.Popen(  # nosec B603 # constant args after -C path, no shell
            git_argv(path, "patch-id", "--stable"), stdin=log.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=git_env())
        procs.append(pid)
        if log.stdout:     # always set with stdout=PIPE; the check narrows its type
            log.stdout.close()
        out, _ = pid.communicate(timeout=PATCH_ID_TIMEOUT_S)
        log.wait(timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
    if log.returncode or pid.returncode:
        return None
    return {c: p for p, c in (line.split() for line in out.splitlines() if line.strip())}


@dataclass(slots=True)
class CloneRead:
    """What one clone says about its anchors, read once per run. Not ok: a git read failed, so
    nothing here may decide a commit."""
    ok: bool = False
    tip_t: int = 0
    on_default: dict[str, int] = field(default_factory=dict)  # commit -> when it landed
    on_branch: set[str] = field(default_factory=set)
    twin: dict[str, str] = field(default_factory=dict)        # off-default full sha -> twin on default
    reverted: dict[str, int] = field(default_factory=dict)    # reverted sha -> earliest revert time

    def revert_time(self, full: str) -> int | None:
        hits = [t for x, t in self.reverted.items() if full.startswith(x)]
        return min(hits) if hits else None


def _landings(log: str) -> tuple[int, dict[str, int]]:
    """From `git log --format='%H %ct %P' <default>`: (tip time, commit -> when it landed = committer
    time of the first-parent commit that brought it in; oldest first, so each commit gets its first
    landing)."""
    par, ct = {}, {}
    for line in log.splitlines():
        h, t, *ps = line.split()
        par[h], ct[h] = ps, int(t)
    tip = log.split(None, 1)[0] if log else None
    fp: list[str] = []
    at = tip
    while at in par:
        fp.append(at)
        at = par[at][0] if par[at] else None
    on_default: dict[str, int] = {}
    for f in reversed(fp):
        stack = [f]
        while stack:
            x = stack.pop()
            if x not in on_default:
                on_default[x] = ct[f]
                stack.extend(q for q in par.get(x, ()) if q in par)
    return (ct[tip] if tip else 0), on_default


def _twins(path: str, default: str, since: int, off: list[str]) -> dict[str, str] | None:
    """off-default full sha -> the commit on default with its patch id; None when git failed."""
    on_default = _patch_ids(path, ["--no-merges", f"--since=@{since}", default])
    theirs = _patch_ids(path, ["--no-walk=unsorted", "--stdin"], "".join(f"{f}\n" for f in off))
    if on_default is None or theirs is None:
        return None
    by_pid = {p: c for c, p in on_default.items()}
    return {c: by_pid[p] for c, p in theirs.items() if p in by_pid and by_pid[p] != c}


def _reverted(log: str) -> dict[str, int]:
    """From `git log --format=%ct%n%B%x00`: reverted sha -> its earliest revert time."""
    reverted: dict[str, int] = {}
    for chunk in log.split("\0"):
        head, _, body = chunk.strip().partition("\n")
        for x in _REVERTS.findall(body) if head.isdigit() else ():
            reverted[x] = min(reverted.get(x, int(head)), int(head))
    return reverted


def read_clone(clone: Clone, since: int, off_default: list[str]) -> CloneRead:
    d, path = clone.default, clone.path
    log = run_git(path, "log", "--format=%H %ct %P", d) if d else ""
    branches = run_git(path, "rev-list", "--branches", "--remotes", *([f"^{d}"] if d else []))
    rv = run_git(path, "log", "--branches", "--remotes", f"--since=@{since}",
              "--grep=This reverts commit", "--format=%ct%n%B%x00")
    if not clone.readable or log is None or branches is None or rv is None:
        return CloneRead()
    tip_t, on_default = _landings(log)
    off = [f for f in off_default if f not in on_default]
    twin = _twins(path, d, since, off) if d and off else {}
    if twin is None:
        return CloneRead()
    return CloneRead(True, tip_t, on_default, set(branches.split()), twin, _reverted(rv))


class GitFailed(Exception):
    """A git read failed, so survival was not measured (which is not the same as 0 lines alive)."""


def _added_files(numstat: str) -> list[tuple[int, str]]:
    """(lines added, path) from `git show --numstat -z`, the first BLAME_FILES that added any."""
    files = []
    for item in numstat.split("\0"):
        a, _, rest = item.strip("\n").partition("\t")
        f = rest.partition("\t")[2]
        if a.isdigit() and int(a) > 0 and f:
            files.append((int(a), f))
    return files[:BLAME_FILES]


def _lines_alive(path: str, base: str, target: str, file: str) -> int:
    """How many lines of file at base git blame attributes to target."""
    bl = run_git(path, "blame", "--porcelain", base, "--", file, timeout=120)
    if bl is not None:
        return sum(1 for line in bl.splitlines() if line.startswith(target + " "))
    # blame also fails on a path that is no file at base: that one really has 0 lines alive
    tree = run_git(path, "ls-tree", base, "--", file)
    if tree is None or " blob " in tree.partition("\t")[0]:
        raise GitFailed
    return 0


def survival(clone: Clone, target: str, merge_t: float) -> float | None:
    """Share of target's added lines still attributed to it on the default branch 14 days after
    the merge (first BLAME_FILES files). None for a merge or root commit or a commit adding no
    lines. Raises GitFailed when git could not answer."""
    path, default = clone.path, clone.default
    if not default:
        raise GitFailed
    parents = run_git(path, "rev-list", "--parents", "-n", "1", target)
    if parents is None:
        raise GitFailed
    if len(parents.split()) != 2:
        return None
    base = run_git(path, "rev-list", "-1", "--first-parent", f"--before=@{int(merge_t + DECIDE_S)}",
                default)
    raw = run_git(path, "show", "--numstat", "-z", "--no-renames", "--format=", target)
    if base is None or raw is None:
        raise GitFailed
    base = base.strip()
    if not base:
        return None
    files = _added_files(raw)
    added = sum(a for a, _ in files)
    if not added:
        return None
    alive = sum(_lines_alive(path, base, target, f) for _, f in files)
    return round(min(1.0, alive / added), 4)


def gh_merged(github_slug: str, sha: str) -> bool | None:
    """True when a merged PR of github_slug contains sha; None when gh is missing or fails."""
    if not shutil.which("gh"):
        return None
    try:
        r = subprocess.run(  # nosec B603, B607 # no shell; slug and sha are values of their flags
            ["gh", "pr", "list", "--repo", github_slug, "--state", "merged", "--search",
             sha, "--json", "number", "--limit", "1"], capture_output=True,
            text=True, timeout=FETCH_TIMEOUT_S)
        return bool(json.loads(r.stdout)) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class ResolvedCommit(NamedTuple):
    full: str
    committed: int
