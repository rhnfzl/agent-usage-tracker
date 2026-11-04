"""Cursor CLI collector: the `cursor-agent` CLI's chat stores and the Cursor SDK's run stores.

CLI chats: ~/.cursor/chats/<h>/<uuid>/store.db (sqlite, opened read-only, nothing copied). Meta
key '0' is hex-encoded JSON (agentId, createdAt, latestRootBlobId, and subagentInfo on a chat
another agent spawned). `blobs` holds the messages (JSON) and root blobs (protobuf) that list the
messages in order as field 1, each a 32-byte blob id. Every step writes a new root. A summarized
or rewound chat starts a new list, so its older messages sit only in older roots: every root list
that is not a prefix of another one is read.

This collector emits NO token usage (the stores have none, and cursor_api counts these tokens).
It emits human rows (zero tokens) for the prompts, user messages carrying
`providerOptions.cursor.requestId` (the others are injected context or a summary), outside
subagent chats, plus their session rows and activity rows: steer, commit, rejection, turn.
SDK stores: ~/.cursor/projects/*/sdk-agent-store/*/index.db, one `turn` row per ended run.
"""

from __future__ import annotations

import copy
import glob
import json
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ..config import Config
from ..event import (Batch, ActivityEvent, CollectorUnavailable, EventSession, UsageEvent,
                     make_event_id)
from ..repo_keys import RepoKeys
from .activity_common import (Link, TurnAgg, add_call, blank_agg, commit_rows, ms_between, text_of,
                              turn_attrs)
from .commits import commit_call, commit_dir, commit_shas
from ._util import describe_files, epoch_ms_to_iso, warn_skipped
from .cursor import connect_ro

NAME = "cursor_cli"
# The store has no turn-end record, so a turn ends at the next prompt or once the chat sits idle.
# shortcut: a tool call logged under a prompt more than _IDLE_S after the chat went quiet is lost
# (turn rows are insert-only); upgrade if Cursor ever logs a turn end in the store.
_IDLE_S = 3600
_ENDED = {"FINISHED": "finished", "CANCELLED": "cancelled", "ERROR": "error"}
_BATCH = 500
_TS = re.compile(r"<timestamp>\s*\w+,\s*([A-Za-z]{3})\w*\.?\s+(\d{1,2}),\s*(\d{4}),\s*(\d{1,2}):(\d\d)"
                 r"\s*([AP]M)\s*\(UTC(?:([+-])(\d{1,2})(?::(\d\d))?)?\)\s*</timestamp>")
USER_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.S)
EXIT_LINE = re.compile(r"^Exit code: (-?\d+)\s*(?:Command output:\s*)?")
_IDENT = re.compile(r"[A-Za-z][A-Za-z_]*")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def chat_stores(home: Path) -> list[str]:
    """Every chat's store.db; a chat's directory name is its agentId in every store seen."""
    return sorted(glob.glob(str(home / ".cursor" / "chats" / "*" / "*" / "store.db")))


def _cursor_options(m: dict) -> dict:
    """The cursor block of a message's providerOptions."""
    po = m.get("providerOptions")
    c = po.get("cursor") if isinstance(po, dict) else None
    return c if isinstance(c, dict) else {}


def request_id(m: dict) -> str | None:
    """requestId of a real prompt; None for any other message."""
    c = _cursor_options(m)
    r = c.get("requestId")
    return r if m.get("role") == "user" and isinstance(r, str) and r and not c.get("isSummary") \
        else None


def _tool(name: str) -> str:
    """by_tool key. An MCP tool is `mcp_<server>_<tool>` and a server label is free text, so it
    counts as `mcp`; a name that is not a plain identifier counts as `other`."""
    if name.startswith("mcp") or name == "CallMcpTool":
        return "mcp"
    return name if _IDENT.fullmatch(name) else "other"


def prompt_ts(text: str, created: str | None) -> str | None:
    """The prompt's embedded `<timestamp>Weekday, Mon D, YYYY, H:MM AM (UTC+N)</timestamp>`
    (minute precision), else the chat's createdAt."""
    m = _TS.search(text)
    if not m:
        return created
    mon, day, year, hh, mm, ap, sign, oh, om = m.groups()
    try:
        d = datetime.strptime(f"{mon} {day} {year} {hh}:{mm} {ap}", "%b %d %Y %I:%M %p")
        off = timedelta(hours=int(oh or 0), minutes=int(om or 0)) * (-1 if sign == "-" else 1)
        return d.replace(tzinfo=timezone(off)).isoformat()
    except ValueError:
        return created


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]                      # IndexError on a truncated blob
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def _field1_ids(b: bytes) -> tuple[str, ...]:
    """The 32-byte field-1 entries of a top-level protobuf message, in order."""
    out, i = [], 0
    while i < len(b):
        tag, i = _varint(b, i)
        wt = tag & 7
        if wt == 0:
            _, i = _varint(b, i)
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        elif wt == 2:
            n, i = _varint(b, i)
            if tag >> 3 == 1 and n == 32:
                out.append(b[i:i + n].hex())
            i += n
        else:
            raise ValueError(wt)
    return tuple(out)


def root_lists(con: sqlite3.Connection) -> list[tuple[str, ...]]:
    """Every root list that is no other list's prefix. A root starts with its first field-1
    entry (measured on every root of real stores)."""
    seqs = set()
    for (data,) in con.execute("SELECT data FROM blobs WHERE substr(data, 1, 2) = X'0A20'"):
        try:
            ids = _field1_ids(bytes(data))
        except (ValueError, IndexError):
            continue
        if ids:
            seqs.add(ids)
    srt = sorted(seqs)
    # in sorted order a list that prefixes another one prefixes the very next one
    return [a for a, b in zip(srt, srt[1:] + [()]) if b[:len(a)] != a]


def messages(con: sqlite3.Connection, ids: set[str]) -> dict[str, dict]:
    """The listed messages, plus every prompt (a prompt can be in no surviving root)."""
    out: dict[str, dict] = {}

    def take(rows) -> None:
        for k, v in rows:
            try:
                m = json.loads(v)
            except (ValueError, TypeError):
                continue
            if isinstance(m, dict):
                out[k] = m
    q = sorted(ids)
    for j in range(0, len(q), _BATCH):
        part = q[j:j + _BATCH]
        take(con.execute(f"SELECT id, data FROM blobs WHERE id IN ({','.join('?' * len(part))})",
                         part))
    take(con.execute("SELECT id, data FROM blobs WHERE substr(data, 1, 1) = X'7B' "
                     "AND instr(data, '\"requestId\"') > 0"))
    return out


@dataclass
class _Turn:
    """One requestId's turn. A prompt no surviving root lists has no tool messages to read and
    emits no turn or link row (`listed` False)."""
    msg: dict
    closed: bool = False
    listed: bool = True
    agg: TurnAgg = field(default_factory=blank_agg)
    acts: list = field(default_factory=list)         # (kind, seed, attrs) rows, emitted at close
    cids: set = field(default_factory=set)           # tool calls counted, by toolCallId
    more: dict = field(default_factory=dict)         # follow-up prompt text -> (blob id, message)
    link: dict = field(default_factory=dict)


class _Activity:
    """One chat's activity extractor. Rows wait on the _Turn: it is emitted only once closed."""

    def __init__(self, keys: RepoKeys) -> None:
        self.keys = keys
        self.calls: dict = {}     # toolCallId -> args, across the chat's lists
        self.errors = 0           # messages whose extraction raised (skipped)

    def record(self, message: dict, turn: _Turn) -> None:
        """Best effort: a message whose extraction raises is counted and skipped, so activity
        never blocks the human and session rows."""
        try:
            self._record(message, turn)
        except Exception:
            self.errors += 1

    def _record(self, message: dict, turn: _Turn) -> None:
        """One assistant or tool message: tool aggregates, commits, rejections, link."""
        link = Link(self.keys, turn.link)
        content = [b for b in message["content"] if isinstance(b, dict)] \
            if isinstance(message.get("content"), list) else []
        if message.get("role") == "assistant":
            link.text(text_of(content))
            for b in content:
                if b.get("type") == "tool-call" and isinstance(b.get("toolName"), str):
                    cid = b.get("toolCallId")
                    self.calls[cid] = b.get("args")
                    if isinstance(cid, str) and cid in turn.cids:
                        continue        # a rewritten blob that a later root lists again
                    turn.cids.add(cid)
                    add_call(turn.agg, _tool(b["toolName"]), b.get("args"))
            return
        if message.get("role") != "tool":
            return
        hl = _cursor_options(message).get("highLevelToolCallResult")
        err = isinstance(hl, dict) and hl.get("isError") is True
        turn.agg["errors"] += err
        for b in content:
            if b.get("type") != "tool-result":
                continue
            res, cid = b.get("result"), b.get("toolCallId")
            res = res if isinstance(res, str) else ""
            link.text(res)
            if b.get("toolName") == "SwitchMode" \
                    and res.startswith("Mode switch was rejected by the user"):
                turn.acts.append(("rejection", str(cid), {}))
            elif b.get("toolName") == "Shell":
                self._shell_result(b, res, cid, err, turn, link)

    def _shell_result(self, b: dict, res: str, cid: object, err: bool, turn: _Turn,
                      link: Link) -> None:
        turn.agg["hooks"] += res.startswith("Rejected: Hook blocked")
        args = self.calls.get(cid)
        cmd = args.get("command") if isinstance(args, dict) else None
        wd = args.get("working_directory") if isinstance(args, dict) else None
        link.place(3, self.keys.of_cwd(wd))
        hit = commit_call(cmd) if isinstance(cmd, str) else None
        if not hit:
            return
        ex = EXIT_LINE.match(res)       # "Exit code: N\n\nCommand output:\n..." when it ran
        output = res[ex.end():] if ex else res
        found = commit_shas(output, err or not ex or ex[1] != "0", hit[1])
        for sha, attrs in commit_rows(link, wd, commit_dir(cmd), output, found, hit[0]):
            turn.acts.append(("commit", f"{cid}|{sha}", attrs))


def _read_turns(lists: list[tuple[str, ...]], msgs: dict[str, dict], done: set[str], idle: bool,
                act: _Activity) -> dict[str, _Turn]:
    """Every requestId's turn, its activity read unless the turn was already sent."""
    turns: dict[str, _Turn] = {}
    seen: set[str] = set()
    for seq in lists:
        rid = None
        for mid in seq:
            m = msgs.get(mid)
            if m is None:
                continue
            r = request_id(m)
            if r:
                if rid and rid != r:
                    turns[rid].closed = True
                rid = r
                turn = turns.setdefault(r, _Turn(m))
                # A prompt sent while the turn runs reuses its requestId. A blob with the first
                # prompt's text is a rewrite of it, not a new prompt.
                # shortcut: "first" is the first one seen in list order, so a follow-up that sits
                # only in a list sorting before the original's swaps their ids; upgrade if
                # backfill and live steer ids ever disagree.
                text = text_of(m.get("content"))
                if m is not turn.msg and text != text_of(turn.msg.get("content")):
                    turn.more.setdefault(text, (mid, m))
            elif rid and rid not in done and mid not in seen:
                seen.add(mid)
                act.record(m, turns[rid])
    for m in msgs.values():             # a prompt no surviving root lists
        r = request_id(m)
        if r and r not in turns:
            # its history was dropped, or it is newer than the root that will list it: wait
            # until the chat is idle so a still-running turn is not closed early
            turns[r] = _Turn(m, closed=idle, listed=False)
    return turns


def _turn_rows(rid: str, turn: _Turn, sid: str, human: bool, origin: dict, created: str | None,
               keys: RepoKeys) -> list:
    """The rows of one closed turn."""
    rows: list = []
    ts = prompt_ts(text_of(turn.msg.get("content")), created)

    def emit(kind: str, seed: str, attrs: dict, at: str | None = ts) -> None:
        rows.append(ActivityEvent(event_id=make_event_id(NAME, kind, f"{sid}|{seed}"), ts=at,
                                  agent=NAME, kind=kind, session_id=sid, turn_id=rid, attrs=attrs))
    # the first prompt keeps the (sid, rid) ids; a follow-up adds its blob id to them
    prompts = [((), turn.msg)] + [((mid,), pm) for mid, pm in turn.more.values()]
    for extra, pm in prompts if human else ():
        ptext = text_of(pm.get("content"))
        pts = prompt_ts(ptext, created)
        q = USER_QUERY.search(ptext)
        words = (q[1] if q else ptext).strip()
        c = pm.get("content")
        images = sum(isinstance(b, dict) and b.get("type") == "image" for b in c) \
            if isinstance(c, list) else 0
        if turn.listed:
            Link(keys, turn.link).text(ptext)
        rows.append(UsageEvent(
            event_id=make_event_id(NAME, "human", sid, rid, *extra), ts=pts, agent=NAME,
            model=None, provider=None, kind="human",
            session=EventSession(sid, None, "interactive", rid)))
        emit("steer", "|".join((rid, *extra)), {"via": "typed", "words": len(words.split()),
                                                "chars": len(words), "images": images}, pts)
    for kind, seed, attrs in turn.acts:
        emit(kind, seed, attrs)
    if turn.listed:
        emit("turn", rid, {**turn_attrs(turn.agg), "hook_blocks": turn.agg["hooks"], **origin})
        emit("link", rid, Link(keys, turn.link).attrs(origin["run_origin"]))
    return rows


def _chat(path: str, con: sqlite3.Connection, meta: dict, cur: dict, idle: bool,
          keys: RepoKeys) -> tuple[list, int]:
    """Rows of every turn that closed since the last scan; updates `cur` (a copy) in place."""
    sid = meta.get("agentId") if isinstance(meta.get("agentId"), str) and meta.get("agentId") \
        else os.path.basename(os.path.dirname(path))
    # A subagent chat's prompts are its parent agent talking, not a person. Its parent is in
    # subagentInfo.parentAgentId, but it has no usage or human row to carry a session row.
    info = meta.get("subagentInfo")
    human = not info
    parent = info.get("parentAgentId") if isinstance(info, dict) else None
    # No session row can carry a subagent chat's origin (it has no usage row), so its turn does.
    origin = {"run_origin": "interactive"} if human else {
        "run_origin": "subagent",
        "parent_session_id": parent if isinstance(parent, str) and _UUID.fullmatch(parent) else None}
    created = epoch_ms_to_iso(meta.get("createdAt"))
    lists = root_lists(con)
    msgs = messages(con, {i for seq in lists for i in seq})
    done = set(cur.get("done", ()))
    act = _Activity(keys)
    turns = _read_turns(lists, msgs, done, idle, act)
    events: list = []
    for rid, turn in turns.items():
        if rid in done or not (turn.closed or idle):
            continue
        done.add(rid)
        events += _turn_rows(rid, turn, sid, human, origin, created, keys)
    cur["done"] = sorted(done)
    cur["open"] = sum(r not in done for r in turns)
    return events, act.errors


def chat_meta(con: sqlite3.Connection) -> dict | None:
    row = con.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
    try:
        m = json.loads(bytes.fromhex(row[0]))
    except (TypeError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def _mtime(path: str) -> float:
    return max((os.stat(p).st_mtime for p in (path, path + "-wal") if os.path.exists(p)),
               default=0.0)


def _sdk(path: str, have: set[str]) -> list[ActivityEvent] | None:
    """One turn row per run that ended and was not sent yet; None if the store cannot be read.
    Queued and running runs wait: a row is insert-only, so one stored as running would never
    become finished."""
    con = connect_ro(path)
    if con is None:
        return None
    try:
        rows = con.execute("SELECT run_id, agent_id, status, created_at, started_at, finished_at "
                           "FROM runs").fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()
    out = []
    for run_id, agent_id, status, created, started, finished in rows:
        st = _ENDED.get(status)       # error_code and result are free text: never read
        if not st or not isinstance(run_id, str) or run_id in have:
            continue
        out.append(ActivityEvent(
            event_id=make_event_id(NAME, "turn", f"{agent_id}|{run_id}"), ts=started or created,
            agent=NAME, kind="turn", session_id=agent_id, turn_id=run_id,
            attrs={"status": st, "duration_ms": ms_between(started, finished),
                   "run_origin": "automated"}))
    return out


def describe(cfg: Config, wm: dict) -> str:
    if "chats" in wm:
        return f"{len(wm['chats'])} chats, {len(wm.get('sdk', {}))} SDK stores tracked"
    return describe_files(cfg, wm)


def collect(cfg: Config, wm: dict) -> Iterator[Batch]:
    chats: dict = wm.setdefault("chats", {})
    sdk: dict = wm.setdefault("sdk", {})
    keys = RepoKeys(cfg.repo_key_salt)
    skipped: Counter = Counter()
    unreadable = 0
    for path in chat_stores(cfg.home):
        prev = chats.get(path) or {}
        con = connect_ro(path)
        if con is None:
            unreadable += 1
            continue
        try:
            meta = chat_meta(con)
            if meta is None or (meta.get("latestRootBlobId") == prev.get("root")
                                and not prev.get("open")):
                continue
            # A deep copy: only advance_watermark() may publish this chat's state into the watermark.
            cur = copy.deepcopy(prev)
            cur["root"] = meta.get("latestRootBlobId")
            events, errs = _chat(path, con, meta, cur, time.time() - _mtime(path) >= _IDLE_S,
                                 keys)
        except sqlite3.DatabaseError:   # locked or corrupt: the next scan retries
            unreadable += 1
            continue
        finally:
            con.close()
        skipped["activity"] += errs
        if not events and cur == prev:
            continue

        def advance_watermark(p: str = path, c: dict = cur) -> None:
            chats[p] = c

        yield events, advance_watermark
    for path in sorted(glob.glob(str(cfg.home / ".cursor" / "projects" / "*" / "sdk-agent-store"
                                     / "*" / "index.db"))):
        have = set(sdk.get(path, {}).get("runs", ()))
        events = _sdk(path, have)
        if events is None:
            unreadable += 1
            continue
        if not events:
            continue

        def advance_sdk_watermark(p: str = path,
                                  runs: list = sorted(have | {e.turn_id for e in events})) -> None:
            sdk[p] = {"runs": runs}

        yield events, advance_sdk_watermark
    warn_skipped(NAME, skipped, unaffected="human")
    if unreadable:
        raise CollectorUnavailable(f"{NAME}: {unreadable} store(s) locked or corrupt, left for the "
                                   "next scan")
