"""Hermetic tests for repo keys: origin URLs normalized to one identity, salted per machine."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from agent_usage_tracker import scan
from agent_usage_tracker.config import AutError, Config
from agent_usage_tracker.repo_keys import RepoKeys
from agent_usage_tracker.repo_keys import repo_identity
from test_activity import SALT, _key


def test_repo_key_normalisation() -> None:
    k = repo_identity
    assert k("https://user:secret@Example.com/Owner/Repo.git") == "example.com/owner/repo"
    assert k("git@example.com:Owner/Repo.git") == "example.com/owner/repo"
    assert k("ssh://git@example.com:22/Owner/Repo/") == "example.com/owner/repo"
    keys = RepoKeys(SALT)
    assert keys.of_cwd("/no/such/dir/anywhere") is None
    with tempfile.TemporaryDirectory() as d:
        plain = Path(d) / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        assert keys.of_cwd(str(plain)) is None                     # repo without a remote
    assert keys.of("example.com/owner/repo") == _key("example.com/owner/repo")
    assert RepoKeys("other").of("example.com/owner/repo") != _key("example.com/owner/repo")


def test_repo_key_salt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("AUT_STATE", str(Path(d) / "st" / "state.json"))
        k1 = RepoKeys(None).of("example.com/owner/repo")
        salt = Path(d) / "st" / "repo_key.salt"
        assert salt.exists(), salt
        if os.name != "nt":                                 # Windows has no POSIX modes
            assert (salt.stat().st_mode & 0o777) == 0o600, oct(salt.stat().st_mode)
        assert RepoKeys(None).of("example.com/owner/repo") == k1      # reused
        assert k1 != hashlib.sha256(b"example.com/owner/repo").hexdigest()
        assert k1 != _key("example.com/owner/repo")
        cfgf = Path(d) / "config.toml"
        cfgf.write_text('repo_key_salt = "env:AUT_TEST_SALT"\n')
        cfgf.chmod(0o600)
        monkeypatch.setenv("AUT_CONFIG", str(cfgf))
        monkeypatch.setenv("AUT_TEST_SALT", "from-env")
        from agent_usage_tracker.config import load_config
        assert load_config().repo_key_salt == "from-env"
        with tempfile.TemporaryDirectory() as d2:
            monkeypatch.setenv("AUT_STATE", str(Path(d2) / "state.json"))
            (Path(d2) / "repo_key.salt").write_text("")        # a broken salt: NULL, not unsalted
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                assert RepoKeys(None).of("example.com/owner/repo") is None
            assert "too short" in err.getvalue()


@pytest.mark.parametrize("corrupt", [b"\xff\xfe" * 40, b"short", b""])
def test_a_corrupt_salt_warns_and_stops_a_scan(tmp_path, monkeypatch, capsys, corrupt) -> None:
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    salt = tmp_path / "repo_key.salt"
    salt.write_bytes(corrupt)
    assert RepoKeys(None).of("github.com/o/r") is None
    assert "repo_key stays NULL" in capsys.readouterr().err
    monkeypatch.setattr(scan, "make_sender", lambda cfg: [].extend)
    cfg = Config(machine="m", agents=("pi",), webhook_url=None, webhook_secret=None, pg_dsn="pg://x",
                 home=tmp_path)
    for run in (lambda: scan.run_scan(cfg, ["pi"], dry_run=False),
                lambda: scan.run_history_backfill(cfg, ["pi"], dry_run=False)):
        with pytest.raises(AutError, match="delete it to start new keys") as exc:
            run()
        assert str(salt) in str(exc.value) and "no longer match" in str(exc.value)
    assert salt.read_bytes() == corrupt                 # never regenerated
    assert not (tmp_path / "state.json").exists()
    scan.run_scan(cfg, ["pi"], dry_run=True)            # a dry run warns and goes on


def test_an_unreadable_salt_warns_and_stops_a_scan(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    salt = tmp_path / "repo_key.salt"
    salt.write_text("s" * 40)

    def refuse(self, *a, **k):
        raise PermissionError(13, "denied", str(self))
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)
    assert RepoKeys(None).of("github.com/o/r") is None
    assert "repo_key stays NULL" in capsys.readouterr().err
    monkeypatch.undo()
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(scan, "make_sender", lambda cfg: [].extend)
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: refuse(self) if self == salt else real(self))
    cfg = Config(machine="m", agents=("pi",), webhook_url=None, webhook_secret=None, pg_dsn=None,
                 home=tmp_path)
    with pytest.raises(AutError, match="repo_key"):
        scan.run_scan(cfg, ["pi"], dry_run=False)
    assert not (tmp_path / "state.json").exists()


class _KeysSink:
    """connect() stand-in: answers the 'does this machine already have repo keys' read."""
    def __init__(self, has_keys: bool):
        self.has_keys, self.params = has_keys, []

    def __call__(self, dsn, timeout=15):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        assert "repo_key" in sql and "machine" in sql
        self.params.append(params)
        return self

    def fetchone(self):
        return (1,) if self.has_keys else None


def test_a_missing_salt_with_keys_in_the_sink_stops_the_scan(tmp_path, monkeypatch) -> None:
    from agent_usage_tracker import repo_keys
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(scan, "make_sender", lambda cfg: [].extend)
    sink = _KeysSink(has_keys=True)
    monkeypatch.setattr(repo_keys, "connect", sink)
    cfg = Config(machine="laptop", agents=("pi",), webhook_url=None, webhook_secret=None,
                 pg_dsn="pg://x", home=tmp_path)
    for run in (lambda: scan.run_scan(cfg, ["pi"], dry_run=False),
                lambda: scan.run_history_backfill(cfg, ["pi"], dry_run=False)):
        with pytest.raises(AutError, match="docs/new-laptop.md") as exc:
            run()
        assert "repo_key.salt" in str(exc.value) and "repo_key_salt" in str(exc.value)
    assert sink.params == [("laptop",), ("laptop",)]        # this machine's rows only
    assert not (tmp_path / "repo_key.salt").exists()        # no new salt minted
    assert not (tmp_path / "state.json").exists()


def test_a_missing_salt_on_a_new_sink_or_a_configured_salt_goes_on(tmp_path, monkeypatch) -> None:
    from agent_usage_tracker import repo_keys
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    sink = _KeysSink(has_keys=False)
    monkeypatch.setattr(repo_keys, "connect", sink)
    repo_keys.require_readable_salt(None, "pg://x", "laptop")       # a first install: fine
    repo_keys.require_readable_salt(None, None, "laptop")           # webhook only: cannot tell
    sink.has_keys = True
    repo_keys.require_readable_salt("from-config-" * 3, "pg://x", "laptop")  # config salt: no read
    assert sink.params == [("laptop",)]


def test_every_command_that_can_make_a_salt_asks_the_sink_first(tmp_path, monkeypatch) -> None:
    """A dry run, a label run and the outcome commands create the salt too (RepoKeys mints it on
    first use), so each one refuses before it, not only a sending scan."""
    from agent_usage_tracker import outcomes, repo_keys
    from agent_usage_tracker.label.run import run_label
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(repo_keys, "connect", _KeysSink(has_keys=True))
    cfg = Config(machine="laptop", agents=("pi",), webhook_url=None, webhook_secret=None,
                 pg_dsn="pg://x", home=tmp_path)
    for run in (lambda: scan.run_scan(cfg, ["pi"], dry_run=True),
                lambda: scan.run_history_backfill(cfg, ["pi"], dry_run=True),
                lambda: run_label(cfg, [], dry_run=True),
                lambda: outcomes.run_outcomes(cfg, refresh=False, dry_run=False),
                lambda: outcomes.build_manual_outcome(cfg, "shipped", str(tmp_path), "s1")):
        with pytest.raises(AutError, match="docs/new-laptop.md"):
            run()
    assert not (tmp_path / "repo_key.salt").exists()


def test_a_new_salt_with_no_sink_to_ask_is_a_clear_warning(tmp_path, monkeypatch, capsys) -> None:
    from agent_usage_tracker import repo_keys
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    repo_keys.require_readable_salt(None, None, "laptop")           # webhook only: cannot tell
    err = capsys.readouterr().err
    assert "new repo_key.salt" in err and "docs/new-laptop.md" in err
    (tmp_path / "repo_key.salt").write_text("a" * 64)
    repo_keys.require_readable_salt(None, None, "laptop")           # the salt is there: quiet
    assert capsys.readouterr().err == ""
