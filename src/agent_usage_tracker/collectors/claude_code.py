"""Claude Code collector: ~/.claude/projects/**/*.jsonl

One row per `assistant` record. Token truth lives in message.usage. IMPORTANT: most of the
prompt rides in the cache fields (cache_read/creation), not input_tokens - this is exactly
the field people miss when they report a "174x undercount". We capture all four separately.
Subscription-safe (client writes these regardless of OAuth vs API-key auth).

Robustness: each record is parsed in isolation, so one corrupt line is skipped (and counted)
rather than wedging every future scan at the same offset. Note: if Anthropic ever RENAMES a
usage key, an absent key reads as 0 and would undercount silently - the v2 OTLP lane is the
intended cross-check for that.
"""

from __future__ import annotations

import glob
import json
from typing import Callable, Iterator

from ..config import Config
from ..event import UsageEvent, make_event_id
from ._util import basename, scan_file

NAME = "claude_code"


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    base = cfg.home / ".claude" / "projects"
    if not base.exists():
        return
    files: dict = wm.setdefault("files", {})

    for path in glob.glob(str(base / "**" / "*.jsonl"), recursive=True):
        fstate = files.get(path, {})
        lines, new_fstate = scan_file(path, fstate)
        if not lines and new_fstate == fstate:
            continue

        events: list[UsageEvent] = []
        for raw in lines:
            try:
                o = json.loads(raw)
                if o.get("type") != "assistant":
                    continue
                msg = o.get("message") or {}
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                inp = int(u.get("input_tokens") or 0)
                out = int(u.get("output_tokens") or 0)
                cr = int(u.get("cache_read_input_tokens") or 0)
                cw = int(u.get("cache_creation_input_tokens") or 0)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
                continue
            total = inp + out + cr + cw
            if total == 0:
                continue
            uid = o.get("uuid") or o.get("requestId") or f"{path}:{o.get('timestamp')}"
            events.append(UsageEvent(
                event_id=make_event_id(NAME, uid),
                ts=o.get("timestamp"),
                agent=NAME,
                model=msg.get("model"),
                provider="anthropic",
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                total_tokens=total,
                project=basename(o.get("cwd")),
            ))

        def commit(p: str = path, s: dict = new_fstate) -> None:
            files[p] = s

        yield events, commit
