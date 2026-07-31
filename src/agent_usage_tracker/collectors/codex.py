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
            "session_id": None, "cwd": None, "tc_ord": 0, "um_ord": 0,
            "originator": None, "source": None}


# session_meta.source values that mean the thread was started by software, not by a person:
# 'exec' is a scripted `codex exec`, 'mcp' is a programmatic MCP call, and a dict carrying
# 'subagent' is a thread another agent spawned (whose opening prompt is the parent agent
# talking, and which would double-count the human prompt that started the parent anyway).
_PROGRAM_SOURCES = {"exec", "mcp"}


def _is_driven_by_a_program(originator: object, source: object) -> bool:
    """True when this session's user_messages come from software rather than a person.

    Without this every user_message counts as 'a prompt you sent', which on a machine that runs
    scheduled automations, `codex exec` harnesses and agent-spawned subagents overstates human
    input several-fold -- and since the showcase reports agent_turns_per_human_turn, an inflated
    denominator silently understates leverage.

    Structural signals only. Deciding this from the message text would be guesswork; originator
    and source are recorded by codex itself."""
    if isinstance(source, dict):
        return "subagent" in source
    if isinstance(source, str) and source in _PROGRAM_SOURCES:
        return True
    # Another coding agent driving codex through its own integration (the rescue/review flows).
    return isinstance(originator, str) and originator.strip().lower() == "claude code"


def _is_scheduled_automation(message: object) -> bool:
    """True for the preamble Codex Desktop puts on a scheduled automation run.

    Text-based, unlike _is_driven_by_a_program, because these arrive over the same originator and
    source as a person typing in the desktop app -- there is no structural flag to read. Both
    markers are required so an ordinary message that merely opens with the word 'Automation'
    is not swallowed."""
    if not isinstance(message, str):
        return False
    head = message.lstrip()[:200]
    return head.startswith("Automation:") and "Automation ID:" in head


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
        # New/rotated file (or a state-format upgrade whose old state lacks 'ino') -> reset and
        # re-read. Safe because event_id is content-deterministic: re-emitted rows dedup at the
        # sink. Identity is st_ino ALONE, deliberately not (st_dev, st_ino) -- st_dev is assigned
        # at mount, so macOS/APFS renumbers it on reboot and every file would look rotated. See
        # the long note in _util.scan_file; this collector keeps its own copy of the check because
        # it also tracks per-session ordinals, so keep the two rules in step.
        if (not prev or prev.get("ino") != st.st_ino
                or "um_ord" not in prev):   # state saved before um_ord existed -> re-read from
            prev = _fresh_state(st.st_dev, st.st_ino)   # offset 0 so user_message ordinals (and
            #                                            thus their event_ids) stay consistent if
            #                                            the file is later reset and re-read.
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
                # Who is driving this session. Recorded so user_message can tell a person typing
                # from a program prompting -- see _is_driven_by_a_program.
                cur["originator"] = p.get("originator")
                cur["source"] = p.get("source")
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
                # A prompt someone typed. Sessions a program drives (subagent threads, `codex
                # exec`, MCP calls, another agent's integration) and Codex Desktop's scheduled
                # automations emit the same event, so screen those out rather than counting them
                # as human input.
                if _is_driven_by_a_program(cur.get("originator"), cur.get("source")):
                    continue
                if _is_scheduled_automation(p.get("message")):
                    continue
                # No tokens; a stable per-session ordinal makes the id deterministic so a
                # re-read (file reset) re-emits the same id and the sink dedups it. The ordinal
                # advances only for kept messages, which is fine: it just has to be stable for a
                # given file, and the screening above is deterministic from the same bytes.
                # .get(): per-file state persisted before this field existed lacks um_ord.
                cur["um_ord"] = cur.get("um_ord", 0) + 1
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
