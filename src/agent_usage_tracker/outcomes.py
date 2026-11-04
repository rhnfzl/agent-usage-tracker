"""`aut outcomes`: what became of each commit an agent made, read from the local clones.

Reads the commit rows (agents.activity_event kind commit) and the git clones under
outcome_repo_roots, and writes agents.commit_outcome rows: one per change of state, never a URL,
path, subject or file name. repo_key -> clone is held in memory for the run only.

States (a commit moves through them by new rows; readers take the latest):
  merged      ancestor of origin/HEAD, a single-commit patch-id twin on it, or (network on) a
              merged GitHub PR found by gh; survival = share of its added lines still there
              14 days after the merge
  reverted    "This reverts commit X" for it or its twin within 14 days of the merge
  reworked    merged, but under half of its added lines survive 14 days
  open        reachable from a branch only
  abandoned   unreachable, no twin, no merged PR
  superseded  a pre-amend SHA (unreachable, and the session's next commit was an amend)
  undecided   younger than 14 days (from the merge, or from the commit row when not merged), or
              not decidable this run (default ref older than merge + 14 days, gh failed, a git
              read of the clone failed)
A run that cannot see or decide a commit never replaces its stored state.

`aut outcome <shipped|ruled-out|open>`: the manual outcome of the newest interactive session of
the current project, written to agents.manual_outcome.
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
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from glob import escape as glob_escape
from glob import glob
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple, NotRequired, TypedDict

from .collectors import claude_code
from .config import AutError, Config
from .git_safe import git_argv, git_env
from .push import deliver_scan_rows, scan_row, scrub_error
from .repo_keys import RepoKeys, repo_identity
from .sink import connect, write_many
from .state import state_path, atomic_write_text, load_json_dict

DAY = 86400
DECIDE_S = 14 * DAY          # undecided until day 14; also the revert and survival window
FETCH_TIMEOUT_S = 30
PATCH_ID_TIMEOUT_S = 600     # one clone's `git log -p | git patch-id`; a giant repo takes minutes
BLAME_FILES = 30             # survival blames at most this many files per commit
CACHE_NAME = "outcomes_cache.json"
_CACHE_V = 2                 # 2: a failed git read no longer counts as a survival score
_GH_STABLE_S = 60 * DAY      # a "no merged PR" answer is cached once the commit is this old
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git", ".tox"}
_MAX_DEPTH = 6
_WORKERS = 8
MANUAL = {"shipped": "shipped", "ruled-out": "ruled_out", "open": "open"}

OutcomeState = Literal["merged", "reverted", "reworked", "open", "abandoned", "superseded",
                       "undecided"]


class Outcome(TypedDict):
    sha: str
    repo_key: str
    state: OutcomeState
    survival: float | None
    sticky: NotRequired[bool]     # this run could not see or decide it: keep any stored state


def _git(path: str, *args: str, inp: str | None = None, timeout: float = 300) -> str | None:
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
    gh: str | None            # owner/repo when origin is on github.com (for gh)
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
    out = _git(path, "for-each-ref", "--format=%(refname) %(symref)", head, *fallbacks)
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


def _clone_at(path: str, common: str, keys: RepoKeys) -> Clone:
    url = (_git(path, "remote", "get-url", "origin", timeout=10) or "").strip()
    key, gh = _origin_ids(url, keys)
    heads = glob(os.path.join(glob_escape(common), "FETCH_HEAD")) + glob(
        os.path.join(glob_escape(common), "worktrees", "*", "FETCH_HEAD"))
    default, readable = _default_ref(path)
    return Clone(path, key, gh, default, max((os.path.getmtime(h) for h in heads), default=0.0),
                 readable)


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


def _lookup(clone: Clone, shas: list[str]) -> dict[str, _Hit] | None:
    """sha (as stored, possibly abbreviated) -> its full sha and committer time, for those it
    holds; None when git failed."""
    out = _git(clone.path, "cat-file", "--batch-check",
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
    log = _git(clone.path, "log", "--no-walk=unsorted", "--stdin", "--format=%H %ct",
               inp="".join(f"{f}\n" for f in set(full.values())))
    if log is None:
        return None
    ct = dict(line.split() for line in log.splitlines() if line.strip())
    return {s: _Hit(f, int(ct[f])) for s, f in full.items() if f in ct}


def _patch_ids(path: str, args: list[str], inp: str | None = None) -> dict[str, str] | None:
    """full sha -> stable patch id for the commits `git log -p <args>` prints; None when git failed
    or took over PATCH_ID_TIMEOUT_S, and then neither process stays behind."""
    procs: list[subprocess.Popen] = []
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
        if log.stdout:
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
class _Repo:
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


def _read_repo(clone: Clone, since: int, off_default: list[str]) -> _Repo:
    d, path = clone.default, clone.path
    log = _git(path, "log", "--format=%H %ct %P", d) if d else ""
    branches = _git(path, "rev-list", "--branches", "--remotes", *([f"^{d}"] if d else []))
    rv = _git(path, "log", "--branches", "--remotes", f"--since=@{since}",
              "--grep=This reverts commit", "--format=%ct%n%B%x00")
    if not clone.readable or log is None or branches is None or rv is None:
        return _Repo()
    tip_t, on_default = _landings(log)
    off = [f for f in off_default if f not in on_default]
    twin = _twins(path, d, since, off) if d and off else {}
    if twin is None:
        return _Repo()
    return _Repo(True, tip_t, on_default, set(branches.split()), twin, _reverted(rv))


class _GitFailed(Exception):
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
    bl = _git(path, "blame", "--porcelain", base, "--", file, timeout=120)
    if bl is not None:
        return sum(1 for line in bl.splitlines() if line.startswith(target + " "))
    # blame also fails on a path that is no file at base: that one really has 0 lines alive
    tree = _git(path, "ls-tree", base, "--", file)
    if tree is None or " blob " in tree.partition("\t")[0]:
        raise _GitFailed
    return 0


def _survival(clone: Clone, target: str, merge_t: float) -> float | None:
    """Share of target's added lines still attributed to it on the default branch 14 days after
    the merge (first BLAME_FILES files). None for a merge or root commit or a commit adding no
    lines. Raises _GitFailed when git could not answer."""
    path, default = clone.path, clone.default
    if not default:
        raise _GitFailed
    parents = _git(path, "rev-list", "--parents", "-n", "1", target)
    if parents is None:
        raise _GitFailed
    if len(parents.split()) != 2:
        return None
    base = _git(path, "rev-list", "-1", "--first-parent", f"--before=@{int(merge_t + DECIDE_S)}",
                default)
    raw = _git(path, "show", "--numstat", "-z", "--no-renames", "--format=", target)
    if base is None or raw is None:
        raise _GitFailed
    base = base.strip()
    if not base:
        return None
    files = _added_files(raw)
    added = sum(a for a, _ in files)
    if not added:
        return None
    alive = sum(_lines_alive(path, base, target, f) for _, f in files)
    return round(min(1.0, alive / added), 4)


def _gh_merged(repo: str, sha: str) -> bool | None:
    """True when a merged PR of repo contains sha; None when gh is missing or fails."""
    if not shutil.which("gh"):
        return None
    try:
        r = subprocess.run(  # nosec B603, B607 # no shell; repo and sha are values of their flags
            ["gh", "pr", "list", "--repo", repo, "--state", "merged", "--search",
             sha, "--json", "number", "--limit", "1"], capture_output=True,
            text=True, timeout=FETCH_TIMEOUT_S)
        return bool(json.loads(r.stdout)) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class _Hit(NamedTuple):
    full: str
    committed: int


class _Candidate(NamedTuple):
    clone: int                # index into the run's clones
    full: str


class _Pick(NamedTuple):
    clone: Clone
    repo: _Repo
    full: str


@dataclass(slots=True)
class _Anchor:
    """One (sha, repo_key) of the commit rows."""
    sha: str
    key: str | None
    ts: float
    amended: bool = False
    pick: _Pick | None = None     # the clone that knows most of it
    unread: bool = False          # a clone that might know more could not be read


class _SurvivalJob(NamedTuple):
    o: Outcome
    clone: Clone
    target: str
    merge_t: float


class _GhJob(NamedTuple):
    o: Outcome
    repo: str
    full: str
    ts: float
    on_branch: bool


def _anchors(rows: list[dict], stats: Counter) -> list[_Anchor]:
    """The distinct commits of the rows, a pre-amend one marked as amended."""
    anchors: dict[tuple[str, str | None], _Anchor] = {}
    by_session = defaultdict(list)
    for r in rows:
        sha = (r["sha"] or "").lower()
        if not _SHA.match(sha):
            stats["junk_sha"] += 1
            continue
        a = anchors.setdefault((sha, r["repo_key"]), _Anchor(sha, r["repo_key"], r["ts"]))
        a.ts = min(a.ts, r["ts"])
        by_session[r["session_id"]].append((r["ts"], r["amend"], a))
    for seq in by_session.values():
        seq.sort(key=lambda x: (x[0], bool(x[1]), x[2].sha))   # an amend sorts after a tie
        for (_, _, a), (_, amend, _) in zip(seq, seq[1:]):
            a.amended |= bool(amend)
    stats["anchors"] = len(anchors)
    return list(anchors.values())


def _candidates(a: _Anchor, readable: dict[int, dict[str, _Hit]], failed: list[int],
                clones: list[Clone]) -> list[_Candidate]:
    """The clones that may hold the anchor, its own repo's first; marks it `unread` when a clone
    that might know more could not be read."""
    cands = [_Candidate(i, h[a.sha].full) for i, h in readable.items()
             if a.sha in h and abs(h[a.sha].committed - a.ts) <= DAY]
    own = [c for c in cands if a.key and clones[c.clone].key == a.key]
    a.unread = any(not own or clones[i].key == a.key for i in failed)
    return own or sorted(cands, key=lambda c: clones[c.clone].key is None)


def _advance(rp: _Repo, full: str) -> int:
    """How far along a commit is in one clone: merged < open < unreachable."""
    return 0 if full in rp.on_default or full in rp.twin else 1 if full in rp.on_branch else 2


def _resolve(anchors: list[_Anchor], clones: list[Clone], stats: Counter) -> None:
    """Point each anchor at the clone where it is furthest along, and mark the ones a failed git
    read could change."""
    shas = sorted({a.sha for a in anchors})
    held = {i: _lookup(c, shas) for i, c in enumerate(clones)} if shas else {}
    failed = [i for i, h in held.items() if h is None]
    readable = {i: h for i, h in held.items() if h is not None}
    found: list[tuple[_Anchor, list[_Candidate]]] = []
    per_clone: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for a in anchors:
        cands = _candidates(a, readable, failed, clones)
        if cands:
            found.append((a, cands))
            for i, full in cands:
                per_clone[i].append((full, readable[i][a.sha].committed))
        else:
            stats["not_found"] += 1
    repos = {i: _read_repo(clones[i], int(min(t for _, t in got)) - 2 * DAY, [f for f, _ in got])
             for i, got in per_clone.items()}
    for a, cands in found:
        best = min(cands, key=lambda c: _advance(repos[c.clone], c.full))
        a.pick = _Pick(clones[best.clone], repos[best.clone], best.full)
        a.unread |= not all(repos[j].ok for j, _ in cands)
        stats["found"] += 1
        stats["null_key_resolved"] += a.key is None and clones[best.clone].key is not None


def _decide_merged(o: Outcome, pick: _Pick, target: str, now_ts: float, cache: dict,
                   stats: Counter) -> tuple[Outcome, _SurvivalJob | None]:
    """The outcome of a commit (or its twin) on the default branch, once 14 days have passed."""
    c, rp, full = pick
    merge_t = rp.on_default[target]            # when it landed on the default branch
    rv = rp.revert_time(full) or rp.revert_time(target)
    if now_ts - merge_t < DECIDE_S:
        return o, None
    if rv is not None and rv - merge_t <= DECIDE_S:
        o["state"] = "reverted"
        return o, None
    if target in cache or max(rp.tip_t, c.fetched_at) >= merge_t + DECIDE_S:
        o["state"] = "merged"
        return o, _SurvivalJob(o, c, target, merge_t)
    # shortcut: the default ref was last updated before merge + 14 d, so survival
    # cannot be measured yet; a repo nobody fetches stays undecided. Upgrade with a
    # per-repo "inactive" signal if that holds months back.
    o["sticky"] = True
    stats["stale_default"] += 1
    return o, None


def _decide_unmerged(o: Outcome, a: _Anchor, pick: _Pick, young: bool, cache: dict,
                     gh: bool) -> tuple[Outcome, _GhJob | None]:
    """The outcome of a commit off the default branch."""
    c, rp, full = pick
    on_branch = full in rp.on_branch
    if not on_branch and a.amended:
        o["state"] = "superseded"
    elif not young:
        # a squash-merged PR leaves its commits off the default branch; a cached answer needs no gh
        if f"gh:{full}" in cache or (gh and c.gh):
            return o, _GhJob(o, c.gh or "", full, a.ts, on_branch)
        o["state"] = "open" if on_branch else "abandoned"
    return o, None


def _decide(a: _Anchor, now_ts: float, cache: dict, gh: bool,
            stats: Counter) -> tuple[Outcome, _SurvivalJob | _GhJob | None]:
    """The anchor's outcome, and the job that finishes it if one is needed. Returning with the
    state untouched means undecided."""
    o: Outcome = {"sha": a.sha, "repo_key": a.key or "", "state": "undecided", "survival": None}
    if a.pick:
        o["repo_key"] = a.key or a.pick.clone.key or ""
    if a.unread:                  # git could not answer: decide nothing, keep what is stored
        o["sticky"] = True
        stats["repo_unreadable"] += 1
        return o, None
    young = now_ts - a.ts < DECIDE_S
    if a.pick is None:
        # shortcut: a commit no local clone holds counts as abandoned after day 14; right for
        # work never pushed, wrong for a repo not cloned on this machine. Upgrade with an
        # 'unknown' state in commit_outcome (and the linker) if that share grows. sticky:
        # a commit that later goes missing (clone moved, pre-amend SHA pruned) keeps its state.
        o["state"], o["sticky"] = ("undecided" if young else "abandoned"), True
        stats["abandoned_not_found"] += not young
        return o, None
    _c, rp, full = a.pick
    target = full if full in rp.on_default else rp.twin.get(full)
    if target:
        return _decide_merged(o, a.pick, target, now_ts, cache, stats)
    return _decide_unmerged(o, a, a.pick, young, cache, gh)


def _ask_gh(job: _GhJob, now_ts: float, cache: dict) -> tuple[int, int]:
    """Settle a commit off the default branch by its PR; (gh lookups, gh failures)."""
    o, repo, full, ts, on_branch = job
    k = f"gh:{full}"
    looked = 0
    merged: bool | None
    if k in cache:
        merged = bool(cache[k])
    else:
        merged = _gh_merged(repo, full)
        looked = 1
        if merged is None:          # gh could not answer: decide nothing, keep what is stored
            o["sticky"] = True
            return 1, 1
        if merged or now_ts - ts >= _GH_STABLE_S:
            cache[k] = merged       # a merge never undoes itself; an old "no" stays no
    o["state"] = "merged" if merged else "open" if on_branch else "abandoned"
    return looked, 0


def _measure(job: _SurvivalJob, cache: dict) -> str:
    """Fill in a merged commit's survival; the stat to count."""
    o, c, target, merge_t = job
    fresh = target not in cache
    if fresh:
        try:
            cache[target] = _survival(c, target, merge_t)
        except _GitFailed:          # not cached: the next run measures it again
            o["state"], o["sticky"] = "undecided", True
            return "survival_failed"
    o["survival"] = cache[target]
    if o["survival"] is not None and o["survival"] < 0.5:
        o["state"] = "reworked"
    return "survival_computed" if fresh else "survival_cached"


def evaluate(rows: list[dict], clones: list[Clone], now_ts: float, cache: dict,
             gh: bool = False) -> tuple[list[Outcome], Counter]:
    """One outcome per (sha, repo_key) of the commit rows, as of the epoch now_ts. rows: sha, repo_key, ts (epoch),
    session_id, amend. cache: full sha -> survival (filled in place); gh answers sit under 'gh:', its version under 'v'."""
    stats: Counter = Counter(rows=len(rows))
    anchors = _anchors(rows, stats)
    _resolve(anchors, clones, stats)
    outs, jobs, gh_jobs = [], [], []
    for a in anchors:
        o, job = _decide(a, now_ts, cache, gh, stats)
        outs.append(o)
        if isinstance(job, _GhJob):
            gh_jobs.append(job)
        elif job:
            jobs.append(job)
    with ThreadPoolExecutor(_WORKERS) as pool:     # tallies stay on this thread
        for looked, failed in pool.map(lambda j: _ask_gh(j, now_ts, cache), gh_jobs):
            stats["gh_lookups"] += looked
            stats["gh_failed"] += failed
        for stat in pool.map(lambda j: _measure(j, cache), jobs):
            stats[stat] += 1
    # a NULL-key row resolved to a clone can repeat a keyed row of the same commit: keep one
    outs = list({(o["sha"], o["repo_key"]): o for o in reversed(outs)}.values())[::-1]
    return outs, stats


def pending(outs: list[Outcome], stored: dict[tuple[str, str], tuple]) -> list[Outcome]:
    """The outcomes whose (state, survival) differs from the latest stored row. A sticky outcome
    (this run could not see the commit or decide it) never replaces a stored row of its sha,
    under any repo_key: readers match on the sha alone."""
    have = {sha for sha, _ in stored}
    return [o for o in outs if not (o.get("sticky") and o["sha"] in have)
            and stored.get((o["sha"], o["repo_key"])) != (o["state"], o["survival"])]


def _cache_path() -> Path:
    return state_path().parent / CACHE_NAME


def _load_cache() -> dict:
    """The cache; survivals of an older version are dropped, so they are measured again."""
    cache = load_json_dict(_cache_path())
    if cache.get("v") != _CACHE_V:
        cache = {k: v for k, v in cache.items() if k.startswith("gh:")}
        cache["v"] = _CACHE_V
    return cache


def _load_rows(dsn: str) -> list[dict]:
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT session_id, ts, attrs->>'sha', attrs->>'repo_key', "
            "coalesce((attrs->>'amend')::boolean, false) "
            "FROM agents.activity_event WHERE kind = 'commit'").fetchall()
    return [{"session_id": s, "ts": ts.timestamp(), "sha": sha, "repo_key": k, "amend": am}
            for s, ts, sha, k, am in rows]


def _load_stored(dsn: str) -> dict[tuple[str, str], tuple]:
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (sha, repo_key) sha, repo_key, state, survival "
            "FROM agents.commit_outcome ORDER BY sha, repo_key, decided_at DESC").fetchall()
    return {(sha, k): (st, None if sv is None else round(float(sv), 4))
            for sha, k, st, sv in rows}


def _insert(dsn: str, rows: list[dict]) -> None:
    write_many(dsn, "INSERT INTO agents.commit_outcome (sha, repo_key, state, survival, decided_at) "
               "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
               [(r["sha"], r["repo_key"], r["state"], r["survival"], r["decided_at"])
                for r in rows])


def run_outcomes(cfg: Config, *, refresh: bool, dry_run: bool,
                 now: datetime | None = None) -> dict[str, Any]:
    """Score every commit row and write the changed states. refresh (--fetch, outcome_fetch) git
    fetches the clones and turns on gh. A dry run reads the sink and the clones as they are and
    changes nothing: no rows, no cache, no heartbeat, no fetch, no gh, even with refresh."""
    dsn = cfg.require_pg("outcomes (it reads the commit rows)")
    started = datetime.now(timezone.utc)
    decided = (now or started).astimezone(timezone.utc)
    written, status, error, interrupted = 0, "ok", None, False
    try:
        rows = _load_rows(dsn)
        clones = find_clones(list(cfg.outcome_repo_roots), RepoKeys(cfg.repo_key_salt))
        stats: dict[str, Any] = {"clones": len(clones)}
        if refresh and dry_run:
            stats["fetch_skipped_dry_run"] = 1
        refresh = refresh and not dry_run
        if refresh:
            # only repos the agents committed to: fetch talks to the remote, so a clone that
            # merely sits under a root (and holds a matching short SHA) is never fetched
            keys = {r["repo_key"] for r in rows if r["repo_key"]}
            want = [c for c in clones if c.key in keys]
            stats["fetch_ok"], stats["fetch_failed"] = fetch_clones(want)
        cache = _load_cache()
        outs, ev = evaluate(rows, clones, decided.timestamp(), cache, gh=refresh)
        stats.update(ev)
        stats["states"] = Counter(o["state"] for o in outs)
        if not dry_run:
            atomic_write_text(_cache_path(), json.dumps(cache))     # before the sink: a failing write never redoes the blame
        todo = pending(outs, _load_stored(dsn))
        stats["to_write"] = len(todo)
        if not dry_run:
            _insert(dsn, [{"sha": o["sha"], "repo_key": o["repo_key"], "state": o["state"],
                           "survival": o["survival"], "decided_at": decided} for o in todo])
            written = len(todo)
        return stats
    except BaseException as exc:
        status, error = "error", scrub_error(exc)
        interrupted = isinstance(exc, KeyboardInterrupt)    # leave at once, send nothing
        raise
    finally:
        if not dry_run and not interrupted:
            deliver_scan_rows(cfg, [scan_row(cfg, "outcomes", started, status=status, error=error,
                                             events=written)], "outcomes")


def print_outcomes(report: dict[str, Any], dry_run: bool) -> None:
    states = dict(sorted(report["states"].items()))
    rest = {k: v for k, v in sorted(report.items()) if k != "states"}
    print(f"outcomes {'DRY-RUN (nothing written)' if dry_run else 'written'}: "
          f"states {json.dumps(states)}, {json.dumps(rest)}")


def _project_paths(cwd: str) -> list[str]:
    """cwd and its parents up to the repo's top level (only cwd outside a repo)."""
    top = (_git(cwd, "rev-parse", "--show-toplevel", timeout=10) or "").strip()
    root = os.path.realpath(top) if top else None
    out, p = [], Path(cwd)
    while True:
        out.append(str(p))
        if not root or os.path.realpath(p) == root or p.parent == p:
            break
        p = p.parent
    return out + ([root] if root and root not in out else [])


def latest_session(home: Path, cwd: str) -> str | None:
    # shortcut: Claude Code sessions only; upgrade when /outcome runs inside another agent.
    files = [f for p in _project_paths(cwd) for f in claude_code.project_session_files(home, p)]
    for f in sorted(set(files), key=lambda f: f.stat().st_mtime, reverse=True):
        if claude_code.is_interactive(f):
            return f.stem
    return None


def build_manual_outcome(cfg: Config, outcome: str, cwd: str, session: str | None = None, *,
                         now: datetime | None = None) -> dict[str, Any]:
    """One agents.manual_outcome row, or AutError with the reason it was refused."""
    if outcome not in MANUAL:
        raise AutError(f"outcome: one of {', '.join(MANUAL)}")
    sid = session or latest_session(cfg.home, cwd)
    if not sid:
        raise AutError("outcome: no interactive session found for this directory; "
                       "pass --session ID")
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {"session_id": sid, "outcome": MANUAL[outcome], "ts": ts,
            "repo_key": RepoKeys(cfg.repo_key_salt).of_cwd(cwd)}


def insert_manual_outcome(dsn: str, row: dict) -> None:
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.manual_outcome (session_id, outcome, ts, repo_key) "
                     "VALUES (%s, %s, %s, %s)",
                     (row["session_id"], row["outcome"], row["ts"], row["repo_key"]))
        conn.commit()
