"""Collect explicitly configured normalized SDK events after delivery acknowledgment."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
from typing import Any, Callable, Iterator

from ..config import AutError, Config
from ..event import Batch, UsageEvent, make_event_id
from ..observations import COUNTS, MAX_BATCH_RECORDS, MAX_FILE_BYTES, MAX_STORE_RECORDS
from ..sdk import validate_envelope

NAME = "generic_sdk"


def describe(cfg: Config, wm: dict[str, Any]) -> str:
    return f"{len(wm.get('records', {}))} acknowledged SDK identities (full scan)"


def collect(cfg: Config, wm: dict[str, Any]) -> Iterator[Batch]:
    pattern = cfg.generic_sdk_paths
    if not pattern or not pattern.strip() or "\x00" in pattern or "://" in pattern:
        raise AutError("generic_sdk_paths requires an explicit local file or glob")
    paths = sorted(set(glob.glob(os.path.expanduser(pattern), recursive=True)))
    if not paths:
        raise AutError("generic_sdk_paths matched no files")
    known = wm.get("records", {})
    pending: dict[str, str] = {}
    events: dict[str, UsageEvent] = {}
    unsupported = unavailable = 0
    for path in paths:
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("file exceeds limit")
            # The final unterminated line belongs to the next pass, even if it parses.
            for line in data.split(b"\n")[:-1]:
                value = validate_envelope(json.loads(line))
                native = value.get("native_source")
                if native in cfg.agents:
                    raise AutError("generic_sdk overlap: native_source collector is enabled")
                usage = value["usage"]
                identity = make_event_id(NAME, value["producer"], usage["id"])
                digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                prior = pending.get(identity, known.get(identity))
                if prior is not None:
                    if prior != digest:
                        raise AutError("generic_sdk conflicting producer/event identity")
                    continue
                pending[identity] = digest
                if len(known) + len(pending) > MAX_STORE_RECORDS:
                    raise ValueError("identity limit exceeded")
                if any(key in usage for key in ("counter", "temporality", "start")):
                    unsupported += 1
                    continue
                required = [
                    key for key in COUNTS if key not in ("input_tokens_total", "reasoning_tokens")
                ]
                if any(usage[key] is None for key in required):
                    unavailable += 1
                    continue
                total = sum(usage[key] for key in required)
                if total > 2**63 - 1:
                    raise ValueError("total tokens exceeds int64")
                counts = {key: usage[key] for key in (*required, "reasoning_tokens")}
                events[identity] = UsageEvent(
                    event_id=identity,
                    ts=usage["timestamp"],
                    agent=NAME,
                    model=value.get("model"),
                    provider=value.get("provider"),
                    total_tokens=total,
                    **counts,
                )
        except (OSError, ValueError, TypeError) as exc:
            raise AutError("generic_sdk malformed or unreadable local source") from exc
    if unsupported or unavailable:
        print(
            f"aut: generic_sdk unsupported={unsupported} unavailable={unavailable}", file=sys.stderr
        )

    def acknowledge_batch(records: dict[str, str]) -> Callable[[], None]:
        def acknowledge() -> None:
            wm.setdefault("records", {}).update(records)

        return acknowledge

    # Validate every source before yielding so late conflicts cannot partially deliver a scan.
    identities = list(pending)
    for offset in range(0, max(1, len(identities)), MAX_BATCH_RECORDS):
        selected = identities[offset : offset + MAX_BATCH_RECORDS]
        yield (
            [events[key] for key in selected if key in events],
            acknowledge_batch({key: pending[key] for key in selected}),
        )
