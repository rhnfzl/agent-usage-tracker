"""`aut git-days`: the owner's commits per Amsterdam day, from every clone, to agents.git_commit_day.

Reads branches, remotes, tags and all registered worktree heads (no merges, stash or notes),
including detached heads outside scan roots, in each object store under [git_days] roots and,
over `ssh -o BatchMode=yes`, under each remote's roots, with the same hardened git (git_safe). No
fetch. A commit counts once (by SHA) when its author email is one of the owner's identities, or a
shared author's in a repo whose name matches that author's repo_regex; a bot never counts.
Only counts leave the Mac: repo names, SHAs, emails and messages stay in memory, and no message
names a repo, host or path.

The table is insert-only and a day's latest scan wins, so a run inserts, with one scanned_at, the
days from `since` to yesterday that have no row yet or whose count went up. A stored day never goes
down by itself: a lower count is only counted in the summary, and `--allow-lower` rewrites it.
A partial scan writes nothing: a missing or unreadable root, a host, folder or store that could not
be read, a remote git older than 2.31, a scan that found nothing, or one with under half the object
stores of the best of the last good runs. After a failed run `aut scan` waits RETRY_AFTER.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess  # nosec B404 # argv lists only; the remote script goes to ssh on stdin
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import PurePosixPath
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AutError, Config
from .git_safe import HARDENED_ENV, git_argv
from .outcome_git import _common_dir, run_git
from .push import job_heartbeat, redacted_error
from .sink import connect, write_many
from .state import collector_wm, load_state, save_state

NAME = "git_days"                   # its aut state entry and heartbeat collector
LEDGER_TZ = "Europe/Amsterdam"      # the ledger's day, as in the sink's views
LOG_ARGS = ("log", "--branches", "--remotes", "--tags", "--no-merges", "--format=%H%x09%aI%x09%ae")
_PRUNE = ("node_modules", ".venv", "venv", "vendor")
_MAX_DEPTH = 6                      # folders below a root; its .git sits one deeper
_BOT = re.compile(r"\[bot\]|^noreply@anthropic\.com$")
MIN_STORE_SHARE = 0.5
KEEP_STORES = 5                     # good runs whose best store count the share rule uses
DUE_AFTER = timedelta(hours=20)
RETRY_AFTER = timedelta(hours=6)    # after an attempt that failed or was refused
SSH_CONNECT_S = 10
REMOTE_TIMEOUT_S = 900
INSERT_SQL = "INSERT INTO agents.git_commit_day (day, commits, scanned_at) VALUES (%s, %s, %s)"


@dataclass(slots=True)
class Found:
    stores: int = 0                 # distinct object stores read
    failed: int = 0                 # hosts and stores that could not be read
    commits: dict[str, date] = field(default_factory=dict)     # counted SHA -> local day


def repo_name(common: str) -> str:
    """The repo's folder name from its git common dir: /x/name/.git, /x/name.git, .git/modules/name."""
    p = PurePosixPath(common.replace("\\", "/"))
    return (p.parent.name if p.name == ".git" else p.name).removesuffix(".git")


def _owned(cfg: Config, email: str, repo: str) -> bool:
    if not email or _BOT.search(email):
        return False
    return email in cfg.git_days_identities or any(
        re.search(e, email, re.IGNORECASE) and re.search(r, repo)
        for e, r in cfg.git_days_shared_authors)


def _take(log: str, repo: str, cfg: Config, tz: tzinfo, into: dict[str, date]) -> None:
    for line in log.splitlines():
        sha, _, rest = line.partition("\t")
        when, _, email = rest.partition("\t")
        if sha in into or not _owned(cfg, email.strip().lower(), repo):
            continue
        try:
            into[sha] = datetime.fromisoformat(when).astimezone(tz).date()
        except ValueError:
            continue


def _scan_local(cfg: Config, tz: tzinfo, found: Found) -> None:
    seen: set[str] = set()

    def unreadable(_exc: OSError) -> None:
        found.failed += 1

    for root in cfg.git_days_roots:
        base = os.path.expanduser(root)
        if not os.path.isdir(base):         # missing or unmounted: its repos would read as zero
            found.failed += 1
            continue
        for dirpath, dirnames, filenames in os.walk(base, onerror=unreadable):
            depth = dirpath[len(base):].count(os.sep)
            if ".git" in dirnames or ".git" in filenames:
                common = _common_dir(dirpath)
                if common is None:
                    found.failed += 1
                elif common and os.path.realpath(common) not in seen:
                    seen.add(os.path.realpath(common))
                    found.stores += 1
                    worktrees = run_git(dirpath, "worktree", "list", "--porcelain")
                    heads = [line[5:] for line in (worktrees or "").splitlines()
                             if line.startswith("HEAD ")]
                    log = run_git(dirpath, *LOG_ARGS, *heads) if worktrees is not None else None
                    if log is None:
                        found.failed += 1
                    else:
                        _take(log, repo_name(common), cfg, tz, found.commits)
            dirnames[:] = [] if depth >= _MAX_DEPTH else \
                [n for n in dirnames if n not in (*_PRUNE, ".git")]


_DIR = "\0"     # stands for the folder in a git argv; no real argument holds a NUL


def _sh(argv: list[str]) -> str:
    return " ".join('"$d"' if a == _DIR else shlex.quote(a) for a in argv)


def _sh_root(root: str) -> str:
    if root == "~" or root.startswith("~/"):
        return '"$HOME"' + (("/" + shlex.quote(root[2:])) if root[2:] else "")
    return shlex.quote(root)


def remote_script(roots: Iterable[str]) -> str:
    """POSIX sh doing what _scan_local does: \\1<common dir> starts a store's log lines, \\2 marks a
    missing root, a folder find could not read or a store git could not read, \\3 a git older than
    2.31 (no --path-format). A stale .git that holds no repository is skipped, as locally. stderr
    stays its own stream: ssh hands it back, and it is never printed (it names paths)."""
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in HARDENED_ENV.items())
    prune = " -o ".join(f"-name {n}" for n in _PRUNE)
    rev = _sh(git_argv(_DIR, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return f"""export {env} LC_ALL=C
set -- $(git version)
v=${{3:-0.0}}
case $v in [0-9]*.[0-9]*) ;; *) v=0.0 ;; esac
maj=${{v%%.*}}; min=${{v#*.}}; min=${{min%%.*}}
if [ "$maj" -lt 2 ] || {{ [ "$maj" -eq 2 ] && [ "$min" -lt 31 ]; }}; then printf '\\003\\n'; exit 0; fi
err=$(mktemp) || exit 3
trap 'rm -f "$err"' EXIT
fail=$(printf '\\002')
seen='
'
for root in {" ".join(map(_sh_root, roots))}; do
  if [ -d "$root" ]; then
    find -H "$root" -maxdepth {_MAX_DEPTH + 1} \\( {prune} \\) -prune \
-o -name .git -prune -print || printf '\\002\\n'
  else
    printf '\\002\\n'
  fi
done | while IFS= read -r g; do
  if [ "$g" = "$fail" ]; then printf '\\002\\n'; continue; fi
  d=${{g%/.git}}
  if ! c=$({rev} 2>"$err"); then
    case $(cat "$err") in *"not a git repository"*|*"invalid gitfile format"*) ;; \
*) printf '\\002\\n' ;; esac
    continue
  fi
  case $seen in *"
$c
"*) continue ;; esac
  seen="$seen$c
"
  printf '\\001%s\\n' "$c"
  if ! heads=$({_sh(git_argv(_DIR, 'worktree', 'list', '--porcelain'))}); then
    printf '\\002\\n'; continue
  fi
  heads=$(printf '%s\\n' "$heads" | sed -n 's/^HEAD //p')
  {_sh(git_argv(_DIR, *LOG_ARGS))} $heads || printf '\\002\\n'
done
exit 0
"""


def _ssh_argv(host: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_S}", host,
            "sh", "-s"]


def _scan_remote(i: int, host: str, roots: tuple[str, ...], cfg: Config, tz: tzinfo,
                 found: Found) -> None:
    try:
        r = subprocess.run(  # nosec B603 # no shell here; the host is validated by config
            _ssh_argv(host), input=remote_script(roots), capture_output=True, text=True,
            errors="replace", timeout=REMOTE_TIMEOUT_S)
        code: int | None = r.returncode
    except (OSError, subprocess.SubprocessError):
        code = None
    if code != 0:
        found.failed += 1
        print(f"aut: warning: git-days: remotes[{i}] could not be read "
              f"({'no answer' if code is None else f'exit {code}'}); this run writes nothing",
              file=sys.stderr)
        return
    if "\x03" in r.stdout.splitlines():
        found.failed += 1
        print(f"aut: warning: git-days: remotes[{i}] has git older than 2.31 (or none); "
              "this run writes nothing", file=sys.stderr)
        return
    repo, log = "", ""
    for line in r.stdout.splitlines() + ["\x01"]:
        if line.startswith("\x01") or line == "\x02":
            if repo:
                _take(log, repo, cfg, tz, found.commits)
            repo, log = "", ""
            if line == "\x02":
                found.failed += 1
            elif line[1:]:
                found.stores += 1
                repo = repo_name(line[1:])
        elif repo:
            log += line + "\n"


def scan_all(cfg: Config, tz: tzinfo) -> Found:
    found = Found()
    _scan_local(cfg, tz, found)
    for i, (host, roots) in enumerate(cfg.git_days_remotes):
        _scan_remote(i, host, roots, cfg, tz, found)
    return found


def day_counts(commits: dict[str, date], since: date, until: date) -> dict[date, int]:
    """Every day from since to until, zeros included."""
    days = {since + timedelta(n): 0 for n in range((until - since).days + 1)}
    for day in commits.values():
        if day in days:
            days[day] += 1
    return days


def to_write(days: dict[date, int], stored: dict[date, int], *,
             allow_lower: bool = False) -> tuple[list[tuple[date, int]], list[int]]:
    """(rows: the days with no row yet or a higher count, the drop of each day that would go
    down). allow_lower: every changed day is a row."""
    rows, drops = [], []
    for d, n in sorted(days.items()):
        old = stored.get(d)
        if old is not None and n < old and not allow_lower:
            drops.append(old - n)
        elif old != n:
            rows.append((d, n))
    return rows, drops


def refusal(found: Found, prev_stores: list[int] | None, *,
            allow_lower: bool = False) -> str | None:
    """Why this scan must not write, or None. prev_stores: the last good runs' store counts."""
    if found.failed:
        return (f"{found.failed} root(s), host(s), folder(s) or object store(s) could not be read "
                "(a partial scan)")
    if not found.stores:
        return "the scan found no repositories"
    best = max(prev_stores or [0])
    if not allow_lower and found.stores < best * MIN_STORE_SHARE:
        return (f"fewer object stores than half the best of the last good runs ({found.stores} < "
                f"{best})")
    return None


def load_stored(dsn: str) -> dict[date, int]:
    with connect(dsn) as conn:
        return dict(conn.execute("SELECT day, commits FROM agents.git_commits_daily").fetchall())


def _ledger_zone() -> tzinfo:
    try:
        return ZoneInfo(LEDGER_TZ)
    except ZoneInfoNotFoundError:
        raise AutError("git-days needs the time zone database here: pip install tzdata") from None


def run(cfg: Config, *, dry_run: bool, allow_lower: bool = False, now: datetime | None = None,
        tz: tzinfo | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    # the heartbeat comes first, so the watchdog also sees a run that fails before the scan
    with job_heartbeat(cfg, NAME, datetime.now(timezone.utc), dry_run=dry_run) as beat:
        if not cfg.git_days_identities:
            raise AutError("git-days needs [git_days] identities in config.toml")
        dsn = None if dry_run else cfg.require_pg("git-days")
        tz = tz or _ledger_zone()
        state = load_state()
        wm = collector_wm(state, NAME)
        kept = wm.get("stores")
        prev = [n for n in kept if isinstance(n, int)] if isinstance(kept, list) else []
        found = scan_all(cfg, tz)
        why = refusal(found, prev, allow_lower=allow_lower)
        days = day_counts(found.commits, cfg.git_days_since,
                          now.astimezone(tz).date() - timedelta(days=1))
        report = {"stores": found.stores, "failed": found.failed, "days": days, "written": 0,
                  "refused": why, "lowered": 0, "lowered_by": 0}
        if dsn is None:
            return report
        if why:
            raise AutError(f"git-days: not writing: {why}")
        rows, drops = to_write(days, load_stored(dsn), allow_lower=allow_lower)
        report["lowered"], report["lowered_by"] = len(drops), sum(drops)
        write_many(dsn, INSERT_SQL, [(d, n, now) for d, n in rows])
        beat.events = report["written"] = len(rows)
        # --allow-lower is the deliberate reset: the store history starts again from this run
        wm.update(stores=([] if allow_lower else prev)[-(KEEP_STORES - 1):] + [found.stores],
                  last_ok=now.isoformat())
        save_state(state, [NAME])
    return report


def print_report(report: dict[str, Any], *, dry_run: bool) -> None:
    head = "git-days DRY-RUN (nothing written)" if dry_run else \
        f"git-days: wrote {report['written']} days"
    print(f"{head}: {report['stores']} object stores read, {report['failed']} could not be read")
    if report.get("lowered"):
        print(f"    {report['lowered']} day(s) would go down ({report['lowered_by']:,} commits in "
              "all); not written. `aut git-days --allow-lower` rewrites them")
    if not dry_run:
        return
    years: dict[int, list[int]] = {}
    for day, n in report["days"].items():
        y = years.setdefault(day.year, [0, 0])
        y[0] += n
        y[1] += n > 0
    for year, (n, k) in sorted(years.items()):
        print(f"    {year}  {n:>7,} commits on {k} days")
    if report["refused"]:
        print(f"    a real run would not write: {report['refused']}")


def _older(stamp: object, now: datetime, gap: timedelta) -> bool:
    try:
        return now - datetime.fromisoformat(str(stamp)) >= gap
    except (TypeError, ValueError):         # never ran, or an unusable stamp
        return True


def run_if_due(cfg: Config, now: datetime | None = None) -> None:
    """From `aut scan`: once the last good run is DUE_AFTER old and the last attempt RETRY_AFTER
    old. Never fails the scan. A manual `aut git-days` calls run() and always runs."""
    if not cfg.git_days_enabled:
        return
    try:
        now = now or datetime.now(timezone.utc)
        state = load_state()
        wm = collector_wm(state, NAME)
        if not (_older(wm.get("last_ok"), now, DUE_AFTER)
                and _older(wm.get("last_attempt"), now, RETRY_AFTER)):
            return
        wm["last_attempt"] = now.isoformat()
        save_state(state, [NAME])
        print_report(run(cfg, dry_run=False), dry_run=False)
    except Exception as exc:
        print(f"aut: warning: git-days did not run: {redacted_error(exc)}", file=sys.stderr)
