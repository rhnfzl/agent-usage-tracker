"""`aut git-days`: the owner's commits per local day, counts only, to agents.git_commit_day.

Locks the rules:
  - only the configured identities count; a shared author only in repos whose name matches; a bot
    never; a SHA once across every store; nothing before `since`, nothing after yesterday, zeros
    included;
  - a run writes only days that are missing or went up, all with one scanned_at; a lower day is
    only counted in the summary unless --allow-lower;
  - a partial scan (a missing root, a failed host, folder or store, an old remote git), zero stores
    or under half the best recent run's stores writes nothing and keeps the last good state;
  - `aut scan` runs it at most once per 20 h when enabled, retries a failure after 6 h, and its
    failure never fails the scan;
  - no repo name, SHA or email reaches the output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_usage_tracker import cli, git_days, push, scan
from agent_usage_tracker.config import AutError, Config, load_config
from agent_usage_tracker.state import load_state
from fake_psycopg import _fake_psycopg

TZ = timezone(timedelta(hours=1))           # a fixed zone: Windows CI has no tz database
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)      # yesterday is 2026-11-05
ME, ALIAS, SHARED, BOT = "me@example.com", "Me@Example.org", "shared@example.invalid", \
    "claude[bot]@users.noreply.github.com"


def _cfg(tmp: Path, **kw) -> Config:
    base = dict(machine="t", agents=(), webhook_url=None, webhook_secret=None,
                pg_dsn="postgresql://x", home=tmp, git_days_enabled=True,
                git_days_since=date(2026, 11, 1),
                git_days_identities=(ME, ALIAS.lower()),
                git_days_shared_authors=((r"^shared@", r"^demo_.*_tool$"),),
                git_days_roots=(str(tmp / "projects"),))
    base.update(kw)
    return Config(**base)  # type: ignore[arg-type]


def _g(repo: Path, *args: str, email: str = ME, when: str | None = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME="a",
               GIT_COMMITTER_EMAIL=email, GIT_CONFIG_NOSYSTEM="1")
    if sys.platform != "win32":     # no signing or hooks from the user's config (Windows: no /dev/null)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _repo(path: Path, commits: list[tuple[str, str]]) -> None:
    """commits: (author email, ISO author date)."""
    path.mkdir(parents=True)
    _g(path, "init", "-q", "-b", "main")
    for i, (email, when) in enumerate(commits):
        _g(path, "commit", "-q", "--allow-empty", "-m", f"c{i}", email=email, when=when)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two repos and a worktree under projects/, a vendored repo that is skipped."""
    p = tmp_path / "projects"
    _repo(p / "app", [(ME, "2026-11-02T10:00:00+00:00"),
                      (ALIAS, "2026-11-02T23:30:00+00:00"),      # 00:30 on 11-03 at +01:00
                      (SHARED, "2026-11-03T10:00:00+00:00"),     # shared, but not its repo
                      (BOT, "2026-11-03T11:00:00+00:00"),
                      ("someone@else.com", "2026-11-04T10:00:00+00:00"),
                      (ME, "2026-10-31T10:00:00+00:00"),         # before since
                      (ME, "2026-11-06T08:00:00+00:00")])        # today: not yet
    _repo(p / "demo_fn_tool", [(SHARED, "2026-11-04T10:00:00+00:00")])
    _g(p / "app", "worktree", "add", "-q", str(p / "app-wt"), "-b", "wt")
    _repo(p / "app" / "node_modules" / "dep", [(ME, "2026-11-04T10:00:00+00:00")])
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    return tmp_path


EXPECTED = {date(2026, 11, 1): 0, date(2026, 11, 2): 1, date(2026, 11, 3): 1,
            date(2026, 11, 4): 1, date(2026, 11, 5): 0}


def test_local_scan_counts_only_the_owner_per_local_day(world):
    found = git_days.scan_all(_cfg(world), TZ)
    assert (found.stores, found.failed) == (2, 0)      # the worktree and node_modules are not stores
    assert git_days.day_counts(found.commits, date(2026, 11, 1), date(2026, 11, 5)) == EXPECTED


def test_a_sha_in_two_stores_counts_once(world):
    subprocess.run(["git", "clone", "-q", str(world / "projects" / "app"),
                    str(world / "projects" / "copy")], check=True, capture_output=True)
    found = git_days.scan_all(_cfg(world), TZ)
    assert found.stores == 3
    assert git_days.day_counts(found.commits, date(2026, 11, 1), date(2026, 11, 5)) == EXPECTED


@pytest.mark.parametrize("remote", [False, True])
def test_detached_worktree_heads_count_even_outside_scan_roots(world, monkeypatch, remote):
    if remote and sys.platform == "win32":
        pytest.skip("the remote script is POSIX sh")
    repo = world / "projects" / "app"
    detached = world / "outside"
    _g(repo, "worktree", "add", "-q", "--detach", str(detached))
    _g(detached, "commit", "-q", "--allow-empty", "-m", "detached linked work",
       when="2026-11-05T10:00:00+00:00")
    _g(repo, "checkout", "-q", "--detach")
    _g(repo, "commit", "-q", "--allow-empty", "-m", "detached main work",
       when="2026-11-05T11:00:00+00:00")
    cfg = _cfg(world)
    if remote:
        monkeypatch.setattr(git_days, "_ssh_argv", lambda host: ["sh", "-s"])
        cfg = _cfg(world, git_days_roots=(),
                   git_days_remotes=(("local", (str(world / "projects"),)),))
    found = git_days.scan_all(cfg, TZ)
    assert (found.stores, found.failed) == (2, 0)
    expected = {**EXPECTED, date(2026, 11, 5): 2}
    assert git_days.day_counts(found.commits, date(2026, 11, 1), date(2026, 11, 5)) == expected


@pytest.mark.parametrize("remote", [False, True])
def test_empty_repos_and_unborn_heads_are_zero_not_partial_reads(world, monkeypatch, remote):
    if remote and sys.platform == "win32":
        pytest.skip("the remote script is POSIX sh")
    empty = world / "projects" / "empty"
    empty.mkdir()
    _g(empty, "init", "-q", "-b", "main")
    _g(world / "projects" / "app", "checkout", "-q", "--orphan", "unborn")
    cfg = _cfg(world)
    if remote:
        monkeypatch.setattr(git_days, "_ssh_argv", lambda host: ["sh", "-s"])
        cfg = _cfg(world, git_days_roots=(), git_days_remotes=(("local", (str(world / "projects"),)),))
    found = git_days.scan_all(cfg, TZ)
    assert (found.stores, found.failed) == (3, 0)
    assert git_days.day_counts(found.commits, date(2026, 11, 1), date(2026, 11, 5)) == EXPECTED


def test_bots_never_count_even_when_a_pattern_matches_them(world):
    cfg = _cfg(world, git_days_shared_authors=((r".*", r".*"),))
    found = git_days.scan_all(cfg, TZ)
    days = git_days.day_counts(found.commits, date(2026, 11, 1), date(2026, 11, 5))
    assert days[date(2026, 11, 3)] == 2         # the shared author now counts, the bot still not
    assert days[date(2026, 11, 4)] == 2         # anyone matches .*: someone@else and the shared author


def test_write_only_changed_and_missing_days():
    days = {date(2026, 11, 1): 0, date(2026, 11, 2): 3, date(2026, 11, 3): 1, date(2026, 11, 4): 0}
    stored = {date(2026, 11, 1): 0, date(2026, 11, 2): 2, date(2026, 11, 4): 0}
    assert git_days.to_write(days, stored) == ([(date(2026, 11, 2), 3), (date(2026, 11, 3), 1)], [])


@pytest.mark.parametrize("stores, failed, prev, refused", [
    (0, 0, None, "no repositories"), (4, 1, None, "could not be read"),
    (4, 0, [9], "fewer object stores"), (5, 0, [9], None), (3, 0, None, None),
    (4, 0, [10, 6], "fewer object stores"), (4, 0, [7, 6], None)])     # the best run, not the last
def test_refusal(stores, failed, prev, refused):
    why = git_days.refusal(git_days.Found(stores=stores, failed=failed), prev)
    assert (why is None) if refused is None else (refused in (why or ""))


def _stored_rows(monkeypatch, stored: dict[date, int]) -> list:
    log: list = []
    monkeypatch.setattr(git_days, "load_stored", lambda dsn: stored)
    monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: log.append(("beat", rows)))
    return log


def test_run_writes_with_one_scanned_at_and_saves_the_store_count(world, monkeypatch, capsys):
    log = _stored_rows(monkeypatch, {date(2026, 11, 1): 0, date(2026, 11, 2): 0})      # 11-02 goes up
    with _fake_psycopg(log):
        report = git_days.run(_cfg(world), dry_run=False, now=NOW, tz=TZ)
    git_days.print_report(report, dry_run=False)
    (sql, rows), = [x for x in log if isinstance(x, tuple) and x[0] != "beat"]
    assert "INSERT INTO agents.git_commit_day (day, commits, scanned_at)" in sql
    assert [r[:2] for r in rows] == [(date(2026, 11, d), n) for d, n in
                                     ((2, 1), (3, 1), (4, 1), (5, 0))]
    assert {r[2] for r in rows} == {NOW}
    wm = load_state()["collectors"]["git_days"]
    assert wm["stores"] == [2] and wm["last_ok"] == NOW.isoformat()
    beat, = [x[1] for x in log if isinstance(x, tuple) and x[0] == "beat"]
    assert beat[0]["collector"] == "git_days" and beat[0]["events"] == 4
    out = capsys.readouterr()
    for secret in (ME, "app", "demo", _g(world / "projects" / "app", "rev-parse", "HEAD")[:7]):
        assert secret not in out.out + out.err


def test_a_shrunken_scan_writes_nothing_and_keeps_the_state(world, monkeypatch):
    (world / "state.json").write_text(json.dumps({"collectors": {"git_days": {"stores": [9]}}}))
    log = _stored_rows(monkeypatch, {})
    with _fake_psycopg(log), pytest.raises(AutError, match="fewer object stores"):
        git_days.run(_cfg(world), dry_run=False, now=NOW, tz=TZ)
    assert not [x for x in log if isinstance(x, tuple) and x[0] != "beat"]
    assert load_state()["collectors"]["git_days"] == {"stores": [9]}


def test_no_identities_is_refused_before_any_scan(world):
    with pytest.raises(AutError, match="identities"):
        git_days.run(_cfg(world, git_days_identities=()), dry_run=True, now=NOW, tz=TZ)


def test_dry_run_prints_year_totals_and_touches_nothing(world, monkeypatch, capsys):
    monkeypatch.setattr(git_days, "load_stored", lambda dsn: pytest.fail("a dry run read the sink"))
    monkeypatch.setattr(push, "send_scan_rows", lambda dsn, rows: pytest.fail("heartbeat"))
    git_days.print_report(git_days.run(_cfg(world, pg_dsn=None), dry_run=True, now=NOW, tz=TZ),
                          dry_run=True)
    out = capsys.readouterr().out
    assert "2026" in out and "3 commits on 3 days" in out and "2 object stores" in out
    assert not (world / "state.json").exists()


class _Proc:
    def __init__(self, rc: int, out: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_remote_is_read_over_batch_ssh_and_a_failed_host_blocks_the_write(world, monkeypatch):
    calls: list = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if argv[-3] == "down":
            return _Proc(255)
        return _Proc(0, f"\x01/srv/x/demo_ui_tool/.git\nabc\t2026-11-05T10:00:00+00:00"
                        f"\t{SHARED}\n\x01/srv/y/.git\nabc\t2026-11-05T10:00:00+00:00\t{ME}\n")

    monkeypatch.setattr(git_days.subprocess, "run", fake_run)
    cfg = _cfg(world, git_days_roots=(), git_days_remotes=(("vm", ("~/p",)), ("down", ("/q",))))
    found = git_days.scan_all(cfg, TZ)
    argv, kw = calls[0]
    assert argv[:2] == ["ssh", "-o"] and "BatchMode=yes" in argv \
        and any(a.startswith("ConnectTimeout=") for a in argv) and argv[-2:] == ["sh", "-s"]
    assert "ClearAllForwardings=yes" in argv
    assert '"$HOME"/' in kw["input"] and "core.fsmonitor=" in kw["input"]
    assert (found.stores, found.failed) == (2, 1)
    assert git_days.day_counts(found.commits, date(2026, 11, 5), date(2026, 11, 5)) == \
        {date(2026, 11, 5): 1}              # one SHA, counted once
    assert "could not be read" in (git_days.refusal(found, None) or "")


@pytest.mark.skipif(sys.platform == "win32", reason="the remote script is POSIX sh")
def test_the_remote_script_reads_the_same_as_the_local_scan(world, monkeypatch):
    monkeypatch.setattr(git_days, "_ssh_argv", lambda host: ["sh", "-s"])
    cfg = _cfg(world, git_days_roots=(),
               git_days_remotes=(("local", (str(world / "projects"),)),))
    found = git_days.scan_all(cfg, TZ)
    assert (found.stores, found.failed) == (2, 0)
    assert git_days.day_counts(found.commits, date(2026, 11, 1), date(2026, 11, 5)) == EXPECTED


@pytest.mark.skipif(sys.platform == "win32", reason="shared-repo scan uses POSIX sh and symlinks")
@pytest.mark.parametrize("remote", [False, True])
def test_shared_reads_are_exact_scoped_hardened_and_offline(world, monkeypatch, remote):
    outside = world / "outside"
    _repo(outside, [(ME, "2026-11-05T10:00:00+00:00")])
    (world / "projects" / "escape").symlink_to(outside, target_is_directory=True)
    marker = world / "executed"
    _g(world / "projects" / "app", "config", "core.fsmonitor", f"touch {marker}")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https:ssh:file")
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    assert subprocess.run(["git", "-C", str(world / "projects" / "app"), "status"],
                          capture_output=True).returncode != 0
    monkeypatch.setattr(git_days, "_ssh_argv", lambda host: ["sh", "-s"])
    cfg = _cfg(world, git_days_roots=() if remote else (str(world / "projects"),),
               git_days_remotes=(("local", (str(world / "projects"),)),) if remote else ())
    found = git_days.scan_all(cfg, TZ)
    assert (found.stores, found.failed) == (2, 0)
    assert git_days.day_counts(found.commits, date(2026, 11, 1), date(2026, 11, 5)) == EXPECTED
    assert not marker.exists()
    argv = git_days._read_argv(str(world / "projects" / "app"), "log")
    assert "safe.directory=" in argv and not any(a.endswith("=*") for a in argv)
    assert git_days.READ_ENV["GIT_ALLOW_PROTOCOL"] == ""
    assert git_days.READ_ENV["GIT_NO_LAZY_FETCH"] == "1"


def test_run_if_due_respects_enabled_and_the_20_hour_gap(world, monkeypatch):
    ran: list = []
    monkeypatch.setattr(git_days, "run", lambda cfg, dry_run: ran.append(1) or
                        {"stores": 1, "failed": 0, "days": {}, "written": 0, "refused": None})
    git_days.run_if_due(_cfg(world, git_days_enabled=False), now=NOW)
    (world / "state.json").write_text(json.dumps(
        {"collectors": {"git_days": {"last_ok": (NOW - timedelta(hours=19)).isoformat()}}}))
    git_days.run_if_due(_cfg(world), now=NOW)
    assert ran == []
    git_days.run_if_due(_cfg(world), now=NOW + timedelta(hours=2))
    assert ran == [1]


def test_a_failing_git_days_never_fails_the_scan(world, monkeypatch, capsys):
    conf = world / "config.toml"
    conf.write_text('machine = "t"\nagents = ["claude_code"]\n[git_days]\nenabled = true\n'
                    f'identities = ["{ME}"]\nroots = ["{(world / "projects").as_posix()}"]\n')
    monkeypatch.setenv("AUT_CONFIG", str(conf))
    monkeypatch.setenv("HOME", str(world))
    monkeypatch.setenv("USERPROFILE", str(world))
    monkeypatch.setattr(scan, "make_sender", lambda cfg: lambda rows: None)

    def boom(cfg, dry_run):
        raise RuntimeError("sink down")

    monkeypatch.setattr(git_days, "run", boom)
    assert cli.main(["scan"]) == 0
    assert "git-days did not run" in capsys.readouterr().err


def test_config_reads_the_git_days_table(tmp_path, monkeypatch):
    conf = tmp_path / "config.toml"
    conf.write_text('[git_days]\nenabled = true\nsince = "2023-01-02"\n'
                    'identities = ["A@B.com"]\nroots = ["/r"]\n'
                    'shared_authors = [{email_regex = "^u@", repo_regex = "^s_"}]\n'
                    'remotes = [{host = "vm1", roots = ["~/p"]}]\n')
    monkeypatch.setenv("AUT_CONFIG", str(conf))
    cfg = load_config()
    assert cfg.git_days_enabled and cfg.git_days_since == date(2023, 1, 2)
    assert cfg.git_days_identities == ("a@b.com",) and cfg.git_days_roots == ("/r",)
    assert cfg.git_days_shared_authors == (("^u@", "^s_"),)
    assert cfg.git_days_remotes == (("vm1", ("~/p",)),)
    conf.write_text("")
    assert not load_config().git_days_enabled and load_config().git_days_since == date(2022, 2, 3)


@pytest.mark.parametrize("body, msg", [
    ('remotes = [{host = "-oProxyCommand=x", roots = ["/p"]}]', "host"),
    ('shared_authors = [{email_regex = "(", repo_regex = "x"}]', "email_regex"),
    ('identities = "me@x"', "identities")])
def test_bad_git_days_config_is_refused_by_name(tmp_path, monkeypatch, body, msg):
    conf = tmp_path / "config.toml"
    conf.write_text(f"[git_days]\n{body}\n")
    monkeypatch.setenv("AUT_CONFIG", str(conf))
    with pytest.raises(AutError, match=msg) as err:
        load_config()
    assert "ProxyCommand" not in str(err.value)


# --- a partial scan, never-lower days, refs, backoff, heartbeat, config --------------------------

def _posix_unreadable_ok() -> bool:
    return sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() != 0


def test_a_missing_root_is_a_failure_and_the_run_writes_nothing(world, monkeypatch):
    cfg = _cfg(world, git_days_roots=(str(world / "projects"), str(world / "gone")))
    found = git_days.scan_all(cfg, TZ)
    assert (found.stores, found.failed) == (2, 1)
    log = _stored_rows(monkeypatch, {})
    with _fake_psycopg(log), pytest.raises(AutError, match="could not be read"):
        git_days.run(cfg, dry_run=False, now=NOW, tz=TZ)
    assert not [x for x in log if isinstance(x, tuple) and x[0] != "beat"]


@pytest.mark.skipif(not _posix_unreadable_ok(), reason="needs POSIX permissions, not as root")
def test_an_unreadable_folder_under_a_root_is_a_failure(world):
    blocked = world / "projects" / "blocked"
    blocked.mkdir()
    blocked.chmod(0)
    try:
        assert git_days.scan_all(_cfg(world), TZ).failed == 1
    finally:
        blocked.chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="the remote script is POSIX sh")
def test_the_remote_script_reports_a_missing_root_and_find_errors(world, monkeypatch):
    monkeypatch.setattr(git_days, "_ssh_argv", lambda host: ["sh", "-s"])
    roots = (str(world / "projects"), str(world / "gone"))
    cfg = _cfg(world, git_days_roots=(), git_days_remotes=(("local", roots),))
    assert (lambda f: (f.stores, f.failed))(git_days.scan_all(cfg, TZ)) == (2, 1)
    if _posix_unreadable_ok():
        blocked = world / "projects" / "blocked"
        blocked.mkdir()
        blocked.chmod(0)
        try:
            cfg = _cfg(world, git_days_roots=(),
                       git_days_remotes=(("local", (str(world / "projects"),)),))
            assert git_days.scan_all(cfg, TZ).failed == 1
        finally:
            blocked.chmod(0o755)


@pytest.mark.skipif(not _posix_unreadable_ok(), reason="needs POSIX permissions, not as root")
@pytest.mark.parametrize("remote", [False, True])
def test_explicit_exclusions_are_literal_and_other_read_errors_still_fail(world, monkeypatch, remote):
    root = world / "projects"
    blocked = root / "foreign[1]"
    blocked.mkdir()
    (blocked / "child").mkdir()
    blocked.chmod(0o744)
    # The bracket in the excluded name must not also exclude a different owner's clone.
    _repo(root / "foreign1", [(ME, "2026-11-05T10:00:00+00:00")])
    monkeypatch.setattr(git_days, "_ssh_argv", lambda host: ["sh", "-s"])
    cfg = _cfg(world, git_days_roots=() if remote else (str(root),),
               git_days_remotes=(("local", (str(root),)),) if remote else (),
               git_days_exclude_dirs=(str(blocked),))
    try:
        found = git_days.scan_all(cfg, TZ)
        assert (found.stores, found.failed) == (3, 0)
        blocked.chmod(0)
        assert git_days.scan_all(cfg, TZ).failed == 0
        assert git_days.scan_all(_cfg(world), TZ).failed > 0
    finally:
        blocked.chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="remote reads use POSIX sh")
@pytest.mark.parametrize("remote", [False, True])
def test_a_configured_root_can_itself_be_excluded(world, monkeypatch, remote):
    root = str((world / "projects" / "app").resolve())
    monkeypatch.setattr(git_days, "_ssh_argv", lambda host: ["sh", "-s"])
    cfg = _cfg(world, git_days_roots=() if remote else (root,),
               git_days_remotes=(("local", (root,)),) if remote else (),
               git_days_exclude_dirs=(root,))
    found = git_days.scan_all(cfg, TZ)
    assert (found.stores, found.failed) == (0, 0)
    assert git_days.refusal(found, None) == "the scan found no repositories"


@pytest.mark.skipif(sys.platform == "win32", reason="the remote script is POSIX sh")
def test_a_remote_git_older_than_2_31_is_refused(world, monkeypatch, capsys):
    shim = world / "oldgit"
    shim.mkdir()
    (shim / "git").write_text("#!/bin/sh\necho 'git version 2.25.1'\n")
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(git_days, "_ssh_argv", lambda host: ["sh", "-s"])
    cfg = _cfg(world, git_days_roots=(), git_days_remotes=(("local", (str(world / "projects"),)),))
    found = git_days.scan_all(cfg, TZ)
    assert (found.stores, found.failed) == (0, 1)
    assert "older than 2.31" in capsys.readouterr().err


def test_stash_and_notes_are_not_commits_but_tags_are(world):
    app = world / "projects" / "app"
    when = "2026-11-04T12:00:00+00:00"
    (app / "x.txt").write_text("x")
    _g(app, "stash", "-u", "-q", when=when)
    _g(app, "notes", "add", "-m", "n", "HEAD", when=when)
    _g(app, "checkout", "-q", "-b", "gone")
    _g(app, "commit", "-q", "--allow-empty", "-m", "tagged", when="2026-11-05T12:00:00+00:00")
    _g(app, "tag", "keep")
    _g(app, "checkout", "-q", "main")
    _g(app, "branch", "-q", "-D", "gone")
    found = git_days.scan_all(_cfg(world), TZ)
    assert git_days.day_counts(found.commits, date(2026, 11, 1), date(2026, 11, 5)) == \
        {**EXPECTED, date(2026, 11, 5): 1}


def test_a_stored_day_never_goes_down_unless_allowed():
    days = {date(2026, 11, 1): 1, date(2026, 11, 2): 3, date(2026, 11, 3): 0, date(2026, 11, 4): 2}
    stored = {date(2026, 11, 1): 4, date(2026, 11, 2): 2, date(2026, 11, 3): 2}
    assert git_days.to_write(days, stored) == ([(date(2026, 11, 2), 3), (date(2026, 11, 4), 2)],
                                               [3, 2])
    assert git_days.to_write(days, stored, allow_lower=True) == (sorted(days.items()), [])


def test_run_keeps_lower_days_and_reports_only_their_count(world, monkeypatch, capsys):
    log = _stored_rows(monkeypatch, {date(2026, 11, 2): 5, date(2026, 11, 3): 1})
    with _fake_psycopg(log):
        report = git_days.run(_cfg(world), dry_run=False, now=NOW, tz=TZ)
    git_days.print_report(report, dry_run=False)
    (_, rows), = [x for x in log if isinstance(x, tuple) and x[0] != "beat"]
    assert [r[:2] for r in rows] == [(date(2026, 11, 1), 0), (date(2026, 11, 4), 1),
                                     (date(2026, 11, 5), 0)]
    out = capsys.readouterr().out
    assert "1 day(s) would go down (4 commits in all)" in out and "--allow-lower" in out
    log = _stored_rows(monkeypatch, {date(2026, 11, 2): 5})
    with _fake_psycopg(log):
        git_days.run(_cfg(world), dry_run=False, allow_lower=True, now=NOW, tz=TZ)
    (_, rows), = [x for x in log if isinstance(x, tuple) and x[0] != "beat"]
    assert (date(2026, 11, 2), 1) in [r[:2] for r in rows]


def test_the_store_history_keeps_the_last_five_good_runs(world, monkeypatch):
    (world / "state.json").write_text(json.dumps(
        {"collectors": {"git_days": {"stores": [3, 3, 3, 3, 3]}}}))
    with _fake_psycopg(_stored_rows(monkeypatch, {})):
        git_days.run(_cfg(world), dry_run=False, now=NOW, tz=TZ)
    assert load_state()["collectors"]["git_days"]["stores"] == [3, 3, 3, 3, 2]
    with _fake_psycopg(_stored_rows(monkeypatch, {})):
        git_days.run(_cfg(world), dry_run=False, allow_lower=True, now=NOW, tz=TZ)
    assert load_state()["collectors"]["git_days"]["stores"] == [2]   # a deliberate reset


def test_a_failed_run_from_scan_backs_off_six_hours(world, monkeypatch, capsys):
    ran: list = []

    def boom(cfg, dry_run):
        ran.append(1)
        raise AutError("down")

    monkeypatch.setattr(git_days, "run", boom)
    git_days.run_if_due(_cfg(world), now=NOW)
    assert load_state()["collectors"]["git_days"]["last_attempt"] == NOW.isoformat()
    git_days.run_if_due(_cfg(world), now=NOW + timedelta(hours=5))
    assert ran == [1]
    git_days.run_if_due(_cfg(world), now=NOW + timedelta(hours=7))
    assert ran == [1, 1]
    assert "git-days did not run" in capsys.readouterr().err


def test_an_early_failure_still_sends_the_heartbeat(world, monkeypatch, capsys):
    log = _stored_rows(monkeypatch, {})
    with pytest.raises(AutError, match="identities"):
        git_days.run(_cfg(world, git_days_identities=()), dry_run=False, now=NOW, tz=TZ)
    beat, = [x[1] for x in log if isinstance(x, tuple) and x[0] == "beat"]
    assert beat[0]["collector"] == "git_days" and beat[0]["status"] == "error"
    with pytest.raises(AutError, match="pg_dsn"):
        git_days.run(_cfg(world, pg_dsn=None), dry_run=False, now=NOW, tz=TZ)
    assert "git_days heartbeat not sent" in capsys.readouterr().err   # it tried; no DSN to send by


def test_a_dst_change_day_in_amsterdam(tmp_path, monkeypatch):
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        ams = zoneinfo.ZoneInfo("Europe/Amsterdam")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone database here")
    p = tmp_path / "projects"
    _repo(p / "app", [(ME, "2026-03-28T23:30:00+00:00"),      # 00:30 CET on 03-29
                      (ME, "2026-03-29T22:30:00+00:00"),      # 00:30 CEST on 03-30 (+1 would say 03-29)
                      (ME, "2026-10-24T22:30:00+00:00"),      # 00:30 CEST on 10-25
                      (ME, "2026-10-25T23:30:00+00:00")])     # 00:30 CET on 10-26
    found = git_days.scan_all(_cfg(tmp_path), ams)
    assert sorted(found.commits.values()) == [date(2026, 3, 29), date(2026, 3, 30),
                                              date(2026, 10, 25), date(2026, 10, 26)]


@pytest.mark.parametrize("value, want", [
    ("2023-01-02", date(2023, 1, 2)), ("2023-01-02T10:00:00", date(2023, 1, 2)),
    ("2023-01-02T10:00:00+02:00", date(2023, 1, 2)), ('"2023-01-02"', date(2023, 1, 2))])
def test_since_takes_a_toml_date_datetime_or_string(tmp_path, monkeypatch, value, want):
    conf = tmp_path / "config.toml"
    conf.write_text(f"[git_days]\nsince = {value}\n")
    monkeypatch.setenv("AUT_CONFIG", str(conf))
    assert load_config().git_days_since == want


@pytest.mark.parametrize("body, msg", [
    ("since = 5", "since"), ('since = "yesterday"', "since"),
    ('remotes = [{host = "vm1", roots = []}]', r"remotes\[0\]\.roots")])
def test_bad_since_or_empty_remote_roots_is_refused(tmp_path, monkeypatch, body, msg):
    conf = tmp_path / "config.toml"
    conf.write_text(f"[git_days]\n{body}\n")
    monkeypatch.setenv("AUT_CONFIG", str(conf))
    with pytest.raises(AutError, match=msg):
        load_config()
