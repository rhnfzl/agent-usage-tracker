"""Shared collector helpers: byte-offset JSONL tailing + small normalizers."""

from __future__ import annotations

import os
from datetime import datetime, timezone


def read_new_lines(path: str, offset: int) -> tuple[list[bytes], int]:
    """Read complete newline-terminated records added after `offset`.

    Returns (complete_lines, new_offset). A trailing partial line (no newline yet)
    is left unconsumed so the next scan picks it up whole. Handles truncation/rotation
    by resetting to 0 when the file shrank below the stored offset.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    if size <= offset:
        return [], offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    if not data:
        return [], offset
    if data.endswith(b"\n"):
        complete = data.split(b"\n")[:-1]
        consumed = len(data)
    else:
        parts = data.split(b"\n")
        complete = parts[:-1]
        consumed = len(data) - len(parts[-1])
    return complete, offset + consumed


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
