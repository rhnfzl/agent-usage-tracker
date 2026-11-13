"""Append explicit, normalized SDK usage to a private local JSONL file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import ALL_AGENTS
from .observations import MAX_FILE_BYTES, MAX_RECORD_BYTES, identifier, validate_record


def validate_envelope(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) - {"version", "producer", "usage", "model", "provider", "native_source"}
        or value.get("version") != 1
        or type(value.get("version")) is not int
    ):
        raise ValueError("invalid SDK envelope")
    result = dict(value)
    result["producer"] = identifier(value.get("producer"))
    result["usage"] = validate_record(value.get("usage"))
    if result["usage"]["kind"] != "usage":
        raise ValueError("SDK envelope requires usage")
    for key in ("model", "provider"):
        item = value.get(key)
        result[key] = item
        if item is not None and (
            not isinstance(item, str)
            or not item
            or len(item) > 128
            or any(ord(c) < 32 for c in item)
        ):
            raise ValueError(f"invalid SDK {key}")
    native = value.get("native_source")
    result["native_source"] = native
    if native is not None and (native not in ALL_AGENTS or native == "generic_sdk"):
        raise ValueError("invalid SDK native_source")
    if len(json.dumps(result).encode()) > MAX_RECORD_BYTES:
        raise ValueError("SDK record exceeds limit")
    return result


def emit_usage(
    path: str | Path,
    usage: dict[str, Any],
    *,
    producer: str,
    model: str | None = None,
    provider: str | None = None,
    native_source: str | None = None,
) -> None:
    """Append one validated record; callers supply stable identity and timestamp."""
    envelope = validate_envelope(
        dict(
            version=1,
            producer=producer,
            usage=usage,
            model=model,
            provider=provider,
            native_source=native_source,
        )
    )
    payload = (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(Path(path).expanduser(), os.O_CREAT | os.O_APPEND | os.O_RDWR, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                raise ValueError("SDK file has an incomplete trailing record")
        if size + len(payload) > MAX_FILE_BYTES:
            raise ValueError("SDK file exceeds limit; rotate it before appending")
        if os.write(fd, payload) != len(payload):
            raise OSError("incomplete SDK append")
    finally:
        os.close(fd)
