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


def test_a_binary_salt_file_means_null_keys_not_a_crash(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    (tmp_path / "repo_key.salt").write_bytes(b"\xff\xfe" * 40)
    assert RepoKeys(None).of("github.com/o/r") is None
    assert "repo_key stays NULL" in capsys.readouterr().err


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
