"""Pi collector: ~/.pi/agent/sessions/**/*.jsonl  (pi.dev, @earendil-works/pi-coding-agent)

Cleanest of the four: each `message` record carries message.usage
{input, output, cacheRead, cacheWrite, totalTokens, cost} plus message.model. Model can
also change mid-session via `model_change` records, so we track it as streaming state.
Subscription-safe (OAuth or API key both log the same usage object).
"""

from __future__ import annotations

import glob
import json
import os
from typing import Callable, Iterator

from ..config import Config
from ..event import UsageEvent, make_event_id
from ._util import read_new_lines

NAME = "pi"


def _pi_project(path: str) -> str | None:
    """Session dir is the working dir encoded with dashes; recover the last segment."""
    parent = os.path.basename(os.path.dirname(path)).strip("-")
    tail = [seg for seg in parent.split("-") if seg]
    return tail[-1] if tail else None


def _cost_total(cost) -> float | None:
    if isinstance(cost, dict):
        v = cost.get("total")
        return float(v) if isinstance(v, (int, float)) else None
    return float(cost) if isinstance(cost, (int, float)) else None


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    base = cfg.home / ".pi"
    if not base.exists():
        return
    files: dict = wm.setdefault("files", {})

    for path in glob.glob(str(base / "**" / "*.jsonl"), recursive=True):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        st = files.get(path) or {"offset": 0, "model": None}
        off = st.get("offset", 0)
        if size < off:
            st = {"offset": 0, "model": None}
            off = 0
        if size <= off:
            continue

        lines, new_off = read_new_lines(path, off)
        cur = dict(st)
        events: list[UsageEvent] = []
        for raw in lines:
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            msg = o.get("message") or {}
            if t in ("model_change", "session"):
                m = o.get("model") or msg.get("model")
                if m:
                    cur["model"] = m
                continue
            if t != "message":
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            inp = int(u.get("input") or 0)
            out = int(u.get("output") or 0)
            cr = int(u.get("cacheRead") or 0)
            cw = int(u.get("cacheWrite") or 0)
            total = int(u.get("totalTokens") or (inp + out + cr + cw))
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
                project=_pi_project(path),
            ))
        cur["offset"] = new_off

        def commit(p: str = path, c: dict = cur) -> None:
            files[p] = c

        yield events, commit
