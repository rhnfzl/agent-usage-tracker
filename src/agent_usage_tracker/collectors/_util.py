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
    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if raw.endswith(b"\n"):
                lines.append(raw)
                consumed += len(raw)
                if len(lines) >= _MAX_LINES:
                    break        # commit here; next scan continues from `consumed`
            else:
                break            # trailing partial line at EOF; picked up whole next time
    return lines, consumed


def scan_file(path: str, fstate: dict) -> tuple[list[bytes], dict]:
    """Inode-aware tail. `fstate` carries {'ino','off'} (empty on first sight). Resets the
    offset to 0 when the inode changes (file rotated/replaced) so the beginning of a
    replacement file is never skipped. Returns (complete_lines, new_fstate)."""
    try:
        st = os.stat(path)
    except OSError:
        return [], fstate
    off = safe_int(fstate.get("off"))
    if fstate.get("ino") != st.st_ino:     # different file behind the same path
        off = 0
    lines, new_off = read_new_lines(path, off)
    new_fstate = dict(fstate)
    new_fstate["ino"] = st.st_ino
    new_fstate["off"] = new_off
    return lines, new_fstate


def basename(path: str | None) -> str | None:
    """Directory basename only (never a full path -> safe for a public-later repo)."""
    if not path:
        return None
    return os.path.basename(str(path).rstrip("/")) or None


def epoch_ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None
