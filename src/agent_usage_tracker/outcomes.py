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

The git and gh reads live in outcome_git; this module decides the states and writes them.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple, NotRequired, TypedDict

from .collectors import claude_code
from .config import AutError, Config
from .outcome_git import (DAY, DECIDE_S, Clone, GitFailed, Hit, Repo, fetch_clones, find_clones,
                          gh_merged, lookup, read_repo, run_git, survival)
from .push import deliver_scan_rows, scan_row, scrub_error
from .repo_keys import RepoKeys
from .sink import connect, write_many
from .state import state_path, atomic_write_text, load_json_dict

CACHE_NAME = "outcomes_cache.json"
_CACHE_V = 2                 # 2: a failed git read no longer counts as a survival score
_GH_STABLE_S = 60 * DAY      # a "no merged PR" answer is cached once the commit is this old
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
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


class _Candidate(NamedTuple):
    clone: int                # index into the run's clones
    full: str


class _Pick(NamedTuple):
    clone: Clone
    repo: Repo
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


def _candidates(a: _Anchor, readable: dict[int, dict[str, Hit]], failed: list[int],
                clones: list[Clone]) -> list[_Candidate]:
    """The clones that may hold the anchor, its own repo's first; marks it `unread` when a clone
    that might know more could not be read."""
    cands = [_Candidate(i, h[a.sha].full) for i, h in readable.items()
             if a.sha in h and abs(h[a.sha].committed - a.ts) <= DAY]
    own = [c for c in cands if a.key and clones[c.clone].key == a.key]
    a.unread = any(not own or clones[i].key == a.key for i in failed)
    return own or sorted(cands, key=lambda c: clones[c.clone].key is None)


def _advance(rp: Repo, full: str) -> int:
    """How far along a commit is in one clone: merged < open < unreachable."""
    return 0 if full in rp.on_default or full in rp.twin else 1 if full in rp.on_branch else 2


def _resolve(anchors: list[_Anchor], clones: list[Clone], stats: Counter) -> None:
    """Point each anchor at the clone where it is furthest along, and mark the ones a failed git
    read could change."""
    shas = sorted({a.sha for a in anchors})
    held = {i: lookup(c, shas) for i, c in enumerate(clones)} if shas else {}
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
    repos = {i: read_repo(clones[i], int(min(t for _, t in got)) - 2 * DAY, [f for f, _ in got])
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
        merged = gh_merged(repo, full)
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
            cache[target] = survival(c, target, merge_t)
        except GitFailed:          # not cached: the next run measures it again
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
    # a NULL-key row resolved to a clone can repeat a keyed row of the same commit: keep the keyed
    # anchor's, whatever order the rows came in, because it alone says which repo it means
    kept: dict[tuple[str, str], Outcome] = {}
    for a, o in zip(anchors, outs):
        if a.key is not None or (o["sha"], o["repo_key"]) not in kept:
            kept[(o["sha"], o["repo_key"])] = o
    return list(kept.values()), stats


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
    top = (run_git(cwd, "rev-parse", "--show-toplevel", timeout=10) or "").strip()
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
