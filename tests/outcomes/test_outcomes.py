"""Hermetic tests for `aut outcomes` and `aut outcome` on temp git repos (real git, synthetic commits).

Locks the rules:
  - merged: ancestor of origin/HEAD, or a single-commit patch-id twin on it, or (network on) a
    merged GitHub PR found by gh; reverted within 14 days of the merge; reworked when under half
    the added lines survive 14 days on; open on a branch only; abandoned when unreachable;
    a pre-amend SHA is superseded; everything is undecided until day 14;
  - a row with no repo_key takes the key of the clone that holds the SHA, but only when the
    committer time is within a day of the row;
  - survival is computed once and cached;
  - only changed states are written, a dry run writes nothing and never fetches or asks gh, a
    real run sends the heartbeat;
  - the merge time is when the commit landed (first parent), survival waits for a default ref
    newer than merge + 14 days, the furthest-along clone wins, a commit gone missing or a failed
    gh lookup or git read never replaces a stored state, a failed git read is never scored or
    cached as survival;
  - the manual outcome picks the newest interactive session of the current project;
  - --fetch only fetches clones whose repo_key a commit row carries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_usage_tracker import outcome_git as og
from agent_usage_tracker import outcomes as oc
from agent_usage_tracker import push
from agent_usage_tracker.repo_keys import RepoKeys, repo_identity
from agent_usage_tracker.config import AutError, Config

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull      # the user's git config never reaches the tests
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
SALT = "test-salt-for-outcomes"
DAY = 86400
NOW = time.time()
NOW_DT = datetime.fromtimestamp(NOW, timezone.utc)


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


def test_null_key_resolved_counts_the_chosen_clone_of_each_anchor():
    with tempfile.TemporaryDirectory() as d:
        _, s = _world(Path(d))
        local = Path(d) / "projects" / "local"             # no origin, so a clone without a key
        local.mkdir()
        _g(local, "init", "-q", "-b", "main")
        only_local = _commit(local, "l.txt", _lines("l"), 30)
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        assert sorted(c.key is None for c in clones) == [False, True], clones
        rows = [_row(s["A"], None, 30), _row(only_local, None, 30)]
        for order in (rows, rows[::-1]):                   # one anchor resolves to a keyed clone
            _, stats = oc.evaluate(order, clones, NOW, cache={})
            assert stats["found"] == 2 and stats["null_key_resolved"] == 1, stats


def test_young_missing_commit_is_undecided():
    outs, stats = oc.evaluate([_row("abcdef1234", None, 3, short=10)], [], NOW, cache={})
    assert _states(outs)["abcdef1234"][0] == "undecided", outs


def test_survival_cached_and_never_recomputed(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        cache: dict = {}
        oc.evaluate([_row(s["D"], key, 40)], clones, NOW, cache=cache)
        assert cache == {s["D"]: 0.0}, cache
        monkeypatch.setattr(oc, "survival", lambda *a: (_ for _ in ()).throw(AssertionError("blame")))
        outs, stats = oc.evaluate([_row(s["D"], key, 40)], clones, NOW, cache=cache)
        assert _states(outs)[s["D"][:9]][:2] == ("reworked", 0.0)
        assert stats["survival_cached"] == 1 and stats["survival_computed"] == 0, stats


def test_gh_merged_pr_counts_as_merged(monkeypatch):
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
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        outs, stats = oc.evaluate(rows, clones, NOW, cache={}, use_gh=True)
        off, _ = oc.evaluate(rows, clones, NOW, cache={}, use_gh=False)
        assert _states(outs)[s["F"][:9]][:2] == ("merged", None), outs
        assert _states(outs)[s["E"][:9]][0] == "open", outs        # gh said no
        assert _states(off)[s["F"][:9]][0] == "abandoned", off       # gh only with the network on
        assert stats["gh_lookups"] == 2, stats


def test_pending_writes_only_changes():
    outs = [{"sha": "a" * 9, "repo_key": "k", "state": "merged", "survival": 1.0},
            {"sha": "b" * 9, "repo_key": "k", "state": "open", "survival": None},
            {"sha": "c" * 9, "repo_key": "", "state": "undecided", "survival": None}]
    stored = {("a" * 9, "k"): ("merged", 1.0), ("b" * 9, "k"): ("undecided", None)}
    assert [o["sha"][0] for o in oc.pending(outs, stored)] == ["b", "c"]


def test_run_outcomes_dry_run_writes_nothing(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        monkeypatch.setenv("AUT_STATE", str(Path(d) / "state" / "state.json"))
        sent: dict = {"rows": [], "scan": []}
        monkeypatch.setattr(oc, "load_rows", lambda dsn: [_row(s["A"], key, 30), _row(s["D"], key, 40)])
        monkeypatch.setattr(oc, "load_stored", lambda dsn: {})
        monkeypatch.setattr(oc, "insert_outcomes", lambda dsn, rows: sent["rows"].extend(rows))
        monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: sent["scan"].extend(rows))
        cfg = _cfg(Path(d), (str(Path(d) / "projects"),))
        stats = oc.run_outcomes(cfg, refresh=False, dry_run=True, now=NOW_DT)
        assert sent == {"rows": [], "scan": []} and stats["to_write"] == 2, (sent, stats)
        assert not (Path(d) / "state").exists()                # no cache file either
        stats = oc.run_outcomes(cfg, refresh=False, dry_run=False, now=NOW_DT)
        assert {r["state"] for r in sent["rows"]} == {"merged", "reworked"}, sent
        assert all(set(r) == {"sha", "repo_key", "state", "survival", "decided_at"}
                   for r in sent["rows"])
        assert [(r["collector"], r["status"], r["events"]) for r in sent["scan"]] == \
            [("outcomes", "ok", 2)]
        cache = json.loads((Path(d) / "state" / oc.CACHE_NAME).read_text())
        assert cache == {"v": oc._CACHE_V, s["A"]: 1.0, s["D"]: 0.0}, cache


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


def test_final_state_is_kept_when_the_commit_goes_missing():
    outs, _ = oc.evaluate([_row("abcdef1234", None, 40, short=10)], [], NOW, cache={})
    assert outs[0]["state"] == "abandoned" and outs[0]["sticky"], outs
    for st in ("merged", "superseded", "reworked", "undecided"):
        stored = {("abcdef1234", "somekey"): (st, None)}            # scored under a clone key
        assert oc.pending(outs, stored) == [], st
    assert oc.pending(outs, {}) == outs                             # never seen: written once


def test_gh_failure_leaves_the_commit_undecided(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        url = "https://github.com/owner/repo.git"
        _g(work, "remote", "set-url", "origin", url)
        bindir = Path(d) / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text("#!/bin/sh\necho 'rate limited' >&2\nexit 1\n")
        (bindir / "gh").chmod(0o755)
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        cache: dict = {}
        outs, stats = oc.evaluate([_row(s["F"], _key(url), 25)], clones, NOW, cache=cache,
                                  use_gh=True)
        assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), outs
        assert stats["gh_failed"] == 1 and cache == {}, (stats, cache)


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


def test_amend_with_the_same_ts_is_ordered_after_the_commit():
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        rows = [_row(s["G2"], key, 25, "sg", amend=True), _row(s["G"], key, 25, "sg")]
        outs, _ = oc.evaluate(rows, clones, NOW, cache={})            # same ts, amend listed first
        assert _states(outs)[s["G"][:9]][0] == "superseded", outs


def test_outcome_repo_roots_must_be_a_list_of_strings(monkeypatch):
    from agent_usage_tracker import config
    with tempfile.TemporaryDirectory() as d:
        conf = Path(d) / "config.toml"
        monkeypatch.setenv("AUT_CONFIG", str(conf))
        conf.write_text('machine = "t"\noutcome_repo_roots = "~/code"\n')
        assert config.load_config().outcome_repo_roots == ("~/code",)
        conf.write_text('machine = "t"\noutcome_repo_roots = 3\n')
        try:
            config.load_config()
        except AutError as exc:
            assert "outcome_repo_roots" in str(exc)
        else:
            raise AssertionError("a non-list must refuse")


def test_cache_is_saved_before_the_sink_is_written(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        monkeypatch.setenv("AUT_STATE", str(Path(d) / "state" / "state.json"))
        monkeypatch.setattr(oc, "load_rows", lambda dsn: [_row(s["D"], key, 40)])
        monkeypatch.setattr(oc, "load_stored", lambda dsn: (_ for _ in ()).throw(RuntimeError("no table")))
        monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: None)
        try:
            oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), refresh=False,
                            dry_run=False, now=NOW_DT)
        except RuntimeError:
            pass
        cache = json.loads((Path(d) / "state" / oc.CACHE_NAME).read_text())
        assert cache == {"v": oc._CACHE_V, s["D"]: 0.0}, cache


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
        row = oc.build_manual_outcome(cfg, "ruled-out", cwd=str(sub), now=NOW_DT)
        assert row == {"session_id": "new-cli", "outcome": "ruled_out",
                       "ts": NOW_DT,
                       "repo_key": _key(str(home / "origin.git"))}, row
        row = oc.build_manual_outcome(cfg, "shipped", cwd=str(sub), session="given", now=NOW_DT)
        assert row["session_id"] == "given" and row["outcome"] == "shipped"
        try:
            oc.build_manual_outcome(cfg, "open", cwd=str(home / "nowhere"), now=NOW_DT)
        except AutError as exc:
            assert "--session" in str(exc)
        else:
            raise AssertionError("no session must refuse")


def test_cli_outcome_and_outcomes(monkeypatch):
    import contextlib
    import io

    from agent_usage_tracker import cli
    with tempfile.TemporaryDirectory() as d:
        conf = Path(d) / "config.toml"
        conf.write_text('machine = "t"\nrepo_key_salt = "x"\noutcome_fetch = true\n'
                        '[transport]\npg_dsn = "postgresql://x"\n')
        conf.chmod(0o600)
        monkeypatch.setenv("AUT_CONFIG", str(conf))
        got: dict = {}
        monkeypatch.setattr(oc, "insert_manual_outcome", lambda dsn, row: got.update(row=row))
        monkeypatch.setattr(oc, "run_outcomes", lambda cfg, refresh, dry_run: (
            got.update(fetch=refresh, dry=dry_run)
            or oc.Counter(rows=3, states=oc.Counter(merged=2))))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(["outcome", "ruled-out", "--session", "abc"]) == 0
            assert cli.main(["outcomes", "--dry-run"]) == 0
        assert got["row"]["session_id"] == "abc" and got["row"]["outcome"] == "ruled_out"
        assert got["fetch"] is True and got["dry"] is True            # config turns fetch on
        assert '"merged": 2' in out.getvalue() and "DRY-RUN" in out.getvalue(), out.getvalue()


def test_fetch_only_clones_with_a_row_key(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        fetched: list = []
        monkeypatch.setenv("AUT_STATE", str(Path(d) / "state" / "state.json"))
        monkeypatch.setattr(oc, "load_rows", lambda dsn: [_row(s["A"], None, 30)])       # the SHA is there, no key
        monkeypatch.setattr(oc, "load_json_dict", lambda p: {})
        monkeypatch.setattr(oc, "load_stored", lambda dsn: {})
        monkeypatch.setattr(oc, "fetch_clones", lambda cs: (fetched.extend(cs), (len(cs), 0))[1])
        monkeypatch.setattr(oc, "insert_outcomes", lambda dsn, rows: None)
        monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: None)
        oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), refresh=True,
                        dry_run=False, now=NOW_DT)
        assert fetched == [], fetched
        monkeypatch.setattr(oc, "load_rows", lambda dsn: [_row(s["A"], _key(str(Path(d) / "origin.git")), 30)])
        oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), refresh=True,
                        dry_run=False, now=NOW_DT)
        assert [c.path for c in fetched] == [str(work)], fetched


def test_dry_run_never_fetches_or_asks_gh(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        monkeypatch.setattr(oc, "load_rows", lambda dsn: [_row(s["F"], key, 25)])
        monkeypatch.setattr(oc, "load_stored", lambda dsn: {})
        monkeypatch.setattr(oc, "fetch_clones", lambda cs: (_ for _ in ()).throw(AssertionError("fetched")))
        monkeypatch.setattr(oc, "gh_merged", lambda *a: (_ for _ in ()).throw(AssertionError("asked gh")))
        stats = oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), refresh=True,
                                dry_run=True, now=NOW_DT)
        assert stats["fetch_skipped_dry_run"] == 1 and "fetch_ok" not in stats, stats


def _failing_git(match):
    """og.run_git, except that a call whose arguments satisfy match fails as a timeout would."""
    real = og.run_git

    def fake(path, *args, **kw):
        return None if match(args) else real(path, *args, **kw)
    return fake


def _with_git(match, fn):
    with pytest.MonkeyPatch.context() as m:
        m.setattr(og, "run_git", _failing_git(match))
        return fn()


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


def test_a_clone_that_cannot_answer_may_know_more(monkeypatch):
    """The stale clone says open; the current one fails: do not take the stale answer."""
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        stale = Path(d) / "projects" / "stale"
        subprocess.run(["git", "clone", "-q", str(Path(d) / "origin.git"), str(stale)], check=True)
        _g(stale, "update-ref", "refs/remotes/origin/main", s["D"])
        _g(stale, "branch", "-q", "topic", s["A"])
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        rows = [_row(s["A"], _key(str(Path(d) / "origin.git")), 30)]
        real = oc.resolve_shas
        monkeypatch.setattr(oc, "resolve_shas", lambda c, shas: None if c.path == str(work) else real(c, shas))
        outs, stats = oc.evaluate(rows, clones, NOW, cache={})
        assert (outs[0]["state"], outs[0]["sticky"]) == ("undecided", True), outs
        assert stats["repo_unreadable"] == 1, stats


def test_a_cached_gh_answer_counts_without_gh():
    with tempfile.TemporaryDirectory() as d:
        _work, s = _world(Path(d))
        clones = oc.find_clones([str(Path(d) / "projects")], RepoKeys(SALT))
        outs, stats = oc.evaluate([_row(s["F"], None, 25)], clones, NOW,
                                  cache={f"gh:{s['F']}": True}, use_gh=False)
        assert outs[0]["state"] == "merged" and stats["gh_lookups"] == 0, (outs, stats)


def test_a_cache_from_before_the_failed_read_rule_is_measured_again(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("AUT_STATE", str(Path(d) / "state.json"))
        (Path(d) / oc.CACHE_NAME).write_text(json.dumps({"abc": 0.1, "gh:def": True}))
        cache = oc._load_cache()
        assert "abc" not in cache and cache["gh:def"] is True, cache
        (Path(d) / oc.CACHE_NAME).write_text(json.dumps({**cache, "abc": 0.1}))
        assert oc._load_cache()["abc"] == 0.1


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


def test_ctrl_c_sends_no_heartbeat(monkeypatch):
    def stop(dsn):
        raise KeyboardInterrupt
    sent: list = []
    monkeypatch.setattr(oc, "load_rows", stop)
    monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: sent.extend(rows))
    try:
        oc.run_outcomes(_cfg(Path(tempfile.gettempdir())), refresh=False, dry_run=False, now=NOW_DT)
        raise AssertionError("KeyboardInterrupt must propagate")
    except KeyboardInterrupt:
        pass
    assert sent == [], sent


def test_the_keyed_anchor_wins_a_duplicate_whatever_the_row_order(monkeypatch):
    keyed, null = _row("abcdef1234", "K", 3), _row("abcdef1234", None, 3)
    monkeypatch.setattr(oc, "_resolve", lambda anchors, clones, stats: None)
    monkeypatch.setattr(oc, "_decide", lambda a, *rest: ({
        "sha": a.sha, "repo_key": "K", "survival": None,
        "state": "merged" if a.key else "undecided"}, None))
    for rows in ([keyed, null], [null, keyed]):
        outs, _ = oc.evaluate(rows, [], NOW, cache={})
        assert [o["state"] for o in outs] == ["merged"], rows


def test_a_read_failure_sends_a_degraded_heartbeat(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as d:
        work, s = _world(Path(d))
        key = _key(str(Path(d) / "origin.git"))
        monkeypatch.setenv("AUT_STATE", str(Path(d) / "state" / "state.json"))
        scan: list = []
        monkeypatch.setattr(oc, "load_rows", lambda dsn: [_row(s["A"], key, 30)])
        monkeypatch.setattr(oc, "load_stored", lambda dsn: {})
        monkeypatch.setattr(oc, "insert_outcomes", lambda dsn, rows: None)
        monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: scan.extend(rows))
        real = oc.evaluate
        monkeypatch.setattr(oc, "evaluate", lambda *a, **k: (real(*a, **k)[0], {"survival_failed": 1}))
        oc.run_outcomes(_cfg(Path(d), (str(Path(d) / "projects"),)), refresh=False, dry_run=False,
                        now=NOW_DT)
        assert [(r["status"], r["error"]) for r in scan] == [("degraded", "GitReadFailed")], scan
        assert '"survival_failed": 1' in capsys.readouterr().err
