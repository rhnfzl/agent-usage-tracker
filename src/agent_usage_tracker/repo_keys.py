"""Repo identity and its salted key: what a row stores instead of a repo, branch or ticket name."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import subprocess  # nosec B404 # one git_argv call below, no shell
import sys
from urllib.parse import urlparse

from .config import AutError
from .git_safe import git_argv, git_env
from .sink import connect
from .state import state_path, warn_if_group_readable


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


def _salt_text(raw: bytes) -> str:
    """The salt a salt file holds; "" when it is not text or too short to be one."""
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""
    return text if len(text) >= 32 else ""


def require_readable_salt(configured: str | None, dsn: str | None = None,
                          machine: str | None = None) -> None:
    """Raises before a run that sends rows, when the salt file is there but cannot be read or is
    corrupt: NULL keys are permanent once the watermark moves on and the sink's ON CONFLICT DO
    NOTHING applies. A corrupt file is never replaced here: a new salt changes every key. Raises
    too when the file is missing but the sink already holds this machine's keys (a new laptop
    without the old salt): a new salt would split every repo's old and new rows for good."""
    if configured:
        return
    p = state_path().parent / "repo_key.salt"
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        refuse_new_salt(configured, dsn, machine)
        return
    except OSError as exc:
        raise AutError(f"aut: could not read {p} ({exc}); fix its owner or mode, or every repo_key "
                       "of this run would be NULL for good") from exc
    if not _salt_text(raw):
        raise AutError(f"aut: {p} is too short or not text, so every repo_key of this run would be "
                       "NULL for good; restore it, or delete it to start new keys (old rows will "
                       "no longer match new ones)")


def refuse_new_salt(configured: str | None, dsn: str | None, machine: str | None) -> None:
    """Called before any command that can create repo_key.salt (RepoKeys makes it on first use: a
    scan, a dry run, a label run, outcomes). Raises when there is no salt yet but the sink already
    holds this machine's repo keys. With no sink to ask (webhook only), it warns instead."""
    p = state_path().parent / "repo_key.salt"
    if configured or p.exists():
        return
    if not dsn:
        print(f"aut: warning: no {p} and no pg_dsn to ask the sink, so a new repo_key.salt is "
              "made. If this machine sent rows before (a new laptop), stop and restore the old "
              "salt first (docs/new-laptop.md).", file=sys.stderr)
        return
    if _sink_has_keys(dsn, machine or ""):
        raise AutError(
            f"aut: {p} is missing, but the sink already holds repo keys from machine "
            f"'{machine}'. A new salt would never match them. Copy the old repo_key.salt "
            "there, or set repo_key_salt in config.toml to its contents, then run again "
            "(docs/new-laptop.md). Nothing was sent.")


def _sink_has_keys(dsn: str, machine: str) -> bool:
    """Whether any activity row of this machine carries a repo_key. A read error raises: going on
    would mint a new salt before anyone could tell."""
    with connect(dsn) as conn:
        return conn.execute("SELECT 1 FROM agents.activity_event WHERE machine = %s "
                            "AND attrs->>'repo_key' IS NOT NULL LIMIT 1", (machine,)).fetchone() \
            is not None


def _load_salt(configured: str | None, create: bool = True) -> bytes:
    """The config's repo_key_salt, else a random salt kept next to the state file (created 0600 on
    first use, unless not `create`). b"" when neither can be had: repo keys are then NULL, never an
    unsalted hash."""
    if configured:
        return configured.encode("utf-8")
    p = state_path().parent / "repo_key.salt"
    try:
        if not p.exists():
            if not create:
                return b""
            # Written in full to a temp file, then linked into place: a crash or a second process
            # never sees a half-written salt, and the first link wins a race.
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
            try:
                os.link(tmp, p)
            except FileExistsError:     # another process linked first: its salt wins
                pass
            finally:
                tmp.unlink(missing_ok=True)
        salt = _salt_text(p.read_bytes())
        warn_if_group_readable(p)
    except OSError as exc:
        print(f"aut: warning: could not read or create {p} ({exc}); repo_key stays NULL until it "
              "is fixed", file=sys.stderr)
        return b""
    if not salt:
        print(f"aut: warning: {p} is too short or not text; repo_key stays NULL until it is fixed",
              file=sys.stderr)
        return b""
    return salt.encode("utf-8")


class RepoKeys:
    """repo_key = HMAC-SHA256(salt, host/owner/repo), so a list of candidate repo names cannot be
    tested against stored keys without the salt. Cached per cwd for one collect() pass. With
    create False a missing salt file stays missing and the keys are NULL (a dry run)."""

    def __init__(self, salt: str | None, *, create: bool = True) -> None:
        self._configured, self._create = salt, create
        self._salt: bytes | None = None
        self._cwd: dict[object, str | None] = {}

    def of(self, identity: str) -> str | None:
        if self._salt is None:
            self._salt = _load_salt(self._configured, self._create)
        if not self._salt:
            return None
        return hmac.new(self._salt, identity.encode("utf-8"), hashlib.sha256).hexdigest()

    def of_cwd(self, cwd: object) -> str | None:
        """Key of the origin remote for a working dir; None when the directory or the remote is
        gone. The URL itself is never stored or logged."""
        if not isinstance(cwd, str):
            return None
        if cwd not in self._cwd:
            self._cwd[cwd] = self._origin_key(cwd) if os.path.isdir(cwd) else None
        return self._cwd[cwd]

    def _origin_key(self, cwd: str) -> str | None:
        try:
            r = subprocess.run(  # nosec B603 # constant args after -C cwd, no shell
                git_argv(cwd, "remote", "get-url", "origin"),
                capture_output=True, timeout=2, env=git_env())
            url = r.stdout.decode("utf-8", "replace").strip()
            return self.of(repo_identity(url)) if r.returncode == 0 and url else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None     # no git, a stuck remote config, a malformed URL: the key is NULL
