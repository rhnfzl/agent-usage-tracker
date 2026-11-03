"""Pi collector: ~/.pi/agent/sessions/**/*.jsonl  (pi.dev, @earendil-works/pi-coding-agent)

Cleanest of the four: each `message` record carries message.usage
{input, output, cacheRead, cacheWrite, reasoning, totalTokens, cost} plus message.model. Model
can also change mid-session via `model_change` records (`modelId`), so we track it as streaming
state. `compaction` records carry the summary call's own usage. Scoped to the sessions directory
so unrelated JSONL under ~/.pi cannot pollute totals. Subscription-safe.

The session header (line 1: id, timestamp, cwd, parentSession on a fork) rides in the file's
watermark entry, because a resumed scan never reads line 1 again. `project` is the last part of
the header cwd (the encoded directory name cannot be decoded: Pi uses dashes for both).
"""

from __future__ import annotations

import copy
import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Iterator

from ..config import Config
from ..event import ActivityEvent, EventSession, UsageEvent, make_event_id
from ._activity_common import (Link, RepoKeys, add_call, blank_agg, commit_call, commit_dir,
                               commit_repo, commit_shas, first_record, ms_between, text_of,
                               tool_key, turn_attrs)
from ._util import basename, read_new_lines, safe_int

NAME = "pi"
_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
_SKILL = re.compile(r'\s*<skill name="([\w.:-]+)"')
_ENDS = ("stop", "aborted", "length")   # an `error` followed by another assistant is a retry


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


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _uuid_in(path: str) -> str | None:
    """The session uuid in a Pi file name (`<ts>_<uuid>.jsonl`)."""
    m = _UUID.search(os.path.basename(path))
    return m.group(1) if m else None


def _header(o: dict, path: str) -> dict:
    """What the watermark keeps of line 1. A fork's parent is the header id of the file named in
    parentSession, else the uuid in that name; the path itself is never kept."""
    ps = _str(o.get("parentSession"))
    parent = None
    if ps:
        first = first_record(ps) if ps.endswith(".jsonl") and os.path.isfile(ps) else None
        parent = (_str(first.get("id")) if first and first.get("type") == "session" else None) \
            or _uuid_in(ps)
    return {"id": _str(o.get("id")) or _uuid_in(path) or Path(path).stem,
            "ts": o.get("timestamp"), "cwd": _str(o.get("cwd")), "parent": parent,
            "fork": bool(ps)}


class _Activity:
    """Per-file activity state machine; `a` is the carried state (already a private copy)."""

    def __init__(self, a: dict, sid: str, cwd: str | None, keys: RepoKeys) -> None:
        self.a, self.sid, self.cwd, self.keys = a, sid, cwd, keys
        self.events: list[ActivityEvent] = []

    def _emit(self, kind: str, ts: object, seed: str, turn: str | None, attrs: dict) -> None:
        self.events.append(ActivityEvent(
            event_id=make_event_id(NAME, kind, f"{self.sid}|{seed}"), ts=ts, agent=NAME,
            kind=kind, session_id=self.sid, turn_id=turn, attrs=attrs))

    def _close(self, end: object) -> None:
        t = self.a.pop("open")
        self._emit("turn", end, t["id"], t["id"], {
            "duration_ms": ms_between(t["ts"], end), "message_count": t["n"],
            **turn_attrs(self.a.get("agg") or blank_agg())})
        link = Link(self.keys, self.a.pop("link", {}))
        link.place(3, self.keys.of_cwd(self.cwd))
        self._emit("link", end, t["id"], t["id"], link.attrs("interactive"))
        self.a.pop("agg", None)
        self.a.pop("pend", None)       # a commit call unanswered at turn end never will be

    def record(self, o: dict, sess: EventSession) -> None:
        a, ts, eid = self.a, o.get("timestamp"), o.get("id")
        if o.get("type") == "compaction":
            tb = o.get("tokensBefore")
            self._emit("compaction", ts, str(eid), sess.turn_id,
                       {"tokens_before": tb} if isinstance(tb, int) else {})
            return
        m = o.get("message")
        if o.get("type") != "message" or not isinstance(m, dict):
            return
        role, content = m.get("role"), m.get("content")
        open_ = a.get("open")
        link = Link(self.keys, a.setdefault("link", {}))
        if role != "user":           # a prompt's text belongs to the turn it opens, below
            link.text(text_of(content))
        if role == "user":
            if open_:
                self._close(open_["last"])
            text = text_of(content)
            skill = _SKILL.match(text)
            via = "skill" if text.lstrip().startswith('<skill name="') else \
                "queued" if a.get("prev") == "toolResult" else "typed"
            self._emit("steer", ts, str(eid), sess.turn_id, {
                "via": via, "words": len(text.split()), "chars": len(text),
                "after_interrupt": bool(a.pop("intr", False))})
            if skill:
                self._emit("invoke", ts, str(eid), sess.turn_id, {"skill": tool_key(skill[1])})
            a["open"] = {"id": eid, "ts": ts, "n": 1, "last": ts}
            Link(self.keys, a.setdefault("link", {})).text(text)
            a["agg"] = blank_agg()
        elif role in ("assistant", "toolResult"):
            if open_:
                open_["n"] += 1
                open_["last"] = ts
            agg = a.setdefault("agg", blank_agg())
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            if role == "assistant":
                for b in blocks:
                    if b.get("type") == "toolCall" and isinstance(b.get("name"), str):
                        args = b.get("arguments")
                        add_call(agg, b["name"], args)
                        cmd = args.get("command") if isinstance(args, dict) else None
                        call = commit_call(cmd) if b["name"] == "bash" and isinstance(cmd, str) \
                            else None
                        if call and isinstance(b.get("id"), str):
                            a.setdefault("pend", {})[b["id"]] = [*call, commit_dir(cmd)]
                stop = m.get("stopReason")
                a["intr"] = stop == "aborted"
                if stop == "aborted":
                    self._emit("interrupt", ts, str(eid), sess.turn_id,
                               {"during_tool": a.get("prev") == "toolResult"})
                if stop in _ENDS and open_:
                    self._close(ts)
            else:
                err = m.get("isError") is True
                agg["errors"] += err
                tid = m.get("toolCallId")
                pend = a.get("pend") or {}
                if isinstance(tid, str) and tid in pend:
                    amend, before, *target = pend.pop(tid)     # older state has no target
                    found = commit_shas(text_of(content), err, bool(before))
                    key = commit_repo(self.keys, self.cwd, target[0] if target else None,
                                      None) if found else None
                    if found:
                        link.commit(key, text_of(content))
                    for sha in found:
                        self._emit("commit", ts, f"{tid}|{sha}", sess.turn_id,
                                   {"sha": sha, "repo_key": key, "amend": bool(amend)})
        a["prev"] = role


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list, Callable[[], None]]]:
    base = cfg.home / ".pi" / "agent" / "sessions"
    if not base.exists():
        return
    files: dict = wm.setdefault("files", {})
    keys = RepoKeys(cfg.repo_key_salt)
    act_errors = 0

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
        # A deep copy: only commit() may publish this chunk's state into the watermark.
        cur = copy.deepcopy(prev)
        if off and "hdr" not in cur:      # resumed from a state saved before the header was kept
            first = first_record(path)
            if first and first.get("type") == "session":
                cur["hdr"] = _header(first, path)
        hdr = cur.get("hdr") or _header({}, path)
        act = _Activity(cur.setdefault("act", {}), hdr["id"], hdr["cwd"], keys)
        events: list = []
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
            if t == "session":
                if "hdr" not in cur:
                    cur["hdr"] = hdr = _header(o, path)
                    act.sid, act.cwd = hdr["id"], hdr["cwd"]
                continue
            if t == "model_change":
                m = o.get("modelId") or o.get("model")
                if m:
                    cur["model"] = m
                if o.get("provider"):
                    cur["provider"] = o["provider"]
                continue
            # A fork copies the parent's entries (same ids and timestamps, all before its own
            # header): their usage ids dedupe, but session and activity rows belong to the parent.
            gap = ms_between(o.get("timestamp"), hdr["ts"]) if hdr["fork"] else None
            copied = gap is not None and gap > 0
            if not copied and t == "message" and msg.get("role") == "user":
                cur["turn"] = _str(o.get("id")) or cur.get("turn")
            sess = None if copied else EventSession(hdr["id"], hdr["parent"], "unknown",
                                                    cur.get("turn"))
            if not copied:
                try:                     # best effort: never blocks usage and session rows
                    act.record(o, sess)
                except Exception:
                    act_errors += 1
            project = basename(hdr["cwd"])
            if t == "message" and msg.get("role") == "user":
                # A human prompt: plain text, no usage block. Emit as a human turn.
                events.append(UsageEvent(
                    event_id=make_event_id(NAME, "human", o.get("id") or f"{path}:{o.get('timestamp')}"),
                    ts=o.get("timestamp"), agent=NAME, model=None, provider=None,
                    kind="human", project=project, session=sess,
                ))
                continue
            if t == "message":
                u, model, provider = msg.get("usage"), msg.get("model"), msg.get("provider")
            elif t == "compaction":      # the summary call's own usage; no model on the record
                u, model, provider = o.get("usage"), None, None
            else:
                continue
            if not isinstance(u, dict):
                continue
            try:
                inp = int(u.get("input") or 0)
                out = int(u.get("output") or 0)
                cr = int(u.get("cacheRead") or 0)
                cw = int(u.get("cacheWrite") or 0)
                total = int(u.get("totalTokens") or (inp + out + cr + cw))
                # a SUBSET of output: informational, never added to totals or cost
                rea = int(u["reasoning"]) if u.get("reasoning") is not None else None
            except (ValueError, TypeError, OverflowError):
                continue
            if total == 0:
                continue
            events.append(UsageEvent(
                event_id=make_event_id(NAME, o.get("id") or f"{path}:{o.get('timestamp')}"),
                ts=o.get("timestamp"),
                agent=NAME,
                model=model or cur["model"],
                provider=provider or cur.get("provider"),
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                reasoning_tokens=rea,
                total_tokens=total,
                est_cost_usd=_cost_total(u.get("cost")),
                project=project,
                session=sess,
            ))
        events.extend(act.events)
        cur["off"] = new_off
        # shortcut: the last open turn of a file is emitted only when a later scan reads its end
        # (or the next prompt); a session left on an error or a tool call keeps it open for good.
        if not cur["act"].get("link"):
            cur["act"].pop("link", None)
        if not cur["act"]:
            del cur["act"]

        def commit(p: str = path, c: dict = cur) -> None:
            files[p] = c

        yield events, commit
    if act_errors:
        print(f"aut: {NAME}: {act_errors} activity record(s) skipped (extraction failed); usage "
              "and session rows are unaffected.", file=sys.stderr)
