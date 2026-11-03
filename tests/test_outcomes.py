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
  - only changed states are written, a dry run writes nothing, a real run sends the heartbeat;
  - the manual outcome picks the newest interactive session of the current project.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import outcomes as oc  # noqa: E402
from agent_usage_tracker.collectors._activity_common import RepoKeys, repo_identity  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402

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
        assert len(outs) == 3 and stats["state_merged"] == 1, outs  # written once
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
        real = oc.subprocess.run
        seen = []

        def slow(cmd, **kw):
            if "fetch" in cmd:
                seen.append(kw.get("timeout"))
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return real(cmd, **kw)
        oc.subprocess.run = slow
        try:
            assert oc.fetch_clones(clones) == (0, 2)
        finally:
            oc.subprocess.run = real
        assert seen == [oc.FETCH_TIMEOUT_S] * 2 and oc.FETCH_TIMEOUT_S == 30
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
        saved = (oc._load_rows, oc._load_stored, oc._insert, oc.send_scan_rows)
        oc._load_rows = lambda dsn: [_row(s["A"], key, 30), _row(s["D"], key, 40)]
        oc._load_stored = lambda dsn: {}
        oc._insert = lambda dsn, rows: sent["rows"].extend(rows)
        oc.send_scan_rows = lambda cfg, rows: sent["scan"].extend(rows)
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
            (oc._load_rows, oc._load_stored, oc._insert, oc.send_scan_rows) = saved
            os.environ.pop("AUT_STATE")
        print("ok run_outcomes_dry_run_writes_nothing")


def _claude_session(proj: Path, sid: str, entrypoint: str, mtime: float) -> None:
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{sid}.jsonl"
    p.write_text(json.dumps({"type": "user", "entrypoint": entrypoint, "sessionId": sid}) + "\n")
    os.utime(p, (mtime, mtime))


def test_manual_outcome_picks_newest_interactive_session():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        work, s = _world(home)
        proj = home / ".claude" / "projects" / oc.claude_project_dir(str(work))
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
        saved = (oc.insert_manual, oc.run_outcomes)
        oc.insert_manual = lambda dsn, row: got.update(row=row)
        oc.run_outcomes = lambda cfg, fetch, dry_run: (got.update(fetch=fetch, dry=dry_run)
                                                       or oc.Counter(state_merged=2, rows=3))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                assert cli.main(["outcome", "ruled-out", "--session", "abc"]) == 0
                assert cli.main(["outcomes", "--dry-run"]) == 0
        finally:
            oc.insert_manual, oc.run_outcomes = saved
            os.environ.pop("AUT_CONFIG")
        assert got["row"]["session_id"] == "abc" and got["row"]["outcome"] == "ruled_out"
        assert got["fetch"] is True and got["dry"] is True            # config turns fetch on
        assert '"merged": 2' in out.getvalue() and "DRY-RUN" in out.getvalue(), out.getvalue()
    print("ok cli_outcome_and_outcomes")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
