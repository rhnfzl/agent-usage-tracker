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


def _is_human_text(content: object) -> bool:
    """True for a genuine typed prompt: a plain string (typed text or a slash command) or a
    block list that is ALL text. False for the tool_result block-lists Claude Code writes as
    role=user after each tool call, so those injections don't count as you prompting."""
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return bool(content) and all(
            isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


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

        # Claude Code writes ONE JSONL line per content block of an assistant message (text +
        # each tool_use), and every one of those lines repeats the SAME cumulative message.usage.
        # Keying by uuid (unique per line) would count a 6-block message 6x. So dedup by
        # (message.id, requestId) within the scan, keeping the max-total record (also correct if
        # the repeats are cumulative-growing rather than identical), and derive event_id from that
        # key so the sink dedups the same message across scans too.
        #
        # Accepted-low: a message whose blocks straddle a scan boundary (a scan racing an in-flight
        # write) or the _MAX_LINES chunk cap is deduped per-chunk, so its two halves can carry
        # different first-block ts and both persist (sink conflicts on (event_id, ts)) -> that one
        # message double-counts. Reachability is a mid-write race on a single message; impact is one
        # message; same trust class as the other accepted lows (st_ino reuse, TOCTOU). Not worth a
        # persistent per-message ts cache to close.
        best: dict[str, UsageEvent] = {}
        human: list[UsageEvent] = []
        for raw in lines:
            try:
                o = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue
            if not isinstance(o, dict):
                continue
            typ = o.get("type")
            if typ == "user":
                # A real human prompt: string content (typed text or a slash command) or an
                # all-text block list. Skip the tool_result user-lines Claude Code injects after
                # every tool call (they dominate the raw "user" count), plus synthetic
                # meta/compact/sidechain lines - none of those are you prompting.
                if o.get("isMeta") or o.get("isCompactSummary") or o.get("isSidechain"):
                    continue
                m = o.get("message")
                if isinstance(m, dict) and _is_human_text(m.get("content")):
                    uid = o.get("uuid") or f"{path}:{o.get('timestamp')}"
                    human.append(UsageEvent(
                        event_id=make_event_id(NAME, "human", uid),
                        ts=o.get("timestamp"), agent=NAME, model=None, provider=None,
                        kind="human", project=basename(o.get("cwd")),
                    ))
                continue
            if typ != "assistant":
                continue
            try:
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                inp = int(u.get("input_tokens") or 0)
                out = int(u.get("output_tokens") or 0)
                cr = int(u.get("cache_read_input_tokens") or 0)
                cw = int(u.get("cache_creation_input_tokens") or 0)
            except (ValueError, TypeError, AttributeError,
                    OverflowError):  # OverflowError: int(float('inf')) from 1e400
                continue
            total = inp + out + cr + cw
            if total == 0:
                continue
            msg_id = msg.get("id")
            # message.id groups a message's blocks; requestId disambiguates a retried message.
            # Fall back to uuid / path:ts only for old transcripts that lack message.id.
            key = f"{msg_id}|{o.get('requestId')}" if msg_id else \
                (o.get("uuid") or f"{path}:{o.get('timestamp')}")
            prev = best.get(key)
            if prev is not None and prev.total_tokens >= total:
                continue                         # already have the fuller (or equal) record
            best[key] = UsageEvent(
                event_id=make_event_id(NAME, key),
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
            )
        events = list(best.values()) + human

        def commit(p: str = path, s: dict = new_fstate) -> None:
            files[p] = s

        yield events, commit
