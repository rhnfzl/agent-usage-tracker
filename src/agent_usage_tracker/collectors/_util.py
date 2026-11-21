"""Shared collector helpers: inode-aware byte-offset JSONL tailing + small normalizers."""

from __future__ import annotations

import copy
import os
import sys
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Sequence

from ..event import Batch, CollectorUnavailable, Row
from ..diagnostics import note_skips

# Cap lines read per file per scan so a huge first-scan/backfill file can't spike memory.
# The offset is committed at the cap, so the next scan simply continues from there.
_MAX_LINES = 50000
MS_PER_S = 1000
# What a malformed log record can raise while it is read. OverflowError: int(inf) from a 1e400.
MALFORMED_RECORD = (KeyError, TypeError, ValueError, AttributeError, OverflowError)


@dataclass
class FileSnapshot:
    """Finish admitted history even when native files keep growing."""
    bounds: dict[str, tuple[int, int]] = field(default_factory=dict)
    frozen: bool = False
    deferred: dict[str, dict[str, Any]] = field(default_factory=dict)

    def restart_deferred(self) -> None:
        # A truncated file can remain as long as its old offset; force a fresh read next run.
        for path, files in self.deferred.items():
            files.pop(path, None)


_SNAPSHOT: ContextVar[FileSnapshot | None] = ContextVar("native_file_snapshot", default=None)


@contextmanager
def file_snapshot(snapshot: FileSnapshot) -> Iterator[None]:
    token = _SNAPSHOT.set(snapshot)
    try:
        yield
    finally:
        _SNAPSHOT.reset(token)


class _SnapshotChanged(Exception):
    """The admitted file generation vanished before its bounded read completed."""


def describe_files(cfg: object, wm: dict[str, Any]) -> str:
    """`aut status` line of a collector whose watermark tracks files."""
    return f"{len(wm['files'])} files tracked" if "files" in wm else "never scanned"


def describe_full_scan(cfg: object, wm: dict[str, Any]) -> str:
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
    raw = record.get(key) or 0
    count = int(raw)
    if count < 0 or (isinstance(raw, float) and raw < 0):
        raise ValueError("negative token count")
    return count


def noop_advance() -> None:
    """The watermark callback of a full-scan collector: it has no watermark to advance."""


def warn_skipped(name: str, skipped: Counter[str], unaffected: str = "usage") -> None:
    """Report a collector's skips. Skipping is best effort on purpose: a failed activity extraction
    never blocks the usage and session rows of the same chunk, and one bad record never wedges a
    scan at its offset. But a malformed usage record's tokens are gone, so neither stays silent.
    Activity failures are keyed "activity:<exception type>": the type names a systematic parser
    break and, unlike a message, cannot carry record content."""
    note_skips(skipped)
    failed = {k.partition(":")[2]: n for k, n in sorted(skipped.items())
              if k.startswith("activity:") and n}
    if failed:
        print(f"aut: {name}: {sum(failed.values())} activity record(s) skipped (extraction failed: "
              f"{', '.join(f'{k} x{n}' for k, n in failed.items())}); "
              f"{unaffected} and session rows are unaffected.", file=sys.stderr)
    if skipped["missing_payloads"]:
        print(f"aut: {name}: {skipped['missing_payloads']} record(s) have no stored payload; "
              "source coverage remains incomplete.", file=sys.stderr)
    if skipped["malformed"]:
        print(f"aut: {name}: {skipped['malformed']} malformed record(s) skipped; any usage they "
              "held is not counted.", file=sys.stderr)


def raise_if_unreadable(name: str, skipped: Counter[str]) -> None:
    """After a collector's last batch: files that are there but could not be read make the scan
    degraded instead of looking like files with nothing new."""
    if skipped["unreadable"]:
        raise CollectorUnavailable(f"{name}: {skipped['unreadable']} file(s) could not be read, "
                                   "left for the next scan")


def read_new_lines(path: str, offset: int, *, end_offset: int | None = None,
                   ) -> tuple[list[bytes], int]:
    """Stream complete newline-terminated records added after `offset` (capped at _MAX_LINES).

    Iterates one line at a time, through end_offset when supplied, and stops at a trailing partial line
    (no newline yet), leaving it for the next scan. Returns (complete_lines, new_offset). A file
    that vanished has nothing new; any other OSError (no permission, a failing disk) is the
    caller's to report.
    """
    lines: list[bytes] = []
    consumed = offset
    try:
        with open(path, "rb") as fh:
            snapshot = _SNAPSHOT.get()
            expected = snapshot.bounds.get(path) if snapshot else None
            if expected:
                st = os.fstat(fh.fileno())
                if st.st_ino != expected[0] or st.st_size < expected[1]:
                    raise _SnapshotChanged
            fh.seek(offset)
            while end_offset is None or consumed < end_offset:
                raw = fh.readline(-1 if end_offset is None else end_offset - consumed)
                if not raw:
                    break
                if raw.endswith(b"\n"):
                    lines.append(raw)
                    consumed += len(raw)
                    if len(lines) >= _MAX_LINES:
                        break    # commit here; next scan continues from `consumed`
                else:
                    break        # trailing partial line at EOF; picked up whole next time
            if expected and os.fstat(fh.fileno()).st_size < expected[1]:
                raise _SnapshotChanged
    except FileNotFoundError:
        return [], offset        # gone now; leave the offset put
    return lines, consumed


def same_file(fstate: dict[str, Any], st: os.stat_result) -> bool:
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
    double-counts. A guaranteed re-push every reboot is the worse trade. Entries saved by older
    versions still hold a `dev` key; nothing reads it."""
    return fstate.get("ino") == st.st_ino and st.st_size >= safe_int(fstate.get("off"))


def changed_files(paths: Iterable[str], files: dict[str, Any], fresh: Callable[[os.stat_result], Any],
                  skipped: Counter[str], revisit: Callable[[Any], bool] | None = None,
                  ) -> Iterator[tuple[str, Any, int, list[bytes], int]]:
    """Each file of `paths` with something to do: (path, private copy of its entry in `files`,
    offset read from, complete new lines, new offset). That is a file with bytes past its entry,
    one whose entry must restart (replaced or truncated: an offset left standing would skip bytes
    if it regrew past it), and one whose entry `revisit` wants looked at with no new bytes.
    `fresh(st)` is the entry of a file seen for the first time or replaced. The copy is deep so
    that only the caller's watermark callback can publish a chunk's state. A file that cannot be
    read is counted in skipped["unreadable"]."""
    snapshot = _SNAPSHOT.get()
    for path in paths:
        end_offset = None
        if snapshot:
            if path in snapshot.deferred or (snapshot.frozen and path not in snapshot.bounds):
                continue
            bound = snapshot.bounds.get(path)
            entry = files.get(path) or {}
            if (bound and snapshot.frozen and safe_int(entry.get("off")) >= bound[1]
                    and not (revisit and revisit(entry))):
                continue
        try:
            st = os.stat(path)
            if snapshot:
                bound = snapshot.bounds.setdefault(path, (st.st_ino, st.st_size))
                if st.st_ino != bound[0] or st.st_size < bound[1]:
                    snapshot.deferred[path] = files
                    continue
                end_offset = bound[1]
            entry = files.get(path) or {}
            restart = bool(entry) and not same_file(entry, st)
            prev = fresh(st) if restart or not entry else entry
            off = safe_int(prev.get("off"))
            size = st.st_size if end_offset is None else end_offset
            if size <= off and not restart and not (revisit and revisit(prev)):
                continue
            lines, new_off = (read_new_lines(path, off) if end_offset is None else
                              read_new_lines(path, off, end_offset=end_offset))
        except _SnapshotChanged:
            assert snapshot is not None
            snapshot.deferred[path] = files
            continue
        except FileNotFoundError:   # gone since the glob: nothing to read, and no offset to lose
            continue
        except OSError:
            skipped["unreadable"] += 1
            continue
        yield path, copy.deepcopy(prev), off, lines, new_off


def file_batches(name: str, paths: Iterable[str], files: dict[str, Any],
                 fresh: Callable[[os.stat_result], Any],
                 chunk_rows: Callable[[str, Any, int, list[bytes], Counter[str]], Sequence[Row]],
                 revisit: Callable[[Any], bool] | None = None) -> Iterator[Batch]:
    """A tailing collector's batches over changed_files. `chunk_rows(path, cur, off, lines, skipped)`
    makes one file's rows and updates `cur`, the private copy of its entry, whose "off" already
    holds the new offset; the entry is published only when the batch's callback runs."""
    skipped: Counter[str] = Counter()
    for path, cur, off, lines, new_off in changed_files(paths, files, fresh, skipped, revisit):
        cur["off"] = new_off
        events = chunk_rows(path, cur, off, lines, skipped)

        def advance_watermark(p: str = path, c: dict[str, Any] = cur) -> None:
            files[p] = c

        yield events, advance_watermark
    warn_skipped(name, skipped)
    raise_if_unreadable(name, skipped)


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
