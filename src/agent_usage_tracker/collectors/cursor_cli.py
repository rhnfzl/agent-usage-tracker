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
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from ..config import Config
from ..event import ActivityEvent, EventSession, UsageEvent, make_event_id
from ._activity_common import (Link, RepoKeys, add_call, blank_agg, commit_call, commit_dir,
                               commit_repo, commit_shas,
                               ms_between, text_of, turn_attrs)
from ._util import epoch_ms_to_iso
from .cursor import _connect_ro

NAME = "cursor_cli"
# The store has no turn-end record, so a turn ends at the next prompt or once the chat sits idle.
# shortcut: a tool call logged under a prompt more than _IDLE_S after the chat went quiet is lost
# (turn rows are insert-only); upgrade if Cursor ever logs a turn end in the store.
_IDLE_S = 3600
_ENDED = {"FINISHED": "finished", "CANCELLED": "cancelled", "ERROR": "error"}
_BATCH = 500
_TS = re.compile(r"<timestamp>\s*\w+,\s*([A-Za-z]{3})\w*\.?\s+(\d{1,2}),\s*(\d{4}),\s*(\d{1,2}):(\d\d)"
                 r"\s*([AP]M)\s*\(UTC(?:([+-])(\d{1,2})(?::(\d\d))?)?\)\s*</timestamp>")
_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.S)
_EXIT = re.compile(r"^Exit code: (-?\d+)\s*(?:Command output:\s*)?")
_IDENT = re.compile(r"[A-Za-z][A-Za-z_]*")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _po(m: dict) -> dict:
    po = m.get("providerOptions")
    c = po.get("cursor") if isinstance(po, dict) else None
    return c if isinstance(c, dict) else {}


def _rid(m: dict) -> str | None:
    """requestId of a real prompt; None for any other message."""
    c = _po(m)
    r = c.get("requestId")
    return r if m.get("role") == "user" and isinstance(r, str) and r and not c.get("isSummary") \
        else None


def _tool(name: str) -> str:
    """by_tool key. An MCP tool is `mcp_<server>_<tool>` and a server label is free text, so it
    counts as `mcp`; a name that is not a plain identifier counts as `other`."""
    if name.startswith("mcp") or name == "CallMcpTool":
        return "mcp"
    return name if _IDENT.fullmatch(name) else "other"


def _prompt_ts(text: str, created: str | None) -> str | None:
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


def _lists(con: sqlite3.Connection) -> list[tuple[str, ...]]:
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


def _messages(con: sqlite3.Connection, ids: set[str]) -> dict[str, dict]:
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


def _activity(m: dict, t: dict, calls: dict, keys: RepoKeys) -> None:
    """One assistant or tool message of turn `t`: tool aggregates, commits, rejections, link."""
    agg = t["agg"]
    link = Link(keys, t["link"])
    content = [b for b in m["content"] if isinstance(b, dict)] \
        if isinstance(m.get("content"), list) else []
    if m.get("role") == "assistant":
        link.text(text_of(content))
        for b in content:
            if b.get("type") == "tool-call" and isinstance(b.get("toolName"), str):
                cid = b.get("toolCallId")
                calls[cid] = b.get("args")
                if isinstance(cid, str) and cid in t["cids"]:
                    continue        # a rewritten blob that a later root lists again
                t["cids"].add(cid)
                add_call(agg, _tool(b["toolName"]), b.get("args"))
        return
    if m.get("role") != "tool":
        return
    hl = _po(m).get("highLevelToolCallResult")
    err = isinstance(hl, dict) and hl.get("isError") is True
    agg["errors"] += err
    for b in content:
        if b.get("type") != "tool-result":
            continue
        res, cid = b.get("result"), b.get("toolCallId")
        res = res if isinstance(res, str) else ""
        link.text(res)
        if b.get("toolName") == "SwitchMode" and res.startswith("Mode switch was rejected by the user"):
            t["acts"].append(("rejection", str(cid), {}))
        elif b.get("toolName") == "Shell":
            agg["hooks"] += res.startswith("Rejected: Hook blocked")
            args = calls.get(cid)
            cmd = args.get("command") if isinstance(args, dict) else None
            wd = args.get("working_directory") if isinstance(args, dict) else None
            link.place(3, keys.of_cwd(wd))
            hit = commit_call(cmd) if isinstance(cmd, str) else None
            if not hit:
                continue
            ex = _EXIT.match(res)       # "Exit code: N\n\nCommand output:\n..." when it ran
            found = commit_shas(res[ex.end():] if ex else res, err or not ex or ex[1] != "0",
                                hit[1])
            key = commit_repo(keys, wd, commit_dir(cmd), None) if found else None
            if found:
                link.commit(key, res[ex.end():] if ex else res)
            for sha in found:
                t["acts"].append(("commit", f"{cid}|{sha}",
                                  {"sha": sha, "repo_key": key, "amend": hit[0]}))


def _chat(path: str, con: sqlite3.Connection, meta: dict, cur: dict, idle: bool,
          keys: RepoKeys) -> tuple[list, int]:
    """Rows of every turn that closed since the last scan; updates `cur` (a copy) in place."""
    sid = meta.get("agentId") if isinstance(meta.get("agentId"), str) and meta.get("agentId") \
        else os.path.basename(os.path.dirname(path))
    # A subagent chat's prompts are its parent agent talking, not a person. Its parent is in
    # subagentInfo.parentAgentId, but it has no usage or human row to carry a session row.
    human = not meta.get("subagentInfo")
    sub = meta.get("subagentInfo") if isinstance(meta.get("subagentInfo"), dict) else {}
    parent = sub.get("parentAgentId")
    # No session row can carry a subagent chat's origin (it has no usage row), so its turn does.
    origin = {"run_origin": "interactive"} if human else {
        "run_origin": "subagent",
        "parent_session_id": parent if isinstance(parent, str) and _UUID.fullmatch(parent) else None}
    created = epoch_ms_to_iso(meta.get("createdAt"))
    lists = _lists(con)
    msgs = _messages(con, {i for seq in lists for i in seq})
    done = set(cur.get("done", ()))
    turns: dict[str, dict] = {}
    calls: dict = {}
    seen: set[str] = set()
    errors = 0
    for seq in lists:
        rid = None
        for mid in seq:
            m = msgs.get(mid)
            if m is None:
                continue
            r = _rid(m)
            if r:
                if rid and rid != r:
                    turns[rid]["closed"] = True
                rid = r
                t = turns.setdefault(r, {"msg": m, "agg": blank_agg(), "acts": [], "cids": set(),
                                         "more": {}, "closed": False, "listed": True, "link": {}})
                # A prompt sent while the turn runs reuses its requestId. A blob with the first
                # prompt's text is a rewrite of it, not a new prompt.
                # shortcut: "first" is the first one seen in list order, so a follow-up that sits
                # only in a list sorting before the original's swaps their ids; upgrade if
                # backfill and live steer ids ever disagree.
                text = text_of(m.get("content"))
                if m is not t["msg"] and text != text_of(t["msg"].get("content")):
                    t["more"].setdefault(text, (mid, m))
            elif rid and rid not in done and mid not in seen:
                seen.add(mid)
                # Best effort: a message whose extraction raises is counted and skipped, so
                # activity never blocks the human and session rows.
                try:
                    _activity(m, turns[rid], calls, keys)
                except Exception:
                    errors += 1
    for m in msgs.values():             # a prompt no surviving root lists
        r = _rid(m)
        if r and r not in turns:
            # its history was dropped, or it is newer than the root that will list it: wait
            # until the chat is idle so a still-running turn is not closed early
            turns[r] = {"msg": m, "agg": None, "acts": [], "more": {}, "closed": idle,
                        "listed": False}

    events: list = []
    for rid, t in turns.items():
        if rid in done or not (t["closed"] or idle):
            continue
        done.add(rid)
        text = text_of(t["msg"].get("content"))
        ts = _prompt_ts(text, created)

        def emit(kind: str, seed: str, attrs: dict, r: str = rid, ts: str | None = ts) -> None:
            events.append(ActivityEvent(event_id=make_event_id(NAME, kind, f"{sid}|{seed}"),
                                        ts=ts, agent=NAME, kind=kind, session_id=sid, turn_id=r,
                                        attrs=attrs))
        # the first prompt keeps the (sid, rid) ids; a follow-up adds its blob id to them
        prompts = [((), t["msg"])] + [((mid,), pm) for mid, pm in t["more"].values()]
        for extra, pm in prompts if human else ():
            ptext = text_of(pm.get("content"))
            pts = _prompt_ts(ptext, created)
            q = _QUERY.search(ptext)
            words = (q[1] if q else ptext).strip()
            c = pm.get("content")
            images = sum(isinstance(b, dict) and b.get("type") == "image" for b in c) \
                if isinstance(c, list) else 0
            if t["listed"]:
                Link(keys, t["link"]).text(ptext)
            events.append(UsageEvent(
                event_id=make_event_id(NAME, "human", sid, rid, *extra), ts=pts, agent=NAME,
                model=None, provider=None, kind="human",
                session=EventSession(sid, None, "interactive", rid)))
            emit("steer", "|".join((rid, *extra)), {"via": "typed", "words": len(words.split()),
                                                    "chars": len(words), "images": images},
                 ts=pts)
        for kind, seed, attrs in t["acts"]:
            emit(kind, seed, attrs)
        if t["listed"]:
            emit("turn", rid, {**turn_attrs(t["agg"]), "hook_blocks": t["agg"]["hooks"],
                               **origin})
            emit("link", rid, Link(keys, t["link"]).attrs(origin["run_origin"]))
    cur["done"] = sorted(done)
    cur["open"] = sum(r not in done for r in turns)
    return events, errors


def _meta(con: sqlite3.Connection) -> dict | None:
    row = con.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
    try:
        m = json.loads(bytes.fromhex(row[0]))
    except (TypeError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def _mtime(path: str) -> float:
    return max((os.stat(p).st_mtime for p in (path, path + "-wal") if os.path.exists(p)),
               default=0.0)


def _sdk(path: str, have: set[str]) -> list[ActivityEvent]:
    """One turn row per run that ended and was not sent yet. Queued and running runs wait: a
    row is insert-only, so one stored as running would never become finished."""
    con = _connect_ro(path)
    if con is None:
        return []
    try:
        rows = con.execute("SELECT run_id, agent_id, status, created_at, started_at, finished_at "
                           "FROM runs").fetchall()
    except sqlite3.DatabaseError:
        return []
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


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list, Callable[[], None]]]:
    chats: dict = wm.setdefault("chats", {})
    sdk: dict = wm.setdefault("sdk", {})
    keys = RepoKeys(cfg.repo_key_salt)
    act_errors = 0
    for path in sorted(glob.glob(str(cfg.home / ".cursor" / "chats" / "*" / "*" / "store.db"))):
        prev = chats.get(path) or {}
        con = _connect_ro(path)
        if con is None:
            continue
        try:
            meta = _meta(con)
            if meta is None or (meta.get("latestRootBlobId") == prev.get("root")
                                and not prev.get("open")):
                continue
            # A deep copy: only commit() may publish this chat's state into the watermark.
            cur = copy.deepcopy(prev)
            cur["root"] = meta.get("latestRootBlobId")
            events, errs = _chat(path, con, meta, cur, time.time() - _mtime(path) >= _IDLE_S,
                                 keys)
        except sqlite3.DatabaseError:   # locked or corrupt: the next scan retries
            continue
        finally:
            con.close()
        act_errors += errs
        if not events and cur == prev:
            continue

        def commit(p: str = path, c: dict = cur) -> None:
            chats[p] = c

        yield events, commit
    for path in sorted(glob.glob(str(cfg.home / ".cursor" / "projects" / "*" / "sdk-agent-store"
                                     / "*" / "index.db"))):
        have = set(sdk.get(path, {}).get("runs", ()))
        events = _sdk(path, have)
        if not events:
            continue

        def commit_sdk(p: str = path, runs: list = sorted(have | {e.turn_id for e in events})
                       ) -> None:
            sdk[p] = {"runs": runs}

        yield events, commit_sdk
    if act_errors:
        print(f"aut: {NAME}: {act_errors} activity record(s) skipped (extraction failed); human "
              "and session rows are unaffected.", file=sys.stderr)
