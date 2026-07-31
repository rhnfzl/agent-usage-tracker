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
        # A real prompt may mix text with pasted images; exclude only the tool_result
        # block-lists Claude Code writes as role=user after each tool call.
        return bool(content) and all(
            isinstance(b, dict) and b.get("type") != "tool_result" for b in content)
    return False


# entrypoint values that mean an SDK program is driving the session, not a person at a terminal.
# Their prompts are ordinary role=user text and pass every other check, so entrypoint is the only
# thing separating "you asked for a security review" from "a harness asked for one 700 times".
_PROGRAM_ENTRYPOINTS = {"sdk-py", "sdk-cli"}

# Machine-generated text Claude Code injects AS a role=user turn. These are not tool_results, so
# _is_human_text accepts them, but nobody typed them.
_INJECTED_PREFIXES = (
    "<task-notification>",      # a background task reporting completion back into the session
    "<local-command-stdout>",   # output of a slash command echoed back ("Goodbye!")
    "[Request interrupted",     # the harness noting you pressed escape -- an action, not a prompt
)


def _is_injected(text: str) -> bool:
    """True for harness-generated role=user turns. A `<command-name>` slash-command expansion is
    deliberately NOT here: you typed the slash command, so it stays a human turn."""
    return text.lstrip().startswith(_INJECTED_PREFIXES)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


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
        # ts is whichever line last raised the max within THIS chunk, and that is not stable
        # across scans: Claude Code re-logs a message more than once, observed both a thousand-plus
        # lines later in the same file and with a message's own lines minutes apart. A scan seeing
        # only part of that spread emits the same event_id with a different ts, and the sink's key
        # is (event_id, ts) -- a TimescaleDB hypertable must carry its partitioning column in the
        # unique index -- so both rows persist and the partial one would double-count.
        #
        # Not fixable here: which lines a scan sees is exactly what varies, so no per-chunk rule is
        # invariant, and closing it would need a per-message ts cache in the watermark for every
        # tracked file. report.dedup_by_event settles it instead, keeping the largest total per
        # event_id (counts are cumulative, so the largest is the complete one).
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
                # An SDK program driving the session is not a person prompting. Unknown/absent
                # entrypoints stay counted: only drop what is positively identified as a program.
                if o.get("entrypoint") in _PROGRAM_ENTRYPOINTS:
                    continue
                m = o.get("message")
                if (isinstance(m, dict) and _is_human_text(m.get("content"))
                        and not _is_injected(_text_of(m.get("content")))):
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
