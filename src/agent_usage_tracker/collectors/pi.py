"""Pi collector: ~/.pi/agent/sessions/**/*.jsonl  (pi.dev, @earendil-works/pi-coding-agent)

Cleanest of the four: each `message` record carries message.usage
{input, output, cacheRead, cacheWrite, totalTokens, cost} plus message.model. Model can also
change mid-session via `model_change` records, so we track it as streaming state. Scoped to
the sessions directory so unrelated JSONL under ~/.pi cannot pollute totals. Subscription-safe.

`project` is left None: Pi encodes the working dir with dashes AND uses dashes as path
separators, so a hyphenated repo name cannot be recovered reliably (agent-usage-tracker would
become "tracker"). Better None than a confidently wrong value.
"""

from __future__ import annotations

import glob
import os
from typing import Callable, Iterator

import json

from ..config import Config
from ..event import UsageEvent, make_event_id
from ._util import read_new_lines, safe_int

NAME = "pi"


def _cost_total(cost) -> float | None:
    v = cost.get("total") if isinstance(cost, dict) else cost
    if isinstance(v, bool) or not isinstance(v, (int, float)):   # bool is an int subclass
        return None
    try:
        f = float(v)
    except OverflowError:            # e.g. a huge int literal
        return None
    if f != f or abs(f) == float("inf"):   # drop NaN / inf (the sink numeric can't store them)
        return None
    return f


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    base = cfg.home / ".pi" / "agent" / "sessions"
    if not base.exists():
        return
    files: dict = wm.setdefault("files", {})

    for path in glob.glob(str(base / "**" / "*.jsonl"), recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = files.get(path)
        if (not prev or (prev.get("dev"), prev.get("ino")) != (st.st_dev, st.st_ino)
                or st.st_size < safe_int(prev.get("off"))):   # identity is (dev, ino); see _util
            prev = {"off": 0, "dev": st.st_dev, "ino": st.st_ino, "model": None}
        off = safe_int(prev.get("off"))
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
            msg = o.get("message")
            if not isinstance(msg, dict):
                msg = {}
            if t in ("model_change", "session"):
                m = o.get("model") or msg.get("model")
                if m:
                    cur["model"] = m
                continue
            if t != "message":
                continue
            if msg.get("role") == "user":
                # A human prompt: plain text, no usage block. Emit as a human turn.
                events.append(UsageEvent(
                    event_id=make_event_id(NAME, "human", o.get("id") or f"{path}:{o.get('timestamp')}"),
                    ts=o.get("timestamp"), agent=NAME, model=None, provider=None,
                    kind="human", project=None,
                ))
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            try:
                inp = int(u.get("input") or 0)
                out = int(u.get("output") or 0)
                cr = int(u.get("cacheRead") or 0)
                cw = int(u.get("cacheWrite") or 0)
                total = int(u.get("totalTokens") or (inp + out + cr + cw))
            except (ValueError, TypeError, OverflowError):
                continue
            if total == 0:
                continue
            events.append(UsageEvent(
                event_id=make_event_id(NAME, o.get("id") or f"{path}:{o.get('timestamp')}"),
                ts=o.get("timestamp"),
                agent=NAME,
                model=msg.get("model") or cur["model"],
                provider=msg.get("provider"),
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                total_tokens=total,
                est_cost_usd=_cost_total(u.get("cost")),
                project=None,
            ))
        cur["off"] = new_off

        def commit(p: str = path, c: dict = cur) -> None:
            files[p] = c

        yield events, commit
