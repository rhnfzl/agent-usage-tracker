"""`aut backup [--check]`: an encrypted, deduplicated restic snapshot of what aut reads and keeps.

The sink stores counts and keys, never text, and a re-read of logs that are gone is impossible. So
a dead or replaced laptop loses its transcripts, its ActivityWatch history, the repo-key salt and
the label gold files, unless they live somewhere else too. This copies them to a restic repository
once a day (packaging/launchd/dev.aut.backup.plist). docs/new-laptop.md is the restore.

Settings come from the [backup] table of config.toml: repository and password_command (restic runs
the command itself, so aut never holds the password), and optional paths (replace the defaults)
and excludes (added to the defaults). A run that cannot or should not happen now is skipped with
exit 0 and a log line: another backup still running, the laptop on battery below 20%, the
repository unreachable (off the VPN). A wrong password or a missing repository is an error. Each
good run records its time in backup.json next to the state file, which `aut status` shows, then
runs the retention: restic forget of this machine's aut snapshots, with --prune once a week.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 # restic and pmset, argv lists, no shell
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AutError, Config, config_path
from .state import atomic_write_text, load_json_dict, state_path

try:
    import fcntl
except ImportError:          # Windows: no flock; restic's own repository lock still applies
    fcntl = None  # type: ignore[assignment]

MIN_BATTERY = 20             # percent: below this, on battery, the run waits for the next one
_REACH_TIMEOUT_S = 120
# restic exit codes (0.17+): 3 = snapshot saved but some files could not be read
_PARTIAL, _NO_REPO, _BAD_PASSWORD = 3, 10, 12
# the one retention for this repository (other docs point here): keep 14 daily, 8 weekly
# and 12 monthly snapshots
RETENTION = ("--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "12")
PRUNE_EVERY = timedelta(days=7)


def default_paths(cfg: Config) -> list[Path]:
    from .label.core import data_dir     # here, not at the top: `aut` itself loads no label module
    h = cfg.home
    return [h / ".claude" / "projects", h / ".codex" / "sessions", h / ".codex" / "history.jsonl",
            h / ".codex" / "archived_sessions", h / ".cursor" / "chats",
            h / "Library" / "Application Support" / "activitywatch",
            config_path().parent,           # config.toml, label.json, scan.env
            data_dir(),                     # label state and eval/ gold files; models excluded
            state_path().parent]            # repo_key.salt, the ActivityWatch watermark


def default_excludes() -> list[str]:
    from .label.core import data_dir
    return [str(data_dir() / "label" / "models")]      # model caches: GBs, downloaded again


def _status_path() -> Path:
    return state_path().parent / "backup.json"


def _log(msg: str) -> None:
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} aut backup: {msg}")


def _record(result: str, ok: bool = False) -> None:
    st = load_json_dict(_status_path())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    st.update(last_run=now, last_result=result, **({"last_ok": now} if ok else {}))
    atomic_write_text(_status_path(), json.dumps(st))


def describe() -> str:
    """One line for `aut status`: the age of the last good backup."""
    st = load_json_dict(_status_path())
    if not st:
        return "never ran"
    ok = st.get("last_ok")
    if not ok:
        return f"never succeeded (last run: {st.get('last_result')})"
    hours = int((datetime.now(timezone.utc) - datetime.fromisoformat(ok)).total_seconds() // 3600)
    age = f"{hours} h" if hours < 48 else f"{hours // 24} d"
    return f"last good {age} ago (last run: {st.get('last_result')})"


def _battery() -> tuple[bool, int] | None:
    """(on battery, percent) from pmset, or None where there is no pmset or no battery."""
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,  # nosec B603 B607
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)%", out)
    return ("Battery Power" in out, int(m.group(1))) if m else None


def _restic(args: list[str], env: dict[str, str],
            timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["restic", *args], capture_output=True, text=True,  # nosec B603 B607
                              env=env, timeout=timeout)
    except FileNotFoundError:
        raise AutError("aut backup: restic is not installed or not on PATH (brew install restic; "
                       "a launchd job needs PATH set, see dev.aut.backup.plist)") from None


def _env(cfg: Config) -> dict[str, str]:
    if not cfg.backup_repository or not cfg.backup_password_command:
        raise AutError("aut backup: set repository and password_command in the [backup] table of "
                       "config.toml (see config.example.toml)")
    env = {k: v for k, v in os.environ.items()
           if k not in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE")}
    env.update(RESTIC_REPOSITORY=cfg.backup_repository,
               RESTIC_PASSWORD_COMMAND=cfg.backup_password_command)
    return env


def _tail(r: subprocess.CompletedProcess[str]) -> str:
    return " | ".join((r.stderr or r.stdout or "").strip().splitlines()[-3:])


def _reachable(env: dict[str, str]) -> bool:
    try:
        r = _restic(["cat", "config"], env, timeout=_REACH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False
    if r.returncode == _BAD_PASSWORD:
        raise AutError("aut backup: restic rejected the password (check password_command)")
    if r.returncode == _NO_REPO:
        raise AutError("aut backup: the repository does not exist; create it once with "
                       "`restic init` (docs/new-laptop.md)")
    return r.returncode == 0


def run_backup(cfg: Config, check: bool = False) -> int:
    """One backup (or with `check`, a `restic check` of the repository, sending nothing)."""
    env = _env(cfg)
    lock_path = state_path().parent / "backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        if fcntl is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _log("another backup is still running; skipped")
                return 0
        return _locked(cfg, env, check)


def _skip(reason: str, check: bool) -> int:
    _log(f"skipped: {reason}")
    if not check:
        _record(f"skipped: {reason}")
    return 0


def _locked(cfg: Config, env: dict[str, str], check: bool) -> int:
    batt = _battery()
    if batt and batt[0] and batt[1] < MIN_BATTERY:
        return _skip(f"on battery at {batt[1]}%", check)
    if not _reachable(env):
        return _skip("repository unreachable", check)
    if check:
        r = _restic(["check"], env)
        if r.returncode != 0:
            raise AutError(f"aut backup: restic check failed (exit {r.returncode}): {_tail(r)}")
        _log("restic check: no errors")
        _log(f"last good backup: {describe()}")
        return 0
    paths = [str(p) for p in (Path(x).expanduser() for x in cfg.backup_paths)] \
        if cfg.backup_paths else [str(p) for p in default_paths(cfg)]
    paths = [p for p in paths if Path(p).exists()]
    if not paths:
        raise AutError("aut backup: none of the paths to back up exist")
    excludes: list[Any] = []
    for x in [*default_excludes(), *cfg.backup_excludes]:
        excludes += ["--exclude", x]
    # --host: the machine name, so a new laptop's first snapshot finds the old one as its parent
    r = _restic(["backup", "--tag", "aut", "--host", cfg.machine, "--exclude-caches",
                 *excludes, *paths], env)
    if r.returncode not in (0, _PARTIAL):
        _record(f"failed (exit {r.returncode})")
        raise AutError(f"aut backup: restic backup failed (exit {r.returncode}): {_tail(r)}")
    for line in r.stdout.splitlines():
        if line.startswith(("Files:", "Added to the repository", "snapshot ")):
            _log(line.strip())
    if r.returncode == _PARTIAL:
        print(f"aut backup: warning: some files could not be read: {_tail(r)}", file=sys.stderr)
    _record("ok" if r.returncode == 0 else "ok, some files unreadable", ok=True)
    _forget(cfg, env)
    return 0


def _forget(cfg: Config, env: dict[str, str]) -> None:
    """The retention, after a good backup: this machine's aut snapshots only. The prune (it reads
    and rewrites pack files) runs at most once a week; the forget alone is cheap."""
    st = load_json_dict(_status_path())
    last = st.get("last_prune")
    prune = not last or datetime.now(timezone.utc) - datetime.fromisoformat(last) >= PRUNE_EVERY
    r = _restic(["forget", "--tag", "aut", "--host", cfg.machine, *RETENTION,
                 *(["--prune"] if prune else [])], env)
    if r.returncode != 0:
        _record(f"ok, forget failed (exit {r.returncode})")
        raise AutError(f"aut backup: the backup is saved, but restic forget failed "
                       f"(exit {r.returncode}): {_tail(r)}")
    if prune:
        st = load_json_dict(_status_path())
        st["last_prune"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        atomic_write_text(_status_path(), json.dumps(st))
        _log("forget + prune done")
