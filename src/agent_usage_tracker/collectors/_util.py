"""Shared collector helpers: inode-aware byte-offset JSONL tailing + small normalizers."""

from __future__ import annotations

import copy
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator

# Cap lines read per file per scan so a huge first-scan/backfill file can't spike memory.
# The offset is committed at the cap, so the next scan simply continues from there.
_MAX_LINES = 50000
MS_PER_S = 1000


def describe_files(cfg: object, wm: dict) -> str:
    """`aut status` line of a collector whose watermark tracks files."""
    return f"{len(wm['files'])} files tracked" if "files" in wm else "never scanned"


def describe_full_scan(cfg: object, wm: dict) -> str:
    """`aut status` line of a collector that keeps no watermark."""
    return "full-scan each run (no watermark)"


def str_or_none(value: object) -> str | None:
    """A non-empty string as is; anything else is None."""
    return value if isinstance(value, str) and value else None


def dict_or_empty(value: object) -> dict[str, Any]:
    """A dict as is; anything else is {}, for an untrusted nested object in a log record."""
    return value if isinstance(value, dict) else {}


def safe_int(value: object, default: int = 0) -> int:
    """Non-negative int() that never raises, for untrusted counts, offsets and timestamps from
    logs and state. A negative or unparseable value becomes `default`: a corrupt state offset
    (1e400 -> inf -> OverflowError, or a negative one, which would make fh.seek() raise) must not
    wedge the scan."""
    if not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = int(value)
    except (ValueError, OverflowError):
        return default
    return parsed if parsed >= 0 else default


def count_field(record: dict[str, Any], key: str) -> int:
    """A token count from a log record; absent or null is 0. Raises on a malformed one, which the
    caller counts as a malformed record."""
    return int(record.get(key) or 0)


def noop_advance() -> None:
    """The watermark callback of a full-scan collector: it has no watermark to advance."""


def parse_record(raw: bytes | str) -> dict | None:
    """One complete JSONL line as a dict, else None. read_new_lines holds back partial lines, so
    there a None is a real anomaly: the caller counts it as malformed and skips it. A reader that
    only shows local text just skips it."""
    try:
        o = json.loads(raw)
    except ValueError:              # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None
    return o if isinstance(o, dict) else None


def warn_skipped(name: str, skipped: Counter, unaffected: str = "usage") -> None:
    """Report a collector's skips. Skipping is best effort on purpose: a failed activity extraction
    never blocks the usage and session rows of the same chunk, and one bad record never wedges a
    scan at its offset. But a malformed usage record's tokens are gone, so neither stays silent."""
    if skipped["activity"]:
        print(f"aut: {name}: {skipped['activity']} activity record(s) skipped (extraction failed); "
              f"{unaffected} and session rows are unaffected.", file=sys.stderr)
    if skipped["malformed"]:
        print(f"aut: {name}: {skipped['malformed']} malformed record(s) skipped; any usage they "
              "held is not counted.", file=sys.stderr)


def read_new_lines(path: str, offset: int) -> tuple[list[bytes], int]:
    """Stream complete newline-terminated records added after `offset` (capped at _MAX_LINES).

    Iterates one line at a time (no whole-file read) and stops at a trailing partial line
    (no newline yet), leaving it for the next scan. Resets to 0 when the file shrank below
    the stored offset (truncation). Returns (complete_lines, new_offset).
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:            # truncated -> reread from the start
        offset = 0
    if size <= offset:
        return [], offset
    lines: list[bytes] = []
    consumed = offset
    try:                         # TOCTOU: the file can vanish/rotate between getsize and open
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if raw.endswith(b"\n"):
                    lines.append(raw)
                    consumed += len(raw)
                    if len(lines) >= _MAX_LINES:
                        break    # commit here; next scan continues from `consumed`
                else:
                    break        # trailing partial line at EOF; picked up whole next time
    except OSError:
        return [], offset        # gone/unreadable now; leave the offset put, retry next scan
    return lines, consumed


def same_file(fstate: dict, st: os.stat_result) -> bool:
    """May a tail resume from this watermark entry? False on first sight (empty entry), when the
    file behind the path changed (rotated/replaced), and when it was truncated in place, so the
    beginning of a replacement is never skipped.

    Identity is st_ino ALONE, deliberately not (st_dev, st_ino). st_dev is assigned at mount,
    not intrinsic to the volume: macOS/APFS hands out a different number after every reboot. Any
    watermark holding st_dev therefore goes stale on restart, every tracked path looks replaced,
    and the whole history is re-read and re-pushed. (Idempotent at the sink, but on a heavy
    machine that is a multi-minute scan and ~500k redundant rows per reboot.)

    What dropping it costs: an inode is only unique within a device, so a path that migrates to
    another filesystem could collide with a same-numbered inode there and be resumed mid-file.
    That needs a tracked path to change volumes AND collide, and it undercounts rather than
    double-counts. A guaranteed re-push every reboot is the worse trade. st_dev is still recorded
    for diagnostics."""
    return fstate.get("ino") == st.st_ino and st.st_size >= safe_int(fstate.get("off"))


def scan_file(path: str, fstate: dict) -> tuple[list[bytes], dict]:
    """Inode-aware tail. `fstate` carries {'dev','ino','off'} (empty on first sight); the offset
    restarts at 0 unless same_file allows resuming. Returns (complete_lines, new_fstate)."""
    try:
        st = os.stat(path)
    except OSError:
        return [], fstate
    off = safe_int(fstate.get("off")) if same_file(fstate, st) else 0
    lines, new_off = read_new_lines(path, off)
    new_fstate = dict(fstate)
    new_fstate["dev"] = st.st_dev
    new_fstate["ino"] = st.st_ino
    new_fstate["off"] = new_off
    return lines, new_fstate


def changed_files(paths: Iterable[str], files: dict,
                  fresh: Callable[[os.stat_result], dict]) -> Iterator[tuple[str, dict, int, list[bytes], int]]:
    """Each file of `paths` with bytes past its watermark entry in `files`: (path, private copy of
    the entry, offset read from, complete new lines, new offset). `fresh(st)` is the entry of a
    file seen for the first time or replaced. The copy is deep so that only the caller's watermark
    callback can publish a chunk's state."""
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:         # gone since the glob: nothing to read, and no offset to lose
            continue
        prev = files.get(path) or {}
        if not same_file(prev, st):
            prev = fresh(st)
        off = safe_int(prev.get("off"))
        if st.st_size <= off:
            continue
        lines, new_off = read_new_lines(path, off)
        yield path, copy.deepcopy(prev), off, lines, new_off


def _strip_worktree(parts: list[str]) -> list[str] | None:
    """Rewrite a git-worktree checkout path to the equivalent path in its MAIN checkout.

    Without this every worktree becomes its own `project`: one repo worked on across 50 branches
    reports as 50 projects and no per-project total is real. A worktree IS the same repo, so
    `<repo>/<container>/<branch>/sub` normalizes to `<repo>/sub` -- which also keeps a monorepo's
    inner package attributed to that package rather than to the branch it was checked out on.

    Git puts a worktree wherever you point it, so no rule catches every layout. These are the two
    conventional ones, and both require a DOT-directory, so an ordinary project folder can never
    trip this:

        <repo>/.claude/worktrees/<branch>/...   Claude Code's own layout (a `worktrees` dir
                                                inside any dot-directory)
        <repo>/.worktrees/<branch>/...          the common hand-rolled form (any dot-directory
                                                whose name ends in `worktrees`, so
                                                `.claude-worktrees` matches too)

    Anything else (`git worktree add ../repo-wt`) is indistinguishable from a sibling repo by
    path alone, so it is left as-is. Returns None when there is nothing to rewrite."""
    for i, seg in enumerate(parts):
        if not seg.startswith("."):
            continue
        if seg.lower().endswith("worktrees"):
            drop = 2                                                  # .worktrees/<branch>
        elif i + 1 < len(parts) and parts[i + 1].lower() == "worktrees":
            drop = 3                                                  # .claude/worktrees/<branch>
        else:
            continue
        if i + drop > len(parts):     # the container itself, no branch under it -> leave alone
            return None
        return parts[:i] + parts[i + drop:]
    return None


def project_name(path: object) -> str | None:
    """Last path segment only (never a full path -> safe for a public-later repo), after
    collapsing a git worktree back onto its main checkout (see _strip_worktree). Handles BOTH
    '/' and '\\' so a Windows path is reduced on POSIX too; rejects non-strings (returns None)
    so a stray list/dict can't be str()-leaked as a 'project'."""
    if not isinstance(path, str) or not path:
        return None
    seg = path.rstrip("/\\").replace("\\", "/")
    rewritten = _strip_worktree(seg.split("/"))
    if rewritten and any(rewritten):    # all-empty == the path was ONLY a container; keep it
        seg = "/".join(rewritten)
    return seg.rsplit("/", 1)[-1] or None


def epoch_ms_to_iso(ms: object) -> str | None:
    if not isinstance(ms, (int, float, str)):
        return None
    try:
        return datetime.fromtimestamp(float(ms) / MS_PER_S, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None
