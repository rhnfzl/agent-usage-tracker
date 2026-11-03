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
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterator

from ..config import Config
from ..event import EventSession, UsageEvent, make_event_id
from ._activity_common import IDLE_S
from ._util import basename, safe_int, scan_file
from .claude_code_activity import Activity, RepoKeys
from .claude_code_activity import _text as _text_of

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

# Newer Claude Code stamps who started each turn. turnOrigin is authoritative when present: any
# value but "human" (task_notification, peer, scheduled, system, sdk, auto_continuation) is
# positively not a person typing. promptSource backs it up on records that carry only that field.
_NON_HUMAN_PROMPT_SOURCES = {"system", "sdk"}


def _origin_not_human(o: dict) -> bool:
    origin = o.get("turnOrigin")
    if isinstance(origin, str) and origin != "human":
        return True
    return o.get("promptSource") in _NON_HUMAN_PROMPT_SOURCES


# Machine-generated text Claude Code injects AS a role=user turn. These are not tool_results, so
# _is_human_text accepts them, but nobody typed them. Still needed after turnOrigin: older files
# lack it, and even current ones write some peer messages without it.
_INJECTED_PREFIXES = (
    "<task-notification>",      # a background task reporting completion back into the session
    "<local-command-stdout>",   # output of a slash command echoed back ("Goodbye!")
    "[Request interrupted",     # the harness noting you pressed escape -- an action, not a prompt
    "Another Claude session sent a message",   # a peer session messaging this one
    "[Cross-session idle notice]",             # the harness reporting another session went idle
    "This session is being continued from a previous",  # compaction summary without its flag
    "<bash-stdout>",            # output of a `!` shell command (the typed `<bash-input>` stays)
    "<bash-stderr>",
    "Stop hook feedback:",      # a Stop hook's verdict fed back as a user turn (AgentsView, MIT)
)
# Context blocks the harness or IDE attaches to a turn. A turn made of nothing else was not typed
# (filter ported from AgentsView, github.com/kenn-io/agentsview, MIT licence).
_CONTEXT_BLOCK_RE = re.compile(
    r"<(system-reminder|ide_selection|ide_opened_file)>.*?</\1>", re.S)
_USAGE_CMD = "<command-name>/usage</command-name>"


_LOOP_RE = re.compile(r"<command-(?:message>(?:[\w-]+:)?loop</command-message|name>/loop</command-name)>")


def _is_old_loop(o: dict, text: str) -> bool:
    """A /loop record from before Claude Code wrote turnOrigin. Then, every scheduled firing
    looked exactly like the /loop you typed; newer files mark all repeats `scheduled`."""
    return "turnOrigin" not in o and bool(_LOOP_RE.search(text))


def _is_injected(text: str) -> bool:
    """True for harness-generated role=user turns. A `<command-name>` slash-command expansion is
    deliberately NOT here: you typed the slash command, so it stays a human turn."""
    return (text.lstrip().startswith(_INJECTED_PREFIXES)
            or (bool(text.strip()) and not _CONTEXT_BLOCK_RE.sub("", text).strip()))


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _file_identity(base: Path, path: str) -> tuple[bool, str, str | None]:
    """(is_subagent, fallback session_id, fallback parent) from the path alone. A subagent lives at
    <parent>/subagents/[workflows/<wf>/]agent-<id>.jsonl; a main session at <sessionId>.jsonl."""
    parts = Path(path).relative_to(base).parts
    stem = Path(path).stem
    if "subagents" in parts[1:-1]:       # parts[0] is the project dir, whatever its name
        i = parts.index("subagents", 1)
        return True, stem.removeprefix("agent-"), parts[i - 1] if i else None
    return False, stem, None


def _session_of(o: dict, ident: tuple[bool, str, str | None], turn: str | None) -> EventSession:
    sub, sid, parent = ident
    if sub:     # records carry sessionId = the PARENT session and agentId = their own
        return EventSession(_str(o.get("agentId")) or sid, _str(o.get("sessionId")) or parent,
                            "subagent", turn)
    ep = o.get("entrypoint")
    origin = ("automated" if ep in _PROGRAM_ENTRYPOINTS
              else "interactive" if ep == "cli" else "unknown")
    return EventSession(_str(o.get("sessionId")) or sid, None, origin, turn)


def _complete(path: str, off: int) -> bool:
    """Read to its end and untouched for IDLE_S: no more records will follow."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return off >= st.st_size and time.time() - st.st_mtime >= IDLE_S


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list[UsageEvent], Callable[[], None]]]:
    base = cfg.home / ".claude" / "projects"
    if not base.exists():
        return
    files: dict = wm.setdefault("files", {})
    keys = RepoKeys(cfg.repo_key_salt)   # cwd -> repo_key cached once per collect() pass
    act_errors = 0

    for path in glob.glob(str(base / "**" / "*.jsonl"), recursive=True):
        fstate = files.get(path, {})
        lines, new_fstate = scan_file(path, fstate)
        # a file with an open link turn is looked at again once it went idle, to close it
        if not lines and new_fstate == fstate and not (fstate.get("act") or {}).get("lo"):
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
        # shortcut: counted per scan chunk, so an old file read in two chunks keeps two /loops;
        # harmless while old files are complete on disk and scanned once.
        old_loops = 0
        ident = _file_identity(base, path)
        # turn_id: promptId of the latest user record (tool_results carry it too); a subagent keeps
        # its first one, the parent turn that spawned it.
        # Carried across chunks in the file's watermark entry, so a turn that began in an earlier
        # scan is not lost. Dropped when the path now holds a different file (rotated/replaced).
        # shortcut: files first read before this existed start at NULL until their next user record.
        same_file = fstate.get("ino") == new_fstate.get("ino")
        turn: str | None = _str(fstate.get("turn")) if same_file else None
        # activity state rides in the same watermark entry and is dropped with it on a new file
        act = Activity(NAME, path, ident[0], fstate.get("act") if same_file else None, keys)
        fresh = not same_file or safe_int(fstate.get("off")) == 0     # read from byte 0
        steers: list = []
        for raw in lines:
            try:
                o = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue
            if not isinstance(o, dict):
                continue
            typ = o.get("type")
            if typ == "user":
                pid = _str(o.get("promptId"))
                if pid and not (ident[0] and turn):
                    turn = pid
            act.record(o, _session_of(o, ident, turn))
            if typ == "user":
                # A real human prompt: string content (typed text or a slash command) or an
                # all-text block list. Skip the tool_result user-lines Claude Code injects after
                # every tool call (they dominate the raw "user" count), plus synthetic
                # meta/compact/sidechain lines - none of those are you prompting.
                if o.get("isMeta") or o.get("isCompactSummary") or o.get("isSidechain"):
                    continue
                # An SDK program driving the session is not a person prompting. Unknown/absent
                # entrypoints stay counted: only drop what is positively identified as a program.
                if o.get("entrypoint") in _PROGRAM_ENTRYPOINTS or _origin_not_human(o):
                    continue
                m = o.get("message")
                text = _text_of(m.get("content")) if isinstance(m, dict) else ""
                if (isinstance(m, dict) and _is_human_text(m.get("content"))
                        and not _is_injected(text)):
                    if _is_old_loop(o, text):
                        old_loops += 1
                        if old_loops > 1:        # you typed the first; the rest are firings
                            continue
                    uid = o.get("uuid") or f"{path}:{o.get('timestamp')}"
                    sess = _session_of(o, ident, turn)
                    human.append(UsageEvent(
                        event_id=make_event_id(NAME, "human", uid),
                        ts=o.get("timestamp"), agent=NAME, model=None, provider=None,
                        kind="human", project=basename(o.get("cwd")), session=sess,
                    ))
                    steers.append((act.steer(o, uid, text, sess), _USAGE_CMD in text))
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
                session=_session_of(o, ident, turn),
            )
        if _complete(path, safe_int(new_fstate.get("off"))):
            act.finish()
        # A session that only ran /usage (no model call, every prompt a /usage) was nobody working
        # (AgentsView, MIT). Judged only on a read from byte 0, so a /usage typed mid-session and
        # caught alone by a live scan is kept, as a full re-read keeps it. shortcut: a file whose
        # first chunk held only the /usage keeps it dropped if real work follows; rare.
        if fresh and not best and steers and all(is_usage for _s, is_usage in steers):
            human, steers = [], []
        events = list(best.values()) + human + [s for s, _u in steers if s] + act.events
        act_errors += act.errors
        if turn:
            new_fstate["turn"] = turn
        else:
            new_fstate.pop("turn", None)
        carried = act.state()
        if carried:
            new_fstate["act"] = carried
        else:
            new_fstate.pop("act", None)

        def commit(p: str = path, s: dict = new_fstate) -> None:
            files[p] = s

        yield events, commit
    if act_errors:
        print(f"aut: {NAME}: {act_errors} activity record(s) skipped (extraction failed); usage "
              "and session rows are unaffected.", file=sys.stderr)
