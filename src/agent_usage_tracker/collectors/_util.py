"""Shared collector helpers: inode-aware byte-offset JSONL tailing + small normalizers."""

from __future__ import annotations

import os
from datetime import datetime, timezone

# Cap lines read per file per scan so a huge first-scan/backfill file can't spike memory.
# The offset is committed at the cap, so the next scan simply continues from there.
_MAX_LINES = 50000


def safe_int(value, default: int = 0) -> int:
    """Non-negative int() that never raises. Guards a corrupt state offset (1e400 -> inf ->
    OverflowError) AND a negative offset (which would make fh.seek() raise and wedge the scan);
    every caller feeds a byte offset, which is always >= 0."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= 0 else default


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


def scan_file(path: str, fstate: dict) -> tuple[list[bytes], dict]:
    """Inode-aware tail. `fstate` carries {'dev','ino','off'} (empty on first sight). Resets the
    offset to 0 when the file behind the path changes (rotated/replaced) so the beginning of a
    replacement file is never skipped. Returns (complete_lines, new_fstate).

    Identity is st_ino ALONE, deliberately not (st_dev, st_ino). st_dev is assigned at mount,
    not intrinsic to the volume: macOS/APFS hands out a different number after every reboot. Any
    watermark holding st_dev therefore goes stale on restart, every tracked path looks replaced,
    and the whole history is re-read and re-pushed. (Idempotent at the sink, but on a heavy
    machine that is a multi-minute scan and ~500k redundant rows per reboot.)

    What dropping it costs: an inode is only unique within a device, so a path that migrates to
    another filesystem could collide with a same-numbered inode there and be resumed mid-file.
    That needs a tracked path to change volumes AND collide, and it undercounts rather than
    double-counts. A guaranteed re-push every reboot is the worse trade. st_dev is still recorded
    for diagnostics. Truncation-in-place is caught separately by read_new_lines (size < offset)."""
    try:
        st = os.stat(path)
    except OSError:
        return [], fstate
    off = safe_int(fstate.get("off"))
    if fstate.get("ino") != st.st_ino:            # different file at this path (rotated/replaced)
        off = 0
    lines, new_off = read_new_lines(path, off)
    new_fstate = dict(fstate)
    new_fstate["dev"] = st.st_dev
    new_fstate["ino"] = st.st_ino
    new_fstate["off"] = new_off
    return lines, new_fstate


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


def basename(path) -> str | None:
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


def epoch_ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None
