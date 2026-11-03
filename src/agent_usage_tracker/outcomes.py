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
  undecided   younger than 14 days (from the merge, or from the commit row when not merged)

`aut outcome <shipped|ruled-out|open>`: the manual outcome of the newest interactive session of
the current project, written to agents.manual_outcome.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .collectors._activity_common import RepoKeys, repo_identity
from .config import Config
from .push import scrub_error, send_scan_rows
from .state import _state_path

DAY = 86400
DECIDE_S = 14 * DAY          # undecided until day 14; also the revert and survival window
FETCH_TIMEOUT_S = 30
BLAME_FILES = 30             # survival blames at most this many files per commit
CACHE_NAME = "outcomes_cache.json"
_GH_STABLE_S = 60 * DAY      # a "no merged PR" answer is cached once the commit is this old
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git", ".tox"}
_MAX_DEPTH = 6
_WORKERS = 8
MANUAL = {"shipped": "shipped", "ruled-out": "ruled_out", "open": "open"}


def _git(path: str, *args: str, inp: str | None = None, timeout: float = 300) -> str | None:
    """stdout of a git command, or None when it fails or times out."""
    try:
        r = subprocess.run(["git", "-C", path, *args], input=inp, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


@dataclass(slots=True)
class Clone:
    path: str                 # a worktree of the repo (held in memory only)
    key: str | None           # repo_key of its origin
    gh: str | None            # owner/repo when origin is on github.com (for gh)
    default: str | None       # origin/HEAD's target ref, or a main/master/develop fallback


def _default_ref(path: str) -> str | None:
    ref = (_git(path, "symbolic-ref", "-q", "refs/remotes/origin/HEAD") or "").strip()
    if ref:
        return ref
    for cand in ("refs/remotes/origin/main", "refs/remotes/origin/master",
                 "refs/remotes/origin/develop", "refs/heads/main", "refs/heads/master",
                 "refs/heads/develop"):
        if _git(path, "rev-parse", "--verify", "-q", cand) is not None:
            return cand
    return None


def find_clones(roots: list[str], keys: RepoKeys) -> list[Clone]:
    """One Clone per repository (worktrees of one repo share a git common dir)."""
    seen: set[str] = set()
    out: list[Clone] = []
    for root in roots:
        base = os.path.expanduser(root)
        for dirpath, dirnames, filenames in os.walk(base):
            depth = dirpath[len(base):].count(os.sep)
            if ".git" in dirnames or ".git" in filenames:
                common = (_git(dirpath, "rev-parse", "--path-format=absolute",
                               "--git-common-dir", timeout=10) or "").strip()
                if common and os.path.realpath(common) not in seen:
                    seen.add(os.path.realpath(common))
                    url = (_git(dirpath, "remote", "get-url", "origin", timeout=10) or "").strip()
                    key = gh = None
                    if url:
                        try:
                            ident = repo_identity(url)
                            key = keys.of(ident)
                            gh = ident.removeprefix("github.com/") \
                                if ident.startswith("github.com/") else None
                        except ValueError:
                            pass
                    out.append(Clone(dirpath, key, gh, _default_ref(dirpath)))
            dirnames[:] = [] if depth >= _MAX_DEPTH else \
                [n for n in dirnames if n not in _SKIP_DIRS]
    return out


def fetch_clones(clones: list[Clone]) -> tuple[int, int]:
    """`git fetch` each clone, FETCH_TIMEOUT_S each; (ok, failed). A failure is skipped."""
    ok = failed = 0
    for c in clones:
        if _git(c.path, "fetch", "--quiet", "--no-tags", "origin",
                timeout=FETCH_TIMEOUT_S) is None:
            failed += 1
        else:
            ok += 1
    return ok, failed


def _lookup(clone: Clone, shas: list[str]) -> dict[str, tuple[str, int]]:
    """sha (as stored, possibly abbreviated) -> (full sha, committer time) for those it holds."""
    out = _git(clone.path, "cat-file", "--batch-check",
               inp="".join(f"{s}^{{commit}}\n" for s in shas))
    if out is None:
        return {}
    full = {}
    for s, line in zip(shas, out.splitlines()):
        p = line.split()
        if len(p) == 3 and p[1] == "commit":
            full[s] = p[0]
    if not full:
        return {}
    log = _git(clone.path, "log", "--no-walk=unsorted", "--stdin", "--format=%H %ct",
               inp="".join(f"{f}\n" for f in set(full.values()))) or ""
    ct = dict(line.split() for line in log.splitlines() if line.strip())
    return {s: (f, int(ct[f])) for s, f in full.items() if f in ct}


def _patch_ids(path: str, args: list[str], inp: str | None = None) -> dict[str, str]:
    """full sha -> stable patch id for the commits `git log -p <args>` prints."""
    try:
        log = subprocess.Popen(["git", "-C", path, "log", "-p", "--format=commit %H", *args],
                               stdin=subprocess.PIPE if inp is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        pid = subprocess.Popen(["git", "-C", path, "patch-id", "--stable"], stdin=log.stdout,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        log.stdout.close()
        if inp is not None:
            log.stdin.write(inp.encode())
            log.stdin.close()
        out, _ = pid.communicate(timeout=600)
        log.wait(timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    return {c: p for p, c in (line.split() for line in out.splitlines() if line.strip())}


class _Repo:
    """What one clone says about its anchors, computed once per run."""

    def __init__(self, clone: Clone, since: int, off_default: list[str]) -> None:
        d, path = clone.default, clone.path
        log = (_git(path, "log", "--format=%H %ct", d) or "") if d else ""
        self.on_default = {h: int(t) for h, t in (x.split() for x in log.splitlines() if x)}
        branches = _git(path, "rev-list", "--branches", "--remotes", *([f"^{d}"] if d else []))
        self.on_branch = set((branches or "").split())
        self.twin: dict[str, str] = {}            # off-default full sha -> its twin on default
        off = [f for f in off_default if f not in self.on_default]
        if d and off:
            by_pid = {p: c for c, p in _patch_ids(path, ["--no-merges", f"--since=@{since}",
                                                         d]).items()}
            for c, p in _patch_ids(path, ["--no-walk=unsorted", "--stdin"],
                                   "".join(f"{f}\n" for f in off)).items():
                if p in by_pid and by_pid[p] != c:
                    self.twin[c] = by_pid[p]
        self.reverted: dict[str, int] = {}        # reverted full sha -> earliest revert time
        rv = _git(path, "log", "--branches", "--remotes", f"--since=@{since}",
                  "--grep=This reverts commit", "--format=%ct%n%B%x00") or ""
        for chunk in rv.split("\0"):
            head, _, body = chunk.strip().partition("\n")
            for x in _REVERTS.findall(body) if head.isdigit() else ():
                self.reverted[x] = min(self.reverted.get(x, int(head)), int(head))

    def revert_time(self, full: str) -> int | None:
        hits = [t for x, t in self.reverted.items() if full.startswith(x)]
        return min(hits) if hits else None


def _survival(clone: Clone, target: str, merge_t: float) -> float | None:
    """Share of target's added lines still attributed to it on the default branch 14 days after
    the merge (first BLAME_FILES files). None for a merge commit or a commit adding no lines."""
    path = clone.path
    parents = (_git(path, "rev-list", "--parents", "-n", "1", target) or "").split()
    if len(parents) != 2:
        return None
    base = (_git(path, "rev-list", "-1", f"--before=@{int(merge_t + DECIDE_S)}",
                 clone.default) or "").strip()
    raw = _git(path, "show", "--numstat", "-z", "--no-renames", "--format=", target)
    if not base or raw is None:
        return None
    files = []
    for item in raw.split("\0"):
        a, _, rest = item.strip("\n").partition("\t")
        f = rest.partition("\t")[2]
        if a.isdigit() and int(a) > 0 and f:
            files.append((int(a), f))
    files = files[:BLAME_FILES]
    added = sum(a for a, _ in files)
    if not added:
        return None
    alive = 0
    for _, f in files:
        bl = _git(path, "blame", "--porcelain", base, "--", f, timeout=120)
        alive += sum(1 for line in (bl or "").splitlines() if line.startswith(target + " "))
    return round(min(1.0, alive / added), 4)


def _gh_merged(repo: str, sha: str) -> bool | None:
    """True when a merged PR of repo contains sha; None when gh is missing or fails."""
    if not shutil.which("gh"):
        return None
    try:
        r = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "merged", "--search",
                            sha, "--json", "number", "--limit", "1"], capture_output=True,
                           text=True, timeout=FETCH_TIMEOUT_S)
        return bool(json.loads(r.stdout)) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def evaluate(rows: list[dict], clones: list[Clone], now: float, cache: dict,
             gh: bool = False) -> tuple[list[dict], Counter]:
    """One outcome per (sha, repo_key) of the commit rows. rows: sha, repo_key, ts (epoch),
    session_id, amend. cache: full sha -> survival (filled in place); gh answers sit under 'gh:'."""
    stats: Counter = Counter(rows=len(rows))
    anchors: dict[tuple[str, str | None], dict] = {}
    by_session = defaultdict(list)
    for r in rows:
        sha = (r["sha"] or "").lower()
        if not _SHA.match(sha):
            stats["junk_sha"] += 1
            continue
        a = anchors.setdefault((sha, r["repo_key"]), {"ts": r["ts"], "amended": False})
        a["ts"] = min(a["ts"], r["ts"])
        by_session[r["session_id"]].append((r["ts"], r["amend"], (sha, r["repo_key"])))
    for seq in by_session.values():
        seq.sort(key=lambda x: x[0])
        for (_, _, k), (_, amend, _) in zip(seq, seq[1:]):
            anchors[k]["amended"] |= bool(amend)
    stats["anchors"] = len(anchors)

    shas = sorted({s for s, _ in anchors})
    held = {i: _lookup(c, shas) for i, c in enumerate(clones)} if shas else {}
    picked: dict[tuple, tuple[int, str]] = {}            # anchor -> (clone index, full sha)
    for (sha, key), a in anchors.items():
        cands = [(i, held[i][sha][0]) for i in held
                 if sha in held[i] and abs(held[i][sha][1] - a["ts"]) <= DAY]
        own = [c for c in cands if key and clones[c[0]].key == key]
        cands = own or sorted(cands, key=lambda c: clones[c[0]].key is None)
        if cands:
            picked[(sha, key)] = cands[0]
            stats["found"] += 1
            stats["null_key_resolved"] += key is None and clones[cands[0][0]].key is not None
        else:
            stats["not_found"] += 1
    per_clone: dict[int, list[str]] = defaultdict(list)
    for i, full in picked.values():
        per_clone[i].append(full)
    repos = {}
    for i, fulls in per_clone.items():
        cts = [held[i][s][1] for (s, _), (j, _) in picked.items() if j == i]
        repos[i] = _Repo(clones[i], int(min(cts)) - 2 * DAY, fulls)

    outs, jobs, gh_jobs = [], [], []
    for (sha, key), a in anchors.items():
        o = {"sha": sha, "repo_key": key or "", "state": "undecided", "survival": None}
        outs.append(o)
        young = now - a["ts"] < DECIDE_S
        if (sha, key) not in picked:
            # shortcut: a commit no local clone holds counts as abandoned after day 14; right for
            # work never pushed, wrong for a repo not cloned on this machine. Upgrade with an
            # 'unknown' state in commit_outcome if that share grows.
            o["state"] = "undecided" if young else "abandoned"
            stats["abandoned_not_found"] += not young
            continue
        i, full = picked[(sha, key)]
        c, rp = clones[i], repos[i]
        o["repo_key"] = key or c.key or ""
        target = full if full in rp.on_default else rp.twin.get(full)
        if target:
            # shortcut: the merge time is the (twin) commit's committer time, not the time it
            # reached the default branch; upgrade with --ancestry-path if merges lag commits.
            merge_t = rp.on_default[target]
            rv = rp.revert_time(full) or rp.revert_time(target)
            if now - merge_t < DECIDE_S:
                pass
            elif rv is not None and rv - merge_t <= DECIDE_S:
                o["state"] = "reverted"
            else:
                o["state"] = "merged"
                jobs.append((o, c, target, merge_t))
        elif full not in rp.on_branch and a["amended"]:
            o["state"] = "superseded"
        elif young:
            pass
        elif gh and c.gh:        # a squash-merged PR leaves its commits off the default branch
            gh_jobs.append((o, c, full, a["ts"], full in rp.on_branch))
        else:
            o["state"] = "open" if full in rp.on_branch else "abandoned"

    def gh_one(job) -> tuple[int, int]:
        o, c, full, ts, on_branch = job
        k = f"gh:{full}"
        looked = failed = 0
        if k not in cache:
            ans = _gh_merged(c.gh, full)
            looked, failed = 1, ans is None
            if ans or (ans is False and now - ts >= _GH_STABLE_S):
                cache[k] = ans          # a merge never undoes itself; an old "no" stays no
            merged = bool(ans)
        else:
            merged = bool(cache[k])
        o["state"] = "merged" if merged else "open" if on_branch else "abandoned"
        return looked, int(failed)

    def surv_one(job) -> bool:
        o, c, target, merge_t = job
        fresh = target not in cache
        if fresh:
            cache[target] = _survival(c, target, merge_t)
        o["survival"] = cache[target]
        if o["survival"] is not None and o["survival"] < 0.5:
            o["state"] = "reworked"
        return fresh

    with ThreadPoolExecutor(_WORKERS) as pool:     # tallies stay on this thread
        for looked, failed in pool.map(gh_one, gh_jobs):
            stats["gh_lookups"] += looked
            stats["gh_failed"] += failed
        for fresh in pool.map(surv_one, jobs):
            stats["survival_computed" if fresh else "survival_cached"] += 1
    # a NULL-key row resolved to a clone can repeat a keyed row of the same commit: keep one
    outs = list({(o["sha"], o["repo_key"]): o for o in reversed(outs)}.values())[::-1]
    for o in outs:
        stats[f"state_{o['state']}"] += 1
    return outs, stats


def pending(outs: list[dict], stored: dict[tuple[str, str], tuple]) -> list[dict]:
    """The outcomes whose (state, survival) differs from the latest stored row."""
    return [o for o in outs if stored.get((o["sha"], o["repo_key"])) != (o["state"], o["survival"])]


def _cache_path() -> Path:
    return _state_path().parent / CACHE_NAME


def _load_cache() -> dict:
    try:
        obj = json.loads(_cache_path().read_text())
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(cache, fh)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_rows(dsn: str) -> list[dict]:
    import psycopg  # optional dependency; install with the [postgres] extra

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        rows = conn.execute(
            "SELECT session_id, ts, attrs->>'sha', attrs->>'repo_key', "
            "coalesce((attrs->>'amend')::boolean, false) "
            "FROM agents.activity_event WHERE kind = 'commit'").fetchall()
    return [{"session_id": s, "ts": ts.timestamp(), "sha": sha, "repo_key": k, "amend": am}
            for s, ts, sha, k, am in rows]


def _load_stored(dsn: str) -> dict[tuple[str, str], tuple]:
    import psycopg  # optional dependency; install with the [postgres] extra

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (sha, repo_key) sha, repo_key, state, survival "
            "FROM agents.commit_outcome ORDER BY sha, repo_key, decided_at DESC").fetchall()
    return {(sha, k): (st, None if sv is None else round(float(sv), 4))
            for sha, k, st, sv in rows}


def _insert(dsn: str, rows: list[dict]) -> None:
    import psycopg  # optional dependency; install with the [postgres] extra

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO agents.commit_outcome (sha, repo_key, state, survival, decided_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [(r["sha"], r["repo_key"], r["state"], r["survival"], r["decided_at"])
                 for r in rows])
        conn.commit()


def run_outcomes(cfg: Config, fetch: bool, dry_run: bool, now: float | None = None) -> Counter:
    """Score every commit row and write the changed states. --fetch also turns on gh. A dry run
    reads the sink and the clones and writes nothing: no rows, no cache, no heartbeat."""
    if not cfg.has_pg:
        raise SystemExit("outcomes needs transport.pg_dsn (it reads the commit rows)")
    started = datetime.now(timezone.utc)
    now = now if now is not None else started.timestamp()
    written, status, error = 0, "ok", None
    try:
        rows = _load_rows(cfg.pg_dsn)
        clones = find_clones(list(cfg.outcome_repo_roots), RepoKeys(cfg.repo_key_salt))
        stats: Counter = Counter(clones=len(clones))
        if fetch:
            keys = {r["repo_key"] for r in rows if r["repo_key"]}
            shas = sorted({(r["sha"] or "").lower() for r in rows if _SHA.match(
                (r["sha"] or "").lower())})
            want = [c for c in clones if c.key in keys or _lookup(c, shas)]
            stats["fetch_ok"], stats["fetch_failed"] = fetch_clones(want)
        cache = _load_cache()
        outs, ev = evaluate(rows, clones, now, cache, gh=fetch)
        stats.update(ev)
        todo = pending(outs, _load_stored(cfg.pg_dsn))
        stats["to_write"] = len(todo)
        if not dry_run:
            decided = datetime.fromtimestamp(now, timezone.utc)
            _insert(cfg.pg_dsn, [dict(o, decided_at=decided) for o in todo])
            written = len(todo)
            _save_cache(cache)
        return stats
    except BaseException as exc:
        status, error = "error", scrub_error(exc)
        raise
    finally:
        if not dry_run:
            try:            # best effort, like the scan heartbeat
                send_scan_rows(cfg, [{
                    "machine": cfg.machine, "collector": "outcomes", "started_at": started,
                    "finished_at": datetime.now(timezone.utc), "status": status,
                    "events": written, "human": 0, "newest_ts": None, "error": error,
                    "aut_version": __version__}])
            except Exception as exc:
                print(f"aut: warning: outcomes heartbeat not delivered: {scrub_error(exc)}",
                      file=sys.stderr)


def claude_project_dir(path: str) -> str:
    """The directory name Claude Code keeps a working directory's sessions under."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _project_paths(cwd: str) -> list[str]:
    """cwd and its parents up to the repo's top level (only cwd outside a repo)."""
    top = (_git(cwd, "rev-parse", "--show-toplevel", timeout=10) or "").strip()
    top = os.path.realpath(top) if top else None
    out, p = [], Path(cwd)
    while True:
        out.append(str(p))
        if not top or os.path.realpath(p) == top or p.parent == p:
            break
        p = p.parent
    return out + ([top] if top and top not in out else [])


def _interactive(path: Path) -> bool:
    """A main Claude Code session started at a terminal (entrypoint cli), from its first records."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(200), fh):
                if '"entrypoint"' in line:
                    return json.loads(line).get("entrypoint") == "cli"
    except (OSError, ValueError):
        pass
    return False


def latest_session(home: Path, cwd: str) -> str | None:
    # shortcut: Claude Code sessions only; upgrade when /outcome runs inside another agent.
    base = home / ".claude" / "projects"
    files = [f for p in _project_paths(cwd) for f in (base / claude_project_dir(p)).glob("*.jsonl")]
    for f in sorted(set(files), key=lambda f: f.stat().st_mtime, reverse=True):
        if _interactive(f):
            return f.stem
    return None


def build_manual_outcome(cfg: Config, outcome: str, cwd: str, session: str | None = None,
                         now: float | None = None) -> dict:
    """One agents.manual_outcome row, or SystemExit with the reason it was refused."""
    if outcome not in MANUAL:
        raise SystemExit(f"outcome: one of {', '.join(MANUAL)}")
    sid = session or latest_session(cfg.home, cwd)
    if not sid:
        raise SystemExit("outcome: no interactive session found for this directory; "
                         "pass --session ID")
    ts = datetime.fromtimestamp(now if now is not None else datetime.now().timestamp(),
                                timezone.utc)
    return {"session_id": sid, "outcome": MANUAL[outcome], "ts": ts,
            "repo_key": RepoKeys(cfg.repo_key_salt).of_cwd(cwd)}


def insert_manual(dsn: str, row: dict) -> None:
    import psycopg  # optional dependency; install with the [postgres] extra

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        conn.execute("INSERT INTO agents.manual_outcome (session_id, outcome, ts, repo_key) "
                     "VALUES (%s, %s, %s, %s)",
                     (row["session_id"], row["outcome"], row["ts"], row["repo_key"]))
        conn.commit()
