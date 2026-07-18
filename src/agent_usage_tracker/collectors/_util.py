"""Shared collector helpers: inode-aware byte-offset JSONL tailing + small normalizers."""

from __future__ import annotations

import os
from datetime import datetime, timezone


def read_new_lines(path: str, offset: int) -> tuple[list[bytes], int]:
    """Stream complete newline-terminated records added after `offset`.

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
    off = int(fstate.get("off", 0))
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
    except (TypeError, ValueError, OSError):
        return None
