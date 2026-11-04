"""Repo identity and its salted key: what a row stores instead of a repo, branch or ticket name."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import subprocess
import sys
from urllib.parse import urlparse

from .git_safe import git_argv, git_env
from .state import state_path


def repo_identity(url: str) -> str:
    """host/owner/repo, lowercased, with credentials, port and a .git suffix dropped, so the https
    and scp-style remotes of one repo (and a PR's host + repository) give the same key."""
    m = re.match(r"^[\w.-]+@([^:/]+):(.+)$", url)       # git@host:owner/repo
    if m:
        host, path = m.groups()
    else:
        p = urlparse(url)                 # raises ValueError on a malformed URL
        host, path = p.hostname or "", p.path
    return f"{host}/{path.strip('/').removesuffix('.git')}".lower()


def _load_salt(configured: str | None) -> bytes:
    """The config's repo_key_salt, else a random salt kept next to the state file (created 0600 on
    first use). b"" when neither can be had: repo keys are then NULL, never an unsalted hash."""
    if configured:
        return configured.encode("utf-8")
    p = state_path().parent / "repo_key.salt"
    try:
        if not p.exists():
            # Written in full to a temp file, then linked into place: a crash or a second process
            # never sees a half-written salt, and the first link wins a race.
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
            try:
                os.link(tmp, p)
            except FileExistsError:
                pass
            finally:
                tmp.unlink(missing_ok=True)
        salt = p.read_text().strip()
        if os.name != "nt" and p.stat().st_mode & 0o077:     # Windows has no POSIX modes
            print(f"aut: warning: {p} is group/other-readable; run: chmod 600 {p}", file=sys.stderr)
    except OSError:
        return b""
    if len(salt) < 32:
        print(f"aut: warning: {p} is too short; repo_key stays NULL until it is fixed",
              file=sys.stderr)
        return b""
    return salt.encode("utf-8")


class RepoKeys:
    """repo_key = HMAC-SHA256(salt, host/owner/repo), so a list of candidate repo names cannot be
    tested against stored keys without the salt. Cached per cwd for one collect() pass."""

    def __init__(self, salt: str | None) -> None:
        self._configured, self._salt, self._cwd = salt, None, {}

    def of(self, identity: str) -> str | None:
        if self._salt is None:
            self._salt = _load_salt(self._configured)
        if not self._salt:
            return None
        return hmac.new(self._salt, identity.encode("utf-8"), hashlib.sha256).hexdigest()

    def of_cwd(self, cwd: object) -> str | None:
        """Key of the origin remote for a working dir; None when the directory or the remote is
        gone. The URL itself is never stored or logged."""
        if not isinstance(cwd, str):
            return None
        if cwd not in self._cwd:
            self._cwd[cwd] = None
            if os.path.isdir(cwd):
                try:
                    r = subprocess.run(git_argv(cwd, "remote", "get-url", "origin"),
                                       capture_output=True, timeout=2, env=git_env())
                    url = r.stdout.decode("utf-8", "replace").strip()
                    if r.returncode == 0 and url:
                        self._cwd[cwd] = self.of(repo_identity(url))
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
        return self._cwd[cwd]
