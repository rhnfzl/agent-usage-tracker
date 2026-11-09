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
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, NamedTuple, TypedDict

from ..config import Config
from ..event import Row, ActivityEvent, Batch, EventSession, RunOrigin, UsageEvent, make_event_id
from ..repo_keys import RepoKeys
from ..state import parse_record
from .activity_common import IDLE_S, best_effort, drop_raw_shas, human_event, pad_pending, text_of
from ._util import MALFORMED_RECORD, count_field, file_batches, project_name, str_or_none
from .claude_code_activity import Activity, ActivityState

NAME = "claude_code"
# The watermark's format. Older state is upgraded once by _upgrade_state; delete a step from there
# once no machine can still hold state that old.
_STATE_V = 2


class FileState(TypedDict, total=False):
    """One file's watermark entry. An entry saved by an older version also holds an unread `dev`."""
    off: int
    ino: int
    turn: str               # the turn the next record belongs to, until its next user record
    old_loop_seen: bool
    act: ActivityState


def projects_dir(home: Path) -> Path:
    return home / ".claude" / "projects"


def main_session_files(home: Path) -> list[str]:
    """Main sessions only (<project>/<id>.jsonl); subagents sit deeper and number tens of thousands."""
    return glob.glob(str(projects_dir(home) / "*" / "*.jsonl"))


def project_session_files(home: Path, project: str) -> list[str]:
    """The main sessions of one working directory; Claude Code names the directory after its path."""
    project_dir = projects_dir(home) / re.sub(r"[^A-Za-z0-9]", "-", project)
    return [str(f) for f in project_dir.glob("*.jsonl")]


def is_interactive(path: str | Path) -> bool:
    """A main session started at a terminal (entrypoint cli), from its first records."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(200), fh):
                if '"entrypoint"' in line:
                    o = parse_record(line)
                    return o is not None and o.get("entrypoint") == "cli"
    except OSError:                     # unreadable: it cannot be shown to be interactive
        return False
    return False


def _is_human_text(content: object) -> bool:
    """True for a genuine typed prompt: a plain string (typed text or a slash command) or a
    non-empty block list with no tool_result (text, possibly with pasted images). False for the
    tool_result block-lists Claude Code writes as role=user after each tool call, so those
    injections don't count as you prompting."""
    if isinstance(content, str):
        return True
    if isinstance(content, list):
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


def _origin_not_human(o: dict[str, Any]) -> bool:
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
CONTEXT_BLOCK_RE = re.compile(
    r"<(system-reminder|ide_selection|ide_opened_file)>.*?</\1>", re.S)
_USAGE_CMD = "<command-name>/usage</command-name>"


_LOOP_RE = re.compile(r"<command-(?:message>(?:[\w-]+:)?loop</command-message|name>/loop</command-name)>")


def is_old_loop(o: dict[str, Any], text: str) -> bool:
    """A /loop record from before Claude Code wrote turnOrigin. Then, every scheduled firing
    looked exactly like the /loop you typed; newer files mark all repeats `scheduled`."""
    return "turnOrigin" not in o and bool(_LOOP_RE.search(text))


def _is_injected(text: str) -> bool:
    """True for harness-generated role=user turns. A `<command-name>` slash-command expansion is
    deliberately NOT here: you typed the slash command, so it stays a human turn."""
    return (text.lstrip().startswith(_INJECTED_PREFIXES)
            or (bool(text.strip()) and not CONTEXT_BLOCK_RE.sub("", text).strip()))


def human_text(o: dict[str, Any]) -> str | None:
    """The text of a record a person typed, else None. A real human prompt is string content
    (typed text or a slash command) or a non-empty block list with no tool_result. Skipped: the tool_result
    user-lines Claude Code injects after every tool call (they dominate the raw "user" count),
    synthetic meta/compact/sidechain lines, and an SDK program driving the session (unknown or
    absent entrypoints stay counted: only what is positively a program is dropped)."""
    if o.get("type") != "user" or o.get("isMeta") or o.get("isCompactSummary") \
            or o.get("isSidechain"):
        return None
    if o.get("entrypoint") in _PROGRAM_ENTRYPOINTS or _origin_not_human(o):
        return None
    m = o.get("message")
    if not isinstance(m, dict) or not _is_human_text(m.get("content")):
        return None
    text = text_of(m.get("content"))
    return None if _is_injected(text) else text


class FileIdentity(NamedTuple):
    subagent: bool
    session_id: str               # the fallback when a record does not say
    parent: str | None            # the same


def _file_identity(base: Path, path: str) -> FileIdentity:
    """What the path alone says. A subagent lives at
    <parent>/subagents/[workflows/<wf>/]agent-<id>.jsonl; a main session at <sessionId>.jsonl."""
    parts = Path(path).relative_to(base).parts
    stem = Path(path).stem
    if "subagents" in parts[1:-1]:       # parts[0] is the project dir, whatever its name
        i = parts.index("subagents", 1)
        return FileIdentity(True, stem.removeprefix("agent-"), parts[i - 1])
    return FileIdentity(False, stem, None)


def _session_of(o: dict[str, Any], ident: FileIdentity, turn: str | None) -> EventSession:
    sub, sid, parent = ident
    if sub:     # records carry sessionId = the PARENT session and agentId = their own
        return EventSession(str_or_none(o.get("agentId")) or sid,
                            str_or_none(o.get("sessionId")) or parent,
                            "subagent", turn)
    ep = o.get("entrypoint")
    origin: RunOrigin = ("automated" if ep in _PROGRAM_ENTRYPOINTS
              else "interactive" if ep == "cli" else "unknown")
    return EventSession(str_or_none(o.get("sessionId")) or sid, None, origin, turn)


def _is_fully_read(path: str, off: int) -> bool:
    """Read to its end and untouched for IDLE_S: no more records will follow."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return off >= st.st_size and time.time() - st.st_mtime >= IDLE_S


def _upgrade_state(wm: dict[str, Any]) -> None:
    """Bring state saved by an older version to the current shape, once: pending commit calls with
    their target, and link SHAs keyed. Each step is safe on state already upgraded."""
    if wm.get("state_v") == _STATE_V:
        return
    for entry in wm.setdefault("files", {}).values():
        pad_pending(entry.get("act"))
        drop_raw_shas(entry.get("act"))
    wm["state_v"] = _STATE_V


def _record_uid(o: dict[str, Any], path: str) -> str:
    return o.get("uuid") or f"{path}:{o.get('timestamp')}"


def steer_texts(home: Path, sessions: set[str], want: set[str], *,
                paths: Iterable[str] | None = None) -> dict[str, str]:
    """steer event_id -> its raw text, for the ids in `want`, from the main logs of `sessions`. The
    id comes from the same record uid the steer row was made with."""
    out: dict[str, str] = {}
    for f in main_session_files(home) if paths is None else paths:
        if Path(f).stem not in sessions:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"user"' not in line:
                    continue
                o = parse_record(line)          # a malformed line holds no steer to show
                if o is None or o.get("type") != "user":
                    continue
                eid = make_event_id(NAME, "steer", _record_uid(o, f))
                m = o.get("message")
                if eid in want and isinstance(m, dict):
                    out[eid] = text_of(m.get("content"))
    return out


def agent_replies(home: Path, sessions: set[str], want: set[str], until: datetime | None = None,
                  *, paths: Iterable[str] | None = None,
                  ) -> dict[str, tuple[str, str]]:
    """steer event_id -> (the agent's last text before it, the agent's last text after it and
    before the next steer in `want`), from the main logs of `sessions`. Text blocks only: no tool
    output and no thinking. A record at or after `until` is not read, so a replay sees only what
    was said by then."""
    out: dict[str, tuple[str, str]] = {}
    for f in main_session_files(home) if paths is None else paths:
        if Path(f).stem not in sessions:
            continue
        last, cur = "", None
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                o = parse_record(line)          # a malformed line holds no reply to show
                if o is None:
                    continue
                ts = _record_ts(o)
                if until and ts and ts >= until:
                    break
                m = o.get("message")
                if o.get("type") == "assistant" and isinstance(m, dict):
                    text = text_of(m.get("content"))
                    if text.strip():
                        last = text
                        if cur:
                            out[cur] = (out[cur][0], text)
                elif o.get("type") == "user":
                    eid = make_event_id(NAME, "steer", _record_uid(o, f))
                    if eid in want:
                        out[eid], cur = (last, ""), eid
    return out


def _record_ts(o: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(o.get("timestamp")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _usage_event(o: dict[str, Any], path: str, ident: FileIdentity,
                 turn: str | None) -> tuple[str, UsageEvent] | None:
    """(dedup key, usage row) of an assistant record, None if it has no tokens. Raises on malformed
    token counts."""
    msg = o.get("message")
    if not isinstance(msg, dict):
        return None
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None
    inp = count_field(u, "input_tokens")
    out = count_field(u, "output_tokens")
    cr = count_field(u, "cache_read_input_tokens")
    cw = count_field(u, "cache_creation_input_tokens")
    total = inp + out + cr + cw
    if total == 0:
        return None
    msg_id = msg.get("id")
    # message.id groups a message's blocks; requestId disambiguates a retried message.
    # Fall back to uuid / path:ts only for old transcripts that lack message.id.
    key = f"{msg_id}|{o.get('requestId')}" if msg_id else _record_uid(o, path)
    return key, UsageEvent(
        event_id=make_event_id(NAME, key),
        ts=o.get("timestamp"),
        agent=NAME,
        model=str_or_none(msg.get("model")),
        provider="anthropic",
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        total_tokens=total,
        project=project_name(o.get("cwd")),
        session=_session_of(o, ident, turn),
    )


def _next_turn(o: dict[str, Any], is_subagent: bool, turn: str | None) -> str | None:
    """turn_id after a user record: the latest promptId (tool_results carry it too); a subagent
    keeps its first one, the parent turn that spawned it."""
    pid = str_or_none(o.get("promptId"))
    return pid if pid and not (is_subagent and turn) else turn


def _keep_fuller(best: dict[str, UsageEvent], o: dict[str, Any], path: str,
                 ident: FileIdentity, turn: str | None, skipped: Counter[str]) -> None:
    """Fold an assistant record into best: the fuller record wins, an equal one keeps the first.

    Claude Code writes ONE JSONL line per content block of an assistant message (text + each
    tool_use), and every one of those lines repeats the SAME cumulative message.usage. Keying by
    uuid (unique per line) would count a 6-block message 6x. So dedup by (message.id, requestId)
    within the scan, keeping the max-total record (also correct if the repeats are
    cumulative-growing rather than identical), and derive event_id from that key so the sink dedups
    the same message across scans too.

    ts is whichever line last raised the max within THIS chunk, and that is not stable across
    scans: Claude Code re-logs a message more than once, observed both a thousand-plus lines later
    in the same file and with a message's own lines minutes apart. A scan seeing only part of that
    spread emits the same event_id with a different ts, and the sink's key is (event_id, ts) -- a
    TimescaleDB hypertable must carry its partitioning column in the unique index -- so both rows
    persist and the partial one would double-count.

    Not fixable here: which lines a scan sees is exactly what varies, so no per-chunk rule is
    invariant, and closing it would need a per-message ts cache in the watermark for every tracked
    file. The sink's agents.usage_event_dedup view settles it instead, keeping the largest total
    per event_id (counts are cumulative, so the largest is the complete one)."""
    try:
        found = _usage_event(o, path, ident, turn)
    except MALFORMED_RECORD:
        skipped["malformed"] += 1
        return
    if found and (found[0] not in best or best[found[0]].total_tokens < found[1].total_tokens):
        best[found[0]] = found[1]


def _only_ran_usage(fresh: bool, best: dict[str, UsageEvent],
                    steers: list[tuple[ActivityEvent | None, bool]]) -> bool:
    """A session that only ran /usage (no model call, every prompt a /usage) was nobody working
    (AgentsView, MIT). Judged only on a read from byte 0, so a /usage typed mid-session and caught
    alone by a live scan is kept, as a full re-read keeps it. shortcut: a file whose first chunk
    held only the /usage keeps it dropped if real work follows; rare."""
    return fresh and not best and bool(steers) and all(is_usage for _s, is_usage in steers)


def _human_row(o: dict[str, Any], text: str, path: str, sess: EventSession,
               act: Activity) -> tuple[UsageEvent, tuple[ActivityEvent | None, bool]]:
    """The human row of a typed prompt, and its steer row with whether the prompt ran /usage."""
    uid = _record_uid(o, path)
    row = human_event(NAME, (uid,), o.get("timestamp"), sess, project_name(o.get("cwd")))
    return row, (act.steer(o, uid, text, sess), _USAGE_CMD in text)


def _old_loop_before(path: str, off: int) -> bool:
    """Recover the missing flag from an older watermark without re-emitting historical prompts."""
    if not off:
        return False
    with open(path, "rb") as stream:
        while stream.tell() < off:
            raw = stream.readline(off - stream.tell())
            if not raw:
                break
            o = parse_record(raw)
            text = human_text(o) if o else None
            if o and text is not None and is_old_loop(o, text):
                return True
    return False


def _chunk_rows(path: str, base: Path, lines: list[bytes], from_start: bool, cur: FileState,
                keys: RepoKeys, skipped: Counter[str]) -> list[Row]:
    """All rows of one file's new lines, read from byte 0 or not; updates `cur`, the private copy
    of its watermark entry."""
    best: dict[str, UsageEvent] = {}
    human: list[UsageEvent] = []
    ident = _file_identity(base, path)
    # turn_id is carried across chunks in the file's watermark entry, so a turn that began in an
    # earlier scan is not lost.
    # shortcut: files first read before this existed start at NULL until their next user record.
    turn = str_or_none(cur.get("turn"))
    act = Activity(NAME, path, ident.subagent, cur.get("act"), keys)
    steers: list[tuple[ActivityEvent | None, bool]] = []
    for raw in lines:
        o = parse_record(raw)
        if o is None:
            skipped["malformed"] += 1
            continue
        try:
            typ = o.get("type")
            if typ == "user":
                turn = _next_turn(o, ident.subagent, turn)
            sess = _session_of(o, ident, turn)
            best_effort(act.errors, act.record, o, sess)
            if typ == "user":
                text = human_text(o)
                if text is None:
                    continue
                if is_old_loop(o, text):
                    if cur.get("old_loop_seen"):    # only the first was typed
                        continue
                    cur["old_loop_seen"] = True
                row, steer = _human_row(o, text, path, sess, act)
                human.append(row)
                steers.append(steer)
            elif typ == "assistant":
                _keep_fuller(best, o, path, ident, turn, skipped)
        except MALFORMED_RECORD:    # one bad line must not wedge every later scan at this offset
            skipped["malformed"] += 1
    if _is_fully_read(path, cur["off"]):
        act.finish()
    if _only_ran_usage(from_start, best, steers):
        human, steers = [], []
    skipped.update(act.errors)
    if turn:
        cur["turn"] = turn
    else:
        cur.pop("turn", None)
    state = act.state()
    if state:
        cur["act"] = state
    else:
        cur.pop("act", None)
    return [*best.values(), *human, *(s for s, _u in steers if s), *act.events]


def _fresh_state(st: os.stat_result) -> FileState:
    return {"off": 0, "ino": st.st_ino}


def _has_open_link_turn(entry: FileState) -> bool:
    """Such a file is looked at again with no new bytes: once it has gone idle, its turn closes."""
    return bool((entry.get("act") or {}).get("lo"))


def collect(cfg: Config, wm: dict[str, Any]) -> Iterator[Batch]:
    base = projects_dir(cfg.home)
    if not base.exists():
        return
    _upgrade_state(wm)
    keys = RepoKeys(cfg.repo_key_salt)   # cwd -> repo_key cached once per collect() pass

    def chunk_rows(path: str, cur: FileState, off: int, lines: list[bytes],
                   skipped: Counter[str]) -> list[Row]:
        if "old_loop_seen" not in cur:
            cur["old_loop_seen"] = _old_loop_before(path, off)
        return _chunk_rows(path, base, lines, not off, cur, keys, skipped)

    yield from file_batches(NAME, glob.glob(str(base / "**" / "*.jsonl"), recursive=True),
                            wm["files"], _fresh_state, chunk_rows, _has_open_link_turn)
