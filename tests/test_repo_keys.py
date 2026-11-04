"""Hermetic tests for repo keys: origin URLs normalized to one identity, salted per machine.

Run: `python tests/test_repo_keys.py`  (or `pytest`).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.repo_keys import RepoKeys  # noqa: E402
from agent_usage_tracker.repo_keys import repo_identity  # noqa: E402
from test_activity import SALT, _key  # noqa: E402


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
    print("ok repo_key_normalisation")


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
    print("ok repo_key_salt")


def test_a_binary_salt_file_means_null_keys_not_a_crash(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state.json"))
    (tmp_path / "repo_key.salt").write_bytes(b"\xff\xfe" * 40)
    assert RepoKeys(None).of("github.com/o/r") is None
    assert "repo_key stays NULL" in capsys.readouterr().err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
