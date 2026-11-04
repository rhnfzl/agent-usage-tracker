"""Hermetic tests for `aut outcomes` and `aut outcome` on temp git repos (real git, synthetic commits).

Run: `python tests/test_outcomes.py`  (or `pytest`).
Locks the rules:
  - merged: ancestor of origin/HEAD, or a single-commit patch-id twin on it, or (network on) a
    merged GitHub PR found by gh; reverted within 14 days of the merge; reworked when under half
    the added lines survive 14 days on; open on a branch only; abandoned when unreachable;
    a pre-amend SHA is superseded; everything is undecided until day 14;
  - a row with no repo_key takes the key of the clone that holds the SHA, but only when the
    committer time is within a day of the row;
  - survival is computed once and cached, a fetch that fails or times out is skipped and counted;
  - only changed states are written, a dry run writes nothing and never fetches or asks gh, a
    real run sends the heartbeat;
  - the merge time is when the commit landed (first parent), survival waits for a default ref
    newer than merge + 14 days, the furthest-along clone wins, a commit gone missing or a failed
    gh lookup or git read never replaces a stored state, a failed git read is never scored or
    cached as survival, fetch never runs gc and is terminated on timeout;
  - the manual outcome picks the newest interactive session of the current project;
  - a clone's own config, attributes and hooks never make git run a command (diff.external, a
    textconv driver, core.fsmonitor, a hook, remote uploadpack), and --fetch only fetches clones
    whose repo_key a commit row carries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import outcomes as oc  # noqa: E402
from agent_usage_tracker import push  # noqa: E402
from agent_usage_tracker.repo_keys import RepoKeys, repo_identity  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.git_safe import git_argv, git_env  # noqa: E402

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull      # the user's git config never reaches the tests
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
SALT = "test-salt-for-outcomes"
DAY = 86400
NOW = time.time()


def _cfg(home: Path, roots: tuple[str, ...] = (), pg: str | None = "postgresql://x") -> Config:
    return Config(machine="test", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=pg,
                  home=home, repo_key_salt=SALT, outcome_repo_roots=roots)


def _g(repo: Path, *args: str, age: float | None = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    if age is not None:
        stamp = f"@{int(NOW - age * DAY)} +0000"
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    r = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                       text=True, env=env)
    return r.stdout.strip()


def _commit(repo: Path, name: str, text: str, age: float, msg: str | None = None) -> str:
    (repo / name).write_text(text)
    _g(repo, "add", name)
    _g(repo, "commit", "-q", "-m", msg or f"add {name}", age=age)
    return _g(repo, "rev-parse", "HEAD")


def _lines(tag: str, n: int = 10) -> str:
    return "".join(f"{tag} line {i}\n" for i in range(n))


def _world(d: Path) -> tuple[Path, dict[str, str]]:
    """origin (bare) + one clone with every outcome. Returns the clone and name -> full SHA."""
    origin, work = d / "origin.git", d / "projects" / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work.mkdir(parents=True)
    _g(work, "init", "-q", "-b", "main")
    _g(work, "remote", "add", "origin", str(origin))
    s: dict[str, str] = {}
    _commit(work, "base.txt", "base\n", 60)
    s["D"] = _commit(work, "d.txt", _lines("d"), 40)
    _commit(work, "d.txt", _lines("rewritten"), 35, "rewrite d")     # D's lines all gone
    s["A"] = _commit(work, "a.txt", _lines("a"), 30)
    _g(work, "checkout", "-q", "-b", "topic-b")
    s["B"] = _commit(work, "b.txt", _lines("b"), 30)
    _g(work, "checkout", "-q", "main")
    _g(work, "cherry-pick", s["B"], age=29)                      # the twin lands on main
    s["B2"] = _g(work, "rev-parse", "HEAD")
    _g(work, "branch", "-q", "-D", "topic-b")
    s["C"] = _commit(work, "c.txt", _lines("c"), 28)
    _g(work, "revert", "--no-edit", s["C"], age=27)
    _g(work, "checkout", "-q", "-b", "topic-e")
    s["E"] = _commit(work, "e.txt", _lines("e"), 25)
    _g(work, "push", "-q", "origin", "topic-e")
    _g(work, "checkout", "-q", "main")
    _g(work, "branch", "-q", "-D", "topic-e")
    _g(work, "checkout", "-q", "-b", "topic-f")
    s["F"] = _commit(work, "f.txt", _lines("f"), 25)
    _g(work, "checkout", "-q", "main")
    _g(work, "branch", "-q", "-D", "topic-f")                   # unreachable, object kept
    _g(work, "checkout", "-q", "-b", "topic-g")
    s["G"] = _commit(work, "g.txt", _lines("g"), 25)
    _g(work, "commit", "-q", "--amend", "-m", "amended", age=25)
    s["G2"] = _g(work, "rev-parse", "HEAD")
    _g(work, "checkout", "-q", "main")
    s["Y"] = _commit(work, "y.txt", _lines("y"), 2)                # too young to decide
    _g(work, "push", "-q", "origin", "main")
    _g(work, "remote", "set-head", "origin", "main")
    return work, s


def _key(url: str) -> str:
    return RepoKeys(SALT).of(repo_identity(url))


def _row(sha: str, key: str | None, age: float, session: str = "s1", amend: bool = False,
         short: int = 9) -> dict:
    return {"sha": sha[:short], "repo_key": key, "ts": NOW - age * DAY + 60,
            "session_id": session, "amend": amend}


def _states(outs: list[dict]) -> dict[str, tuple]:
    return {o["sha"]: (o["state"], o["survival"], o["repo_key"]) for o in outs}


def test_states_from_git():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert len(clones) == 1 and clones[0].key == key, clones
        rows = [_row(s["A"], key, 30), _row(s["B"], key, 30), _row(s["C"], key, 28),
                _row(s["D"], key, 40), _row(s["E"], key, 25), _row(s["F"], key, 25),
                _row(s["G"], key, 25, "sg"), _row(s["G2"], key, 25, "sg", amend=True),
                _row(s["Y"], key, 2)]
        rows[-2]["ts"] += 30                                       # the amend comes second
        outs, stats = oc.evaluate(rows, clones, NOW, cache={})
        got = _states(outs)
        exp = {"A": ("merged", 1.0), "B": ("merged", 1.0), "C": ("reverted", None),
               "D": ("reworked", 0.0), "E": ("open", None), "F": ("abandoned", None),
               "G": ("superseded", None), "G2": ("open", None), "Y": ("undecided", None)}
        for name, (state, surv) in exp.items():
            assert got[s[name][:9]][:2] == (state, surv), (name, got[s[name][:9]])
            assert got[s[name][:9]][2] == key
        assert stats["found"] == 9 and stats["not_found"] == 0, stats
        print("ok states_from_git")


def test_null_key_resolution_needs_matching_time():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        rows = [_row(s["A"], None, 30, short=7),                   # time matches: resolved
                _row(s["A"], key, 30, short=7),                    # the same commit, keyed
                _row(s["F"], None, 25 + 3),                        # 3 days off: not this commit
                _row("abcdef1234", None, 20, short=10)]                      # in no clone at all
        outs, stats = oc.evaluate(rows, clones, NOW, cache={})
        got = _states(outs)
        assert got[s["A"][:7]] == ("merged", 1.0, key), got
        assert len(outs) == 3 and oc.Counter(o["state"] for o in outs)["merged"] == 1, outs  # written once
        assert got[s["F"][:9]] == ("abandoned", None, ""), got      # unresolved: no key
        assert got["abcdef1234"] == ("abandoned", None, ""), got
        assert stats["null_key_resolved"] == 1 and stats["not_found"] == 2, stats
        print("ok null_key_resolution_needs_matching_time")


def test_young_missing_commit_is_undecided():
    outs, stats = oc.evaluate([_row("abcdef1234", None, 3, short=10)], [], NOW, cache={})
    assert _states(outs)["abcdef1234"][0] == "undecided", outs
    print("ok young_missing_commit_is_undecided")


def test_survival_cached_and_never_recomputed():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        cache: dict = {}
        oc.evaluate([_row(s["D"], key, 40)], clones, NOW, cache=cache)
        assert cache == {s["D"]: 0.0}, cache
        real, oc._survival = oc._survival, lambda *a: (_ for _ in ()).throw(AssertionError("blame"))
        try:
            outs, stats = oc.evaluate([_row(s["D"], key, 40)], clones, NOW, cache=cache)
        finally:
            oc._survival = real
        assert _states(outs)[s["D"][:9]][:2] == ("reworked", 0.0)
        assert stats["survival_cached"] == 1 and stats["survival_computed"] == 0, stats
        print("ok survival_cached_and_never_recomputed")


def test_gh_merged_pr_counts_as_merged():
    if os.name == "nt":                    # the fakes are POSIX shell scripts
        return
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        url = "https://github.com/owner/repo.git"
        _g(work, "remote", "set-url", "origin", url)              # refs stay, no fetch happens
        bindir = Path(d) / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(f"#!/bin/sh\ncase \"$*\" in *{s['F'][:9]}*) echo '[{{\"number\": 7}}]';;"
                      " *) echo '[]';; esac\n")
        gh.chmod(0o755)
        key = _key(url)
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        rows = [_row(s["F"], key, 25), _row(s["E"], key, 25)]
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{old}"
        try:
            outs, stats = oc.evaluate(rows, clones, NOW, cache={}, gh=True)
            off, _ = oc.evaluate(rows, clones, NOW, cache={}, gh=False)
        finally:
            os.environ["PATH"] = old
        assert _states(outs)[s["F"][:9]][:2] == ("merged", None), outs
        assert _states(outs)[s["E"][:9]][0] == "open", outs        # gh said no
        assert _states(off)[s["F"][:9]][0] == "abandoned", off       # gh only with the network on
        assert stats["gh_lookups"] == 2, stats
        print("ok gh_merged_pr_counts_as_merged")


def test_fetch_failure_and_timeout_are_counted():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        bad = Path(d) / "projects" / "bad"
        bad.mkdir()
        _g(bad, "init", "-q")
        _g(bad, "remote", "add", "origin", str(Path(d) / "missing.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert len(clones) == 2
        assert oc.fetch_clones(clones) == (1, 1)
        assert sum(c.fetched_at > 0 for c in clones) == 1 and oc.FETCH_TIMEOUT_S == 30
        print("ok fetch_failure_and_timeout_are_counted")


def test_find_clones_dedups_worktrees_and_skips_vendored():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        _g(work, "worktree", "add", "-q", str(Path(d) / "projects" / "wt"), "-b", "wt-branch")
        nm = Path(d) / "projects" / "node_modules" / "dep"
        nm.mkdir(parents=True)
        _g(nm, "init", "-q")
        clones = oc.find_clones([str(Path(d) / "projects"), str(Path(d) / "nope")],
                                RepoKeys(SALT))
        assert len(clones) == 1, clones
        print("ok find_clones_dedups_worktrees_and_skips_vendored")


def test_pending_writes_only_changes():
    outs = [{"sha": "a" * 9, "repo_key": "k", "state": "merged", "survival": 1.0},
            {"sha": "b" * 9, "repo_key": "k", "state": "open", "survival": None},
            {"sha": "c" * 9, "repo_key": "", "state": "undecided", "survival": None}]
    stored = {("a" * 9, "k"): ("merged", 1.0), ("b" * 9, "k"): ("undecided", None)}
    assert [o["sha"][0] for o in oc.pending(outs, stored)] == ["b", "c"]
    print("ok pending_writes_only_changes")


def test_run_outcomes_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        os.environ["AUT_STATE"] = str(Path(d) / "state" / "state.json")
        sent: dict = {"rows": [], "scan": []}
        saved = (oc._load_rows, oc._load_stored, oc._insert, push.send_scan_rows)
        oc._load_rows = lambda dsn: [_row(s["A"], key, 30), _row(s["D"], key, 40)]
        oc._load_stored = lambda dsn: {}
        oc._insert = lambda dsn, rows: sent["rows"].extend(rows)
        push.send_scan_rows = lambda dsn, rows: sent["scan"].extend(rows)
        try:
            cfg = _cfg(Path(d), (str(Path(d) / "projects"),))
            stats = oc.run_outcomes(cfg, fetch=False, dry_run=True, now=NOW)
            assert sent == {"rows": [], "scan": []} and stats["to_write"] == 2, (sent, stats)
            assert not (Path(d) / "state").exists()                # no cache file either
            stats = oc.run_outcomes(cfg, fetch=False, dry_run=False, now=NOW)
            assert {r["state"] for r in sent["rows"]} == {"merged", "reworked"}, sent
            assert all(set(r) == {"sha", "repo_key", "state", "survival", "decided_at"}
                       for r in sent["rows"])
            assert [(r["collector"], r["status"], r["events"]) for r in sent["scan"]] == \
                [("outcomes", "ok", 2)]
            cache = json.loads((Path(d) / "state" / oc.CACHE_NAME).read_text())
            assert cache == {s["A"]: 1.0, s["D"]: 0.0}, cache
        finally:
            (oc._load_rows, oc._load_stored, oc._insert, push.send_scan_rows) = saved
            os.environ.pop("AUT_STATE")
        print("ok run_outcomes_dry_run_writes_nothing")


def test_merge_time_is_when_it_landed_and_base_is_first_parent():
    """A side-branch commit merged long after it was written: the 14 days run from the merge,
    and survival is measured on the first-parent line, never on an unmerged side commit."""
    with tempfile.TemporaryDirectory() as d:
        origin, work = Path(d) / "origin.git", Path(d) / "projects" / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        work.mkdir(parents=True)
        _g(work, "init", "-q", "-b", "main")
        _g(work, "remote", "add", "origin", str(origin))
        _commit(work, "base.txt", "base\n", 60)
        _g(work, "checkout", "-q", "-b", "side1")
        x = _commit(work, "x.txt", _lines("x"), 40)
        _g(work, "checkout", "-q", "main")
        _commit(work, "m.txt", _lines("m"), 30)
        _g(work, "merge", "-q", "--no-ff", "-m", "merge one", "side1", age=20)
        _g(work, "checkout", "-q", "-b", "side2", "HEAD~1")         # from before the merge
        w = _commit(work, "w.txt", _lines("w"), 16)                  # newer than X's merge
        _g(work, "checkout", "-q", "main")
        _g(work, "merge", "-q", "--no-ff", "-m", "merge two", "side2", age=1)
        _g(work, "push", "-q", "origin", "main")
        _g(work, "remote", "set-head", "origin", "main")
        key = _key(str(origin))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        outs, _ = oc.evaluate([_row(x, key, 40), _row(w, key, 16)], clones, NOW, cache={})
        got = _states(outs)
        assert got[x[:9]][:2] == ("merged", 1.0), got               # was 0.0: base lacked X
        assert got[w[:9]][0] == "undecided", got                    # merged a day ago
        print("ok merge_time_is_when_it_landed_and_base_is_first_parent")


def test_final_state_is_kept_when_the_commit_goes_missing():
    outs, _ = oc.evaluate([_row("abcdef1234", None, 40, short=10)], [], NOW, cache={})
    assert outs[0]["state"] == "abandoned" and outs[0]["sticky"], outs
    for st in ("merged", "superseded", "reworked", "undecided"):
        stored = {("abcdef1234", "somekey"): (st, None)}            # scored under a clone key
        assert oc.pending(outs, stored) == [], st
    assert oc.pending(outs, {}) == outs                             # never seen: written once
    print("ok final_state_is_kept_when_the_commit_goes_missing")


def test_gh_failure_leaves_the_commit_undecided():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        url = "https://github.com/owner/repo.git"
        _g(work, "remote", "set-url", "origin", url)
        bindir = Path(d) / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text("#!/bin/sh\necho 'rate limited' >&2\nexit 1\n")
        (bindir / "gh").chmod(0o755)
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{old}"
        cache: dict = {}
        try:
            outs, stats = oc.evaluate([_row(s["F"], _key(url), 25)], clones, NOW, cache=cache,
                                      gh=True)
        finally:
            os.environ["PATH"] = old
        assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), outs
        assert stats["gh_failed"] == 1 and cache == {}, (stats, cache)
        print("ok gh_failure_leaves_the_commit_undecided")


def test_stale_default_ref_defers_survival():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        _g(work, "update-ref", "refs/remotes/origin/main", s["A"])   # last pulled at A's time
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        cache: dict = {}
        outs, stats = oc.evaluate([_row(s["A"], key, 30)], clones, NOW, cache=cache)
        assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), outs
        assert cache == {} and stats["stale_default"] == 1, (cache, stats)
        clones[0].fetched_at = NOW                                  # fetched this run: current
        outs, _ = oc.evaluate([_row(s["A"], key, 30)], clones, NOW, cache=cache)
        assert (outs[0]["state"], outs[0]["survival"]) == ("merged", 1.0), outs
        print("ok stale_default_ref_defers_survival")


def test_most_advanced_clone_wins():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        stale = Path(d) / "projects" / "stale"
        subprocess.run(["git", "clone", "-q", str(Path(d) / "origin.git"), str(stale)], check=True)
        _g(stale, "update-ref", "refs/remotes/origin/main", s["D"])  # never pulled since
        _g(stale, "branch", "-q", "topic", s["A"])                   # A only on a branch here
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert len(clones) == 2
        clones.sort(key=lambda c: c.path != str(stale))              # the stale clone first
        clones[0].fetched_at = clones[1].fetched_at = NOW
        outs, _ = oc.evaluate([_row(s["A"], _key(str(Path(d) / "origin.git")), 30)], clones,
                              NOW, cache={})
        assert outs[0]["state"] == "merged", outs
        print("ok most_advanced_clone_wins")


def test_local_branch_is_never_the_default():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d) / "projects" / "solo"
        work.mkdir(parents=True)
        _g(work, "init", "-q", "-b", "main")
        a = _commit(work, "a.txt", _lines("a"), 30)
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert clones[0].default is None, clones
        outs, _ = oc.evaluate([_row(a, None, 30)], clones, NOW, cache={})
        assert outs[0]["state"] == "open", outs                     # never pushed: not merged
        print("ok local_branch_is_never_the_default")


def test_amend_with_the_same_ts_is_ordered_after_the_commit():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        rows = [_row(s["G2"], key, 25, "sg", amend=True), _row(s["G"], key, 25, "sg")]
        outs, _ = oc.evaluate(rows, clones, NOW, cache={})            # same ts, amend listed first
        assert _states(outs)[s["G"][:9]][0] == "superseded", outs
        print("ok amend_with_the_same_ts_is_ordered_after_the_commit")


def test_outcome_repo_roots_must_be_a_list_of_strings():
    from agent_usage_tracker import config
    with tempfile.TemporaryDirectory() as d:
        conf = Path(d) / "config.toml"
        os.environ["AUT_CONFIG"] = str(conf)
        try:
            conf.write_text('machine = "t"\noutcome_repo_roots = "~/code"\n')
            assert config.load_config().outcome_repo_roots == ("~/code",)
            conf.write_text('machine = "t"\noutcome_repo_roots = 3\n')
            try:
                config.load_config()
            except SystemExit as exc:
                assert "outcome_repo_roots" in str(exc)
            else:
                raise AssertionError("a non-list must refuse")
        finally:
            os.environ.pop("AUT_CONFIG")
        print("ok outcome_repo_roots_must_be_a_list_of_strings")


def test_fetch_leaves_the_repo_alone_and_terminates_on_timeout():
    seen: list = []
    real = oc.subprocess.Popen

    def fake(cmd, **kw):
        if "fetch" in cmd:
            seen.append((cmd, kw.get("env", {}).get("GIT_TERMINAL_PROMPT")))
            return real(["sleep", "30"], **kw)                       # hangs like a dead remote
        return real(cmd, **kw)
    oc.subprocess.Popen, timeout = fake, oc.FETCH_TIMEOUT_S
    oc.FETCH_TIMEOUT_S = 0.3
    try:
        t = time.time()
        assert oc.fetch_clones([oc.Clone("/nonexistent", None, None, None)]) == (0, 1)
        assert time.time() - t < 10
    finally:
        oc.subprocess.Popen, oc.FETCH_TIMEOUT_S = real, timeout
    cmd, prompt = seen[0]
    for flag in ("gc.auto=0", "--no-auto-maintenance", "--no-write-fetch-head",
                 "--no-recurse-submodules", "--no-tags"):
        assert flag in cmd, (flag, cmd)
    assert prompt == "0" and "--prune" not in cmd
    print("ok fetch_leaves_the_repo_alone_and_terminates_on_timeout")


def test_cache_is_saved_before_the_sink_is_written():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        os.environ["AUT_STATE"] = str(Path(d) / "state" / "state.json")
        saved = (oc._load_rows, oc._load_stored, push.send_scan_rows)
        oc._load_rows = lambda dsn: [_row(s["D"], key, 40)]
        oc._load_stored = lambda dsn: (_ for _ in ()).throw(RuntimeError("no table"))
        push.send_scan_rows = lambda dsn, rows: None
        try:
            try:
                oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), fetch=False,
                                dry_run=False, now=NOW)
            except RuntimeError:
                pass
            cache = json.loads((Path(d) / "state" / oc.CACHE_NAME).read_text())
            assert cache == {s["D"]: 0.0}, cache
        finally:
            (oc._load_rows, oc._load_stored, push.send_scan_rows) = saved
            os.environ.pop("AUT_STATE")
        print("ok cache_is_saved_before_the_sink_is_written")


def _claude_session(proj: Path, sid: str, entrypoint: str, mtime: float) -> None:
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{sid}.jsonl"
    p.write_text(json.dumps({"type": "user", "entrypoint": entrypoint, "sessionId": sid}) + "\n")
    os.utime(p, (mtime, mtime))


def test_manual_outcome_picks_newest_interactive_session():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        work, s = _world(home)
        proj = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(work))
        _claude_session(proj, "old-cli", "cli", NOW - 500)
        _claude_session(proj, "new-cli", "cli", NOW - 100)
        _claude_session(proj, "scripted", "sdk-py", NOW - 10)
        _claude_session(home / ".claude" / "projects" / "-other", "elsewhere", "cli", NOW)
        cfg = _cfg(home)
        sub = work / "sub"
        sub.mkdir()
        row = oc.build_manual_outcome(cfg, "ruled-out", cwd=str(sub), now=NOW)
        assert row == {"session_id": "new-cli", "outcome": "ruled_out",
                       "ts": datetime.fromtimestamp(NOW, timezone.utc),
                       "repo_key": _key(str(home / "origin.git"))}, row
        row = oc.build_manual_outcome(cfg, "shipped", cwd=str(sub), session="given", now=NOW)
        assert row["session_id"] == "given" and row["outcome"] == "shipped"
        try:
            oc.build_manual_outcome(cfg, "open", cwd=str(home / "nowhere"), now=NOW)
        except SystemExit as exc:
            assert "--session" in str(exc)
        else:
            raise AssertionError("no session must refuse")
        print("ok manual_outcome_picks_newest_interactive_session")


def test_cli_outcome_and_outcomes():
    import contextlib
    import io

    from agent_usage_tracker import cli
    with tempfile.TemporaryDirectory() as d:
        conf = Path(d) / "config.toml"
        conf.write_text('machine = "t"\nrepo_key_salt = "x"\noutcome_fetch = true\n'
                        '[transport]\npg_dsn = "postgresql://x"\n')
        conf.chmod(0o600)
        os.environ["AUT_CONFIG"] = str(conf)
        got: dict = {}
        saved = (oc.insert_manual_outcome, oc.run_outcomes)
        oc.insert_manual_outcome = lambda dsn, row: got.update(row=row)
        oc.run_outcomes = lambda cfg, fetch, dry_run: (got.update(fetch=fetch, dry=dry_run)
                                                       or oc.Counter(rows=3, states=oc.Counter(merged=2)))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                assert cli.main(["outcome", "ruled-out", "--session", "abc"]) == 0
                assert cli.main(["outcomes", "--dry-run"]) == 0
        finally:
            oc.insert_manual_outcome, oc.run_outcomes = saved
            os.environ.pop("AUT_CONFIG")
        assert got["row"]["session_id"] == "abc" and got["row"]["outcome"] == "ruled_out"
        assert got["fetch"] is True and got["dry"] is True            # config turns fetch on
        assert '"merged": 2' in out.getvalue() and "DRY-RUN" in out.getvalue(), out.getvalue()
    print("ok cli_outcome_and_outcomes")


def test_repo_config_never_runs_commands():
    if os.name == "nt":                    # the fakes are POSIX shell scripts
        return
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        marks = Path(d) / "marks"
        marks.mkdir()
        git_dir = work / ".git"
        for ref in ("ctl1", "ctl2"):        # on origin but not fetched yet: each fetch adds a ref
            _g(work, "push", "-q", "origin", f"{s['A']}:refs/heads/{ref}")
            _g(work, "update-ref", "-d", f"refs/remotes/origin/{ref}")
        tools = {}
        for name in ("external", "textconv", "fsmonitor", "hook", "uploadpack"):
            sc = Path(d) / f"evil-{name}.sh"
            sc.write_text(f"#!/bin/sh\necho ran > '{marks / name}'\n")
            sc.chmod(0o755)
            tools[name] = str(sc)
        _g(work, "config", "diff.external", tools["external"])
        _g(work, "config", "diff.evil.textconv", tools["textconv"])
        _g(work, "config", "core.fsmonitor", tools["fsmonitor"])
        _g(work, "config", "remote.origin.uploadpack", tools["uploadpack"])
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "attributes").write_text("*.txt diff=evil\n")
        (work / ".gitattributes").write_text("*.txt diff=evil\n")
        hook = git_dir / "hooks" / "reference-transaction"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(Path(tools["hook"]).read_text())
        hook.chmod(0o755)

        def raw(*args):
            subprocess.run(["git", "-C", str(work), *args], capture_output=True, timeout=60)
        # control: plain git does run each of them, so the guarded run below proves something
        raw("log", "-p", "-1", s["A"])
        raw("status")
        raw("diff", "HEAD~1", "HEAD")
        raw("fetch", "origin")                                       # runs the uploadpack
        raw("fetch", "--upload-pack=git-upload-pack", "origin",
            "ctl1:refs/remotes/origin/ctl1")                         # runs the hook
        assert {p.name for p in marks.iterdir()} == set(tools), sorted(marks.iterdir())
        for p in marks.iterdir():
            p.unlink()

        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert oc.fetch_clones(clones) == (1, 0)                      # fetch to a local bare remote
        assert _g(work, "rev-parse", "refs/remotes/origin/ctl2") == s["A"]    # it did update refs
        outs, _ = oc.evaluate([_row(s["A"], key, 30), _row(s["B"], key, 30)], clones, NOW,
                              cache={})                             # log -p, patch-id, show, blame
        assert _states(outs)[s["A"][:9]][:2] == ("merged", 1.0), _states(outs)
        assert _states(outs)[s["B"][:9]][0] == "merged", _states(outs)     # via its patch-id twin
        assert RepoKeys(SALT).of_cwd(str(work)) == key
        for args in (("status",), ("diff", "HEAD~1", "HEAD"), ("blame", "HEAD", "--", "a.txt")):
            subprocess.run(git_argv(str(work), *args), capture_output=True, env=git_env(),
                           timeout=60)
        assert not list(marks.iterdir()), sorted(marks.iterdir())
    print("ok repo_config_never_runs_commands")


def test_signing_proxy_alternates_and_helpers_never_run():
    if os.name == "nt":                    # the fakes are POSIX shell scripts
        return
    import http.server
    import threading

    class Deny(http.server.BaseHTTPRequestHandler):       # every request asks for credentials
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Deny)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/r.git"
    saved_global = os.environ["GIT_CONFIG_GLOBAL"]
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        marks = Path(d) / "marks"
        marks.mkdir()
        tools = {}
        for name in ("gpg", "proxy", "alt", "cred", "credurl", "global"):
            sc = Path(d) / f"evil-{name}.sh"
            sc.write_text(f"#!/bin/sh\necho ran > '{marks / name}'\n")
            sc.chmod(0o755)
            tools[name] = str(sc)
        body = (f"tree {_g(work, 'rev-parse', 'HEAD^{tree}')}\nparent {s['A']}\n"
                "author t <t@example.com> 1 +0000\ncommitter t <t@example.com> 1 +0000\n"
                "gpgsig -----BEGIN PGP SIGNATURE-----\n \n x\n -----END PGP SIGNATURE-----\n\nsigned\n")
        signed = subprocess.run(["git", "-C", str(work), "hash-object", "-t", "commit", "-w",
                                 "--stdin"], input=body, capture_output=True, text=True,
                                check=True).stdout.strip()
        _g(work, "update-ref", "refs/heads/signed", signed)
        _g(work, "config", "log.showSignature", "true")
        _g(work, "config", "gpg.program", tools["gpg"])
        _g(work, "config", "core.gitProxy", tools["proxy"])
        _g(work, "remote", "add", "gp", "git://127.0.0.1:9/x")
        subprocess.run(["git", "init", "-q", "--bare", str(Path(d) / "alt.git")], check=True)
        (work / ".git" / "objects" / "info" / "alternates").write_text(
            str(Path(d) / "alt.git" / "objects") + "\n")
        _g(work, "config", "core.alternateRefsCommand", tools["alt"])
        _g(work, "config", "credential.helper", "!" + tools["cred"])
        _g(work, "config", f"credential.{url}.helper", "!" + tools["credurl"])
        gconf = Path(d) / "global.gitconfig"
        gconf.write_text(f"[credential]\n\thelper = !{tools['global']}\n")
        os.environ["GIT_CONFIG_GLOBAL"] = str(gconf)
        try:
            def run(argv):
                subprocess.run(argv, capture_output=True, timeout=60, env=git_env())

            def plain(argv):                       # the control: git as the user runs it
                subprocess.run(argv, capture_output=True, timeout=60, env=dict(os.environ))

            def new_ref(name):                     # a fetch with nothing new never negotiates
                _g(work, "push", "-q", "origin", f"{s['A']}:refs/heads/{name}")
                _g(work, "update-ref", "-d", f"refs/remotes/origin/{name}")
            # control: plain git runs each of them
            g = ["git", "-C", str(work)]
            plain([*g, "log", "--format=%H", "-1", signed])
            plain([*g, "fetch", "gp"])
            new_ref("ctl1")
            plain([*g, "fetch", "origin"])
            plain([*g, "fetch", url])
            assert {p.name for p in marks.iterdir()} == set(tools), sorted(marks.iterdir())
            for p in marks.iterdir():
                p.unlink()
            run(git_argv(str(work), "log", "--format=%H %ct %P", "-1", signed))
            run(git_argv(str(work), "show", "-s", signed))
            run(git_argv(str(work), "fetch", "gp"))
            new_ref("ctl2")
            run(git_argv(str(work), "fetch", "origin"))
            assert _g(work, "rev-parse", "refs/remotes/origin/ctl2") == s["A"]   # fetch still works
            # the test server speaks plain http, which git_env() refuses: allow it for this run only
            argv = git_argv(str(work), "fetch", url)
            subprocess.run([argv[0], "-c", "protocol.http.allow=always", *argv[1:]],
                           capture_output=True, timeout=60,
                           env={**git_env(), "GIT_ALLOW_PROTOCOL": "https:ssh:file:http"})
            # the repo's helpers are gone, the user's global one still answers
            assert {p.name for p in marks.iterdir()} == {"global"}, sorted(marks.iterdir())
        finally:
            os.environ["GIT_CONFIG_GLOBAL"] = saved_global
            srv.shutdown()
    print("ok signing_proxy_alternates_and_helpers_never_run")


def test_fetch_only_clones_with_a_row_key():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        fetched: list = []
        os.environ["AUT_STATE"] = str(Path(d) / "state" / "state.json")
        saved = (oc._load_rows, oc.load_json_dict, oc._load_stored, oc.fetch_clones, oc._insert,
                 push.send_scan_rows)
        oc._load_rows = lambda dsn: [_row(s["A"], None, 30)]       # the SHA is there, no key
        oc.load_json_dict, oc._load_stored = (lambda p: {}), lambda dsn: {}
        oc.fetch_clones = lambda cs: (fetched.extend(cs), (len(cs), 0))[1]
        oc._insert, push.send_scan_rows = (lambda dsn, rows: None), lambda dsn, rows: None
        try:
            oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), fetch=True,
                            dry_run=False, now=NOW)
            assert fetched == [], fetched
            oc._load_rows = lambda dsn: [_row(s["A"], _key(str(Path(d) / "origin.git")), 30)]
            oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), fetch=True,
                            dry_run=False, now=NOW)
            assert [c.path for c in fetched] == [str(work)], fetched
        finally:
            (oc._load_rows, oc.load_json_dict, oc._load_stored, oc.fetch_clones, oc._insert,
             push.send_scan_rows) = saved
            os.environ.pop("AUT_STATE")
    print("ok fetch_only_clones_with_a_row_key")


def test_dry_run_never_fetches_or_asks_gh():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        saved = (oc._load_rows, oc._load_stored, oc.fetch_clones, oc._gh_merged)
        oc._load_rows = lambda dsn: [_row(s["F"], key, 25)]
        oc._load_stored = lambda dsn: {}
        oc.fetch_clones = lambda cs: (_ for _ in ()).throw(AssertionError("fetched"))
        oc._gh_merged = lambda *a: (_ for _ in ()).throw(AssertionError("asked gh"))
        try:
            stats = oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), fetch=True,
                                    dry_run=True, now=NOW)
        finally:
            oc._load_rows, oc._load_stored, oc.fetch_clones, oc._gh_merged = saved
        assert stats["fetch_skipped_dry_run"] == 1 and "fetch_ok" not in stats, stats
    print("ok dry_run_never_fetches_or_asks_gh")


def _failing_git(match):
    """oc._git, except that a call whose arguments satisfy match fails as a timeout would."""
    real = oc._git

    def fake(path, *args, **kw):
        return None if match(args) else real(path, *args, **kw)
    return fake


def _with_git(match, fn):
    real = oc._git
    oc._git = _failing_git(match)
    try:
        return fn()
    finally:
        oc._git = real


def test_a_git_failure_never_replaces_a_stored_state():
    reads = {"default log": lambda a: a[0] == "log" and "--format=%H %ct %P" in a,
             "branches": lambda a: a[0] == "rev-list" and "--branches" in a,
             "reverts": lambda a: a[0] == "log" and "--grep=This reverts commit" in a,
             "lookup": lambda a: a[0] == "cat-file"}
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        rows = [_row(s["A"], key, 30)]
        for name, match in reads.items():
            cache: dict = {}
            outs, stats = _with_git(match, lambda: oc.evaluate(rows, clones, NOW, cache=cache))
            assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), (name, outs)
            assert stats["repo_unreadable"] == 1 and cache == {}, (name, stats, cache)
            assert oc.pending(outs, {(s["A"][:9], key): ("merged", 1.0)}) == [], name
        outs, _ = oc.evaluate(rows, clones, NOW, cache={})
        assert outs[0]["state"] == "merged" and "sticky" not in outs[0], outs   # healthy again
    print("ok a_git_failure_never_replaces_a_stored_state")


def test_a_clone_that_cannot_answer_may_know_more():
    """The stale clone says open; the current one fails: do not take the stale answer."""
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        stale = Path(d) / "projects" / "stale"
        subprocess.run(["git", "clone", "-q", str(Path(d) / "origin.git"), str(stale)], check=True)
        _g(stale, "update-ref", "refs/remotes/origin/main", s["D"])
        _g(stale, "branch", "-q", "topic", s["A"])
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        rows = [_row(s["A"], _key(str(Path(d) / "origin.git")), 30)]
        real = oc._lookup
        oc._lookup = lambda c, shas: None if c.path == str(work) else real(c, shas)
        try:
            outs, stats = oc.evaluate(rows, clones, NOW, cache={})
        finally:
            oc._lookup = real
        assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), outs
        assert stats["repo_unreadable"] == 1, stats
    print("ok a_clone_that_cannot_answer_may_know_more")


def test_patch_id_failure_is_not_an_empty_answer():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        assert s["A"] in oc._patch_ids(str(work), ["-1", s["A"]])
        assert oc._patch_ids(str(work), ["refs/heads/no-such-ref"]) is None
    print("ok patch_id_failure_is_not_an_empty_answer")


def test_default_ref_failure_is_not_no_default():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        assert oc._default_ref(str(work)) == ("refs/remotes/origin/main", True)
        _g(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
        assert oc._default_ref(str(work)) == ("refs/remotes/origin/main", True)   # dangling HEAD
        _g(work, "update-ref", "-d", "refs/remotes/origin/main")
        assert oc._default_ref(str(work)) == (None, True)            # no default: a real answer
        clones = _with_git(lambda a: a[0] == "for-each-ref",
                           lambda: oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT)))
        assert clones[0].default is None and not clones[0].readable, clones
        outs, _ = oc.evaluate([_row(s["A"], None, 30)], clones, NOW, cache={})
        assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), outs
    print("ok default_ref_failure_is_not_no_default")


def test_failed_survival_is_neither_scored_nor_cached():
    reads = {"blame": lambda a: a[0] == "blame" or a[0] == "ls-tree",
             "blame only": lambda a: a[0] == "blame",          # the file is there: a real failure
             "numstat": lambda a: a[0] == "show",
             "parents": lambda a: a[0] == "rev-list" and "--parents" in a,
             "base": lambda a: a[0] == "rev-list" and "--first-parent" in a}
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        rows = [_row(s["A"], key, 30)]
        cache: dict = {}
        for name, match in reads.items():
            outs, stats = _with_git(match, lambda: oc.evaluate(rows, clones, NOW, cache=cache))
            assert (outs[0]["state"], outs[0]["survival"], outs[0]["sticky"]) == \
                ("undecided", None, True), (name, outs)
            assert cache == {} and stats["survival_failed"] == 1, (name, cache, stats)
            assert oc.pending(outs, {(s["A"][:9], key): ("merged", 1.0)}) == [], name
        outs, stats = oc.evaluate(rows, clones, NOW, cache=cache)       # measured on the next run
        assert (outs[0]["state"], outs[0]["survival"]) == ("merged", 1.0), outs
        assert cache == {s["A"]: 1.0} and stats["survival_computed"] == 1, (cache, stats)
    print("ok failed_survival_is_neither_scored_nor_cached")


def test_file_deleted_by_the_base_counts_as_zero_alive():
    with tempfile.TemporaryDirectory() as d:
        origin, work = Path(d) / "origin.git", Path(d) / "projects" / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        work.mkdir(parents=True)
        _g(work, "init", "-q", "-b", "main")
        _g(work, "remote", "add", "origin", str(origin))
        _commit(work, "base.txt", "base\n", 60)
        x = _commit(work, "x.txt", _lines("x"), 40)
        _g(work, "rm", "-q", "x.txt")
        _g(work, "commit", "-q", "-m", "drop x", age=35)
        _commit(work, "later.txt", "later\n", 1)
        _g(work, "push", "-q", "origin", "main")
        _g(work, "remote", "set-head", "origin", "main")
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        cache: dict = {}
        outs, _ = oc.evaluate([_row(x, _key(str(origin)), 40)], clones, NOW, cache=cache)
        assert (outs[0]["state"], outs[0]["survival"]) == ("reworked", 0.0), outs
        assert cache == {x: 0.0}, cache
    print("ok file_deleted_by_the_base_counts_as_zero_alive")


def test_a_clone_with_no_branches_still_answers():
    """No branch or remote ref at all: git exits 0 with nothing, which is not a failure."""
    with tempfile.TemporaryDirectory() as d:
        work = Path(d) / "projects" / "solo"
        work.mkdir(parents=True)
        _g(work, "init", "-q", "-b", "main")
        a = _commit(work, "a.txt", _lines("a"), 30)
        _g(work, "checkout", "-q", "--detach")
        _g(work, "branch", "-q", "-D", "main")
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        outs, stats = oc.evaluate([_row(a, None, 30)], clones, NOW, cache={})
        assert outs[0]["state"] == "abandoned" and not stats["repo_unreadable"], (outs, stats)
    print("ok a_clone_with_no_branches_still_answers")


def test_ctrl_c_sends_no_heartbeat():
    def stop(dsn):
        raise KeyboardInterrupt
    sent: list = []
    saved = (oc._load_rows, push.send_scan_rows)
    oc._load_rows, push.send_scan_rows = stop, lambda dsn, rows: sent.extend(rows)
    try:
        try:
            oc.run_outcomes(_cfg(Path(tempfile.gettempdir())), fetch=False, dry_run=False, now=NOW)
            raise AssertionError("KeyboardInterrupt must propagate")
        except KeyboardInterrupt:
            pass
    finally:
        oc._load_rows, push.send_scan_rows = saved
    assert sent == [], sent
    print("ok ctrl_c_sends_no_heartbeat")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_repo_cannot_switch_a_protocol_back_on():
    # protocol.<name>.allow beats protocol.allow, so a repo setting protocol.git.allow=always
    # would turn git:// (and with it the repo's own core.gitProxy) back on; GIT_ALLOW_PROTOCOL wins.
    with tempfile.TemporaryDirectory() as d:
        work, _s = _world(Path(d))
        mark = Path(d) / "proxy-ran"
        sc = Path(d) / "evil-proxy.sh"
        sc.write_text(f"#!/bin/sh\necho ran > '{mark}'\n")
        sc.chmod(0o755)
        _g(work, "config", "protocol.git.allow", "always")
        _g(work, "config", "core.gitProxy", str(sc))
        _g(work, "remote", "add", "gp", "git://127.0.0.1:9/x")
        plain = subprocess.run(["git", "-C", str(work), "-c", "protocol.allow=never", "fetch",
                                "-q", "gp"], capture_output=True, timeout=60)
        assert plain.returncode != 0 and mark.exists(), "the repo setting must win without the guard"
        mark.unlink()
        r = subprocess.run(git_argv(str(work), "fetch", "-q", "gp"), capture_output=True,
                           timeout=60, env=git_env())
        assert r.returncode != 0 and not mark.exists(), r.stderr
