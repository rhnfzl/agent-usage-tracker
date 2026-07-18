"""Codex CLI collector: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Streaming parse: session_meta gives provider + cwd, turn_context updates the current
model, and each `token_count` event carries a per-turn `last_token_usage` delta. We emit
one row per token_count event (its delta), NOT the cumulative total, so re-scans never
double-count. Codex's input_tokens INCLUDES cached, so we split it into fresh input +
cache_read to match the other agents. Subscription-safe (plan_type: pro observed).

Per-file streaming state is persisted so resuming from a byte offset keeps the right
model/session/ordinal across scans.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Callable, Iterator

from ..config import Config
from ..event import UsageEvent, make_event_id
from ._util import basename, read_new_lines

NAME = "codex"


def _fresh_state() -> dict:
    return {"offset": 0, "model": None, "provider": "openai",
            "session_id": None, "cwd": None, "tc_ord": 0}


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    base = cfg.home / ".codex" / "sessions"
    if not base.exists():
        return
    files: dict = wm.setdefault("files", {})

    for path in glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        st = files.get(path) or _fresh_state()
        off = st.get("offset", 0)
        if size < off:            # truncated/rotated -> reparse from scratch
            st = _fresh_state()
            off = 0
        if size <= off:
            continue

        lines, new_off = read_new_lines(path, off)
        cur = dict(st)            # mutate a copy; commit swaps it in after push
        events: list[UsageEvent] = []
        for raw in lines:
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            p = o.get("payload") or {}
            if t == "session_meta":
                cur["session_id"] = p.get("session_id") or p.get("id") or cur["session_id"]
                cur["provider"] = p.get("model_provider") or cur["provider"]
                cur["cwd"] = p.get("cwd") or cur["cwd"]
            elif t == "turn_context":
                if p.get("model"):
                    cur["model"] = p.get("model")
                if p.get("cwd"):
                    cur["cwd"] = p.get("cwd")
            elif t == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                last = info.get("last_token_usage") or {}
                cur["tc_ord"] += 1     # advance ordinal for EVERY token_count (stable id)
                inp = int(last.get("input_tokens") or 0)
                cached = int(last.get("cached_input_tokens") or 0)
                out = int(last.get("output_tokens") or 0)
                rea = int(last.get("reasoning_output_tokens") or 0)
                if inp + out + rea == 0:
                    continue
                total = int(last.get("total_tokens") or (inp + out))
                sid = cur["session_id"] or os.path.basename(path)
                events.append(UsageEvent(
                    event_id=make_event_id(NAME, sid, cur["tc_ord"]),
                    ts=o.get("timestamp"),
                    agent=NAME,
                    model=cur["model"],
                    provider=cur["provider"] or "openai",
                    input_tokens=max(inp - cached, 0),
                    output_tokens=out,
                    cache_read_tokens=cached,
                    cache_write_tokens=0,
                    reasoning_tokens=rea,
                    total_tokens=total,
                    project=basename(cur["cwd"]),
                ))
        cur["offset"] = new_off

        def commit(p: str = path, c: dict = cur) -> None:
            files[p] = c

        yield events, commit
