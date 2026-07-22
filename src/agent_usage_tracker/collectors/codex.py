"""Codex CLI collector: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Streaming parse: session_meta gives provider + cwd, turn_context updates the current model,
and each `token_count` event carries a per-turn `last_token_usage` delta. We emit one row per
token_count event (its delta), NOT the cumulative total, so re-scans never double-count.
Codex's input_tokens INCLUDES cached, so we split it into fresh input + cache_read to match
the other agents. Subscription-safe (plan_type: pro observed).

Idempotency: the event id is a hash of stable content (session, timestamp, and the token
fields), NOT a positional ordinal, so a duplicated or reordered token_count line dedups at
the sink instead of double-counting. Per-file streaming state (model/session/offset/inode) is
persisted so resuming from a byte offset keeps the right attribution across scans.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Callable, Iterator

from ..config import Config
from ..event import UsageEvent, make_event_id
from ._util import basename, read_new_lines, safe_int

NAME = "codex"


def _fresh_state(dev: int | None = None, ino: int | None = None) -> dict:
    return {"off": 0, "dev": dev, "ino": ino, "model": None, "provider": "openai",
            "session_id": None, "cwd": None, "tc_ord": 0, "um_ord": 0}


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    base = cfg.home / ".codex" / "sessions"
    if not base.exists():
        return
    files: dict = wm.setdefault("files", {})

    for path in glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = files.get(path)
        # New/rotated file (or a state-format upgrade whose old state lacks 'dev'/'ino') -> reset
        # and re-read. Safe because event_id is content-deterministic: re-emitted rows dedup at
        # the sink. Identity is (st_dev, st_ino); ino alone is only unique within a device.
        if not prev or (prev.get("dev"), prev.get("ino")) != (st.st_dev, st.st_ino):
            prev = _fresh_state(st.st_dev, st.st_ino)
        off = safe_int(prev.get("off"))
        if st.st_size < off:                            # truncated
            prev = _fresh_state(st.st_dev, st.st_ino)
            off = 0
        if st.st_size <= off:
            continue

        lines, new_off = read_new_lines(path, off)
        cur = dict(prev)
        events: list[UsageEvent] = []
        for raw in lines:
            try:
                o = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(o, dict):
                continue
            t = o.get("type")
            p = o.get("payload") or {}
            if not isinstance(p, dict):
                continue
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
                try:
                    info = p.get("info")
                    last = info.get("last_token_usage") if isinstance(info, dict) else None
                    if not isinstance(last, dict):
                        continue
                    inp = int(last.get("input_tokens") or 0)
                    cached = int(last.get("cached_input_tokens") or 0)
                    out = int(last.get("output_tokens") or 0)
                    rea = int(last.get("reasoning_output_tokens") or 0)
                    total = int(last.get("total_tokens") or (inp + out))
                except (ValueError, TypeError, AttributeError, OverflowError):
                    continue
                cur["tc_ord"] += 1
                if inp + out + rea == 0:
                    continue
                ts = o.get("timestamp")
                sid = cur["session_id"] or os.path.basename(path)
                # content hash, not position; only fall back to the ordinal if ts is absent.
                tiebreak = "" if ts else cur["tc_ord"]
                events.append(UsageEvent(
                    event_id=make_event_id(NAME, sid, ts or "", inp, cached, out, rea, total, tiebreak),
                    ts=ts,
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
            elif t == "event_msg" and p.get("type") == "user_message":
                # A human prompt (or a scripted `codex exec` prompt - indistinguishable here).
                # No tokens; a stable per-session ordinal makes the id deterministic so a
                # re-read (file reset) re-emits the same id and the sink dedups it.
                cur["um_ord"] += 1
                ts = o.get("timestamp")
                sid = cur["session_id"] or os.path.basename(path)
                events.append(UsageEvent(
                    event_id=make_event_id(NAME, "human", sid, ts or "", cur["um_ord"]),
                    ts=ts, agent=NAME, model=None, provider=None,
                    kind="human", project=basename(cur["cwd"]),
                ))
        cur["off"] = new_off

        def commit(p: str = path, c: dict = cur) -> None:
            files[p] = c

        yield events, commit
