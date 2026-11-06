"""Hermetic tests for `aut backup`: restic is never run, a fake records each call."""

from __future__ import annotations

import json
import plistlib
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_usage_tracker import backup, cli
from agent_usage_tracker.config import AutError, Config, load_config

REPO = Path(__file__).resolve().parents[2]
PW_CMD = "security find-generic-password -s aut-backup -w"


class FakeRestic:
    """Answers `cat config`, `backup` and `check` with the given exit codes; records argv + env."""

    def __init__(self, **rc):
        self.rc = {"cat": 0, "backup": 0, "check": 0, "forget": 0, **rc}
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, env, timeout=None):
        self.calls.append((list(args), env))
        rc = self.rc[args[0]]
        if rc == "timeout":
            raise subprocess.TimeoutExpired(["restic", *args], timeout)
        out = "Files: 3 new\nAdded to the repository: 1.2 MiB\nsnapshot 1a2b3c4d saved\n"
        return subprocess.CompletedProcess(["restic", *args], rc, out if rc in (0, 3) else "",
                                           "" if rc == 0 else "Fatal: something")

    def verbs(self):
        return [a[0] for a, _e in self.calls]


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for p in (".claude/projects", ".codex/sessions", ".codex/archived_sessions", ".cursor/chats",
              "Library/Application Support/activitywatch"):
        (home / p).mkdir(parents=True)
    (home / ".codex" / "history.jsonl").write_text("{}\n")
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state" / "state.json"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "conf" / "config.toml"))
    (tmp_path / "share" / "aut" / "label" / "models").mkdir(parents=True)
    (tmp_path / "conf").mkdir()
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(backup, "_battery", lambda: None)
    fake = FakeRestic()
    monkeypatch.setattr(backup, "_restic", fake)
    cfg = Config(machine="laptop", agents=(), webhook_url=None, webhook_secret=None, pg_dsn=None,
                 home=home, backup_repository="sftp:backup-host:/srv/restic/aut",
                 backup_password_command=PW_CMD)
    return cfg, fake, tmp_path


def _status(tmp_path):
    return json.loads((tmp_path / "state" / "backup.json").read_text())


def test_backup_runs_restic_with_the_default_paths_and_records_last_ok(world, capsys):
    cfg, fake, tmp = world
    assert backup.run_backup(cfg) == 0
    assert fake.verbs() == ["cat", "backup", "forget"]   # reachable, the backup, the retention
    args, env = fake.calls[1]
    assert env["RESTIC_REPOSITORY"] == cfg.backup_repository
    assert env["RESTIC_PASSWORD_COMMAND"] == PW_CMD and "RESTIC_PASSWORD" not in env
    assert "--exclude-caches" in args and args[args.index("--host") + 1] == "laptop"
    home = cfg.home
    for p in (".claude/projects", ".codex/sessions", ".codex/history.jsonl",
              ".codex/archived_sessions", ".cursor/chats", "Library/Application Support/activitywatch"):
        assert str(home / p) in args
    for p in ("conf", "share/aut", "state"):              # config, label state + gold, the salt
        assert str(tmp / p) in args
    assert args[args.index("--exclude") + 1] == str(tmp / "share" / "aut" / "label" / "models")
    assert datetime.fromisoformat(_status(tmp)["last_ok"]) > datetime.now(timezone.utc) - \
        timedelta(minutes=1)
    out = capsys.readouterr()
    assert "snapshot 1a2b3c4d saved" in out.out
    assert PW_CMD not in out.out + out.err                # the password command is never printed


def test_missing_paths_are_left_out_and_config_paths_replace_the_defaults(world):
    cfg, fake, tmp = world
    (cfg.home / ".cursor" / "chats").rmdir()
    backup.run_backup(cfg)
    assert str(cfg.home / ".cursor" / "chats") not in fake.calls[1][0]
    only = tmp / "only"
    only.mkdir()
    fake.calls.clear()
    backup.run_backup(replace(cfg, backup_paths=(str(only),), backup_excludes=("*.tmp",)))
    args = fake.calls[1][0]
    assert args[-1] == str(only) and str(cfg.home / ".claude" / "projects") not in args
    assert "*.tmp" in args and str(tmp / "share" / "aut" / "label" / "models") in args  # added to


@pytest.mark.parametrize("battery, skipped", [((True, 15), True), ((True, 20), False),
                                              ((False, 5), False), (None, False)])
def test_low_battery_skips_with_exit_0(world, monkeypatch, battery, skipped):
    cfg, fake, tmp = world
    monkeypatch.setattr(backup, "_battery", lambda: battery)
    assert backup.run_backup(cfg) == 0
    assert (fake.verbs() == []) is skipped
    assert ("last_ok" in _status(tmp)) is not skipped


@pytest.mark.parametrize("rc", [1, "timeout"])
def test_an_unreachable_repo_skips_with_exit_0_and_keeps_last_ok(world, rc, capsys):
    cfg, fake, tmp = world
    backup.run_backup(cfg)
    ok = _status(tmp)["last_ok"]
    fake.rc["cat"] = rc
    fake.calls.clear()
    assert backup.run_backup(cfg) == 0
    assert fake.verbs() == ["cat"] and _status(tmp)["last_ok"] == ok
    assert "unreachable" in _status(tmp)["last_result"]
    assert "unreachable" in capsys.readouterr().out


@pytest.mark.parametrize("rc, says", [(12, "password"), (10, "restic init")])
def test_wrong_password_or_no_repo_is_an_error_not_a_skip(world, rc, says):
    cfg, fake, _tmp = world
    fake.rc["cat"] = rc
    with pytest.raises(AutError, match=says):
        backup.run_backup(cfg)


def test_a_failed_backup_raises_and_a_partial_one_counts(world):
    cfg, fake, tmp = world
    fake.rc["backup"] = 1
    with pytest.raises(AutError, match="restic backup failed"):
        backup.run_backup(cfg)
    assert "last_ok" not in _status(tmp)
    fake.rc["backup"] = 3                                 # a file vanished mid-run: snapshot saved
    assert backup.run_backup(cfg) == 0 and "last_ok" in _status(tmp)


def test_an_overlapping_run_is_skipped(world):
    cfg, fake, tmp = world
    fcntl = pytest.importorskip("fcntl")                 # no flock on Windows
    with open(tmp / "state" / "backup.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert backup.run_backup(cfg) == 0
    assert fake.verbs() == []
    assert backup.run_backup(cfg) == 0 and fake.verbs()[:2] == ["cat", "backup"]   # released


def test_check_runs_restic_check_and_sends_nothing(world, capsys):
    cfg, fake, tmp = world
    assert backup.run_backup(cfg, check=True) == 0
    assert fake.verbs() == ["cat", "check"]
    assert "last good backup: never" in capsys.readouterr().out
    fake.rc["check"] = 1
    with pytest.raises(AutError, match="restic check"):
        backup.run_backup(cfg, check=True)


def test_needs_a_repository_and_a_password_command(world):
    cfg, fake, _tmp = world
    for bad in (replace(cfg, backup_repository=None), replace(cfg, backup_password_command=None)):
        with pytest.raises(AutError, match=r"\[backup\]"):
            backup.run_backup(bad)
    assert fake.calls == []


def test_status_shows_the_age_of_the_last_good_backup(world, capsys):
    cfg, _fake, tmp = world
    assert backup.describe() == "never ran"
    backup.run_backup(cfg)
    assert backup.describe().startswith("last good 0 h ago")
    st = _status(tmp)
    st["last_ok"] = (datetime.now(timezone.utc) - timedelta(days=3, hours=2)).isoformat()
    (tmp / "state" / "backup.json").write_text(json.dumps(st))
    assert backup.describe().startswith("last good 3 d ago")


def test_config_and_cli(world, monkeypatch, capsys):
    _cfg, fake, tmp = world
    (tmp / "conf" / "config.toml").write_text(
        'machine = "laptop"\n[backup]\nrepository = "env:AUT_TEST_REPO"\n'
        f'password_command = "{PW_CMD}"\npaths = ["~/a"]\nexcludes = ["*.log"]\n')
    (tmp / "conf" / "config.toml").chmod(0o600)
    monkeypatch.setenv("AUT_TEST_REPO", "rest:https://example.invalid/aut")
    cfg = load_config()
    assert cfg.backup_repository == "rest:https://example.invalid/aut"
    assert cfg.backup_password_command == PW_CMD
    assert cfg.backup_paths == ("~/a",) and cfg.backup_excludes == ("*.log",)
    monkeypatch.setattr(backup, "_restic", fake)
    assert cli.main(["backup", "--check"]) == 0 and fake.verbs() == ["cat", "check"]
    (tmp / "conf" / "config.toml").write_text('[backup]\npaths = "not a list"\n')
    with pytest.raises(AutError, match="backup"):
        load_config()
    capsys.readouterr()
    (tmp / "conf" / "config.toml").write_text(f'machine = "laptop"\nhome = "{tmp / "home"}"\n')
    assert cli.main(["--agents", "pi", "status"]) == 0
    assert "backup        : never ran" in capsys.readouterr().out


def test_launchd_templates_are_not_installed_and_catch_up_at_once():
    scan = plistlib.loads((REPO / "packaging" / "launchd" / "dev.aut.scan.plist").read_bytes())
    assert scan["RunAtLoad"] is True                     # at login
    assert scan["StartCalendarInterval"]                 # a missed calendar slot fires on wake
    bk = plistlib.loads((REPO / "packaging" / "launchd" / "dev.aut.backup.plist").read_bytes())
    assert bk["ProgramArguments"][-1] == "backup" and bk["RunAtLoad"] is True
    assert set(bk["StartCalendarInterval"]) == {"Hour", "Minute"}        # daily
    for p in (scan, bk):
        assert all("CHANGEME" in a or not a.startswith("/Users/") for a in p["ProgramArguments"])


def test_a_good_backup_forgets_old_snapshots_and_prunes_once_a_week(world):
    cfg, fake, tmp = world
    backup.run_backup(cfg)
    forget = fake.calls[2][0]
    assert forget[:5] == ["forget", "--tag", "aut", "--host", "laptop"]     # this machine only
    assert " ".join(forget[5:]) == "--keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune"
    pruned = _status(tmp)["last_prune"]
    fake.calls.clear()
    backup.run_backup(cfg)                                # the next day: forget, no prune
    assert fake.calls[2][0][:5] == forget[:5] and "--prune" not in fake.calls[2][0]
    assert _status(tmp)["last_prune"] == pruned
    st = _status(tmp)
    st["last_prune"] = (datetime.now(timezone.utc) - timedelta(days=7, minutes=1)).isoformat()
    (tmp / "state" / "backup.json").write_text(json.dumps(st))
    fake.calls.clear()
    backup.run_backup(cfg)                                # a week on: prune again
    assert "--prune" in fake.calls[2][0] and _status(tmp)["last_prune"] != st["last_prune"]


def test_a_failed_forget_keeps_the_good_backup_and_raises(world):
    cfg, fake, tmp = world
    fake.rc["forget"] = 1
    with pytest.raises(AutError, match="restic forget failed"):
        backup.run_backup(cfg)
    st = _status(tmp)
    assert "last_ok" in st and "last_prune" not in st and "forget failed" in st["last_result"]
    fake.rc.update(backup=1, forget=0)
    fake.calls.clear()
    with pytest.raises(AutError):
        backup.run_backup(cfg)
    assert "forget" not in fake.verbs()                   # a failed backup forgets nothing


def test_no_backup_table_means_no_warning_on_any_command(world, capsys):
    _cfg, _fake, tmp = world
    (tmp / "conf" / "config.toml").write_text('machine = "laptop"\n')
    cfg = load_config()
    assert cfg.backup_repository is None and cfg.backup_password_command is None
    assert capsys.readouterr().err == ""


def test_the_example_config_and_runbook_name_a_literal_repository():
    """launchd starts jobs with a bare environment, so an env: repository is never set there."""
    for doc in ("config.example.toml", "docs/new-laptop.md"):
        text = (REPO / doc).read_text()
        assert 'repository = "sftp:' in text and 'repository = "env:' not in text
        assert "launchd" in text
