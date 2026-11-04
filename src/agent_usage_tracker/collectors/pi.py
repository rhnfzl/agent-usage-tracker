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

import glob
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from ..config import Config
from ..event import ActivityKind, Batch, ActivityEvent, EventSession, UsageEvent, make_event_id
from ..repo_keys import RepoKeys
from .activity_common import (Link, TurnAgg, add_call, blank_tool_tally, commit_rows, first_record, ms_between,
                              pad_pending, text_of, tool_key, turn_attrs)
from .commits import commit_call, commit_dir, commit_shas
from ._util import (changed_files, count_field, dict_or_empty, parse_record, project_name,
                    raise_if_unreadable, safe_int, str_or_none, warn_skipped)

NAME = "pi"
_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
SKILL = re.compile(r'\s*<skill name="([\w.:-]+)"')
_ENDS = ("stop", "aborted", "length")   # an `error` followed by another assistant is a retry
# The watermark's format. Older state is upgraded once by _upgrade_state; delete a step from there
# once no machine can still hold state that old.
_STATE_V = 1


def sessions_dir(home: Path) -> Path:
    return home / ".pi" / "agent" / "sessions"


def session_files(home: Path) -> list[str]:
    return glob.glob(str(sessions_dir(home) / "**" / "*.jsonl"), recursive=True)


def _cost_total(cost: object) -> float | None:
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


def uuid_in(path: str) -> str | None:
    """The session uuid in a Pi file name (`<ts>_<uuid>.jsonl`)."""
    m = _UUID.search(os.path.basename(path))
    return m.group(1) if m else None


def header(o: dict, path: str) -> dict[str, Any]:
    """What the watermark keeps of line 1. A fork's parent is the header id of the file named in
    parentSession, else the uuid in that name; the path itself is never kept."""
    ps = str_or_none(o.get("parentSession"))
    parent = None
    if ps:
        first = first_record(ps) if ps.endswith(".jsonl") and os.path.isfile(ps) else None
        parent = (str_or_none(first.get("id")) if first and first.get("type") == "session" else None) \
            or uuid_in(ps)
    return {"id": str_or_none(o.get("id")) or uuid_in(path) or Path(path).stem,
            "ts": o.get("timestamp"), "cwd": str_or_none(o.get("cwd")), "parent": parent,
            "fork": bool(ps)}


def is_fork_copy(o: dict, hdr: dict) -> bool:
    """A fork copies its parent's entries, with their ids and timestamps, all before its own header."""
    gap = ms_between(o.get("timestamp"), hdr["ts"]) if hdr["fork"] else None
    return gap is not None and gap > 0


class _Activity:
    """Per-file activity state machine; `carried` is the watermark's state (a private copy)."""

    def __init__(self, carried: dict, sid: str, cwd: str | None, keys: RepoKeys) -> None:
        self.carried, self.sid, self.cwd, self.keys = carried, sid, cwd, keys
        self.events: list[ActivityEvent] = []
        self.errors: Counter = Counter()      # records whose extraction raised (skipped)

    def _emit(self, kind: ActivityKind, ts: object, seed: str, turn: str | None, attrs: dict) -> None:
        self.events.append(ActivityEvent(
            event_id=make_event_id(NAME, kind, f"{self.sid}|{seed}"), ts=str_or_none(ts), agent=NAME,
            kind=kind, session_id=self.sid, turn_id=turn, attrs=attrs))

    def _close(self, end: object) -> None:
        turn = self.carried.pop("open")
        self._emit("turn", end, turn["id"], turn["id"], {
            "duration_ms": ms_between(turn["ts"], end), "message_count": turn["n"],
            **turn_attrs(self.carried.get("agg") or blank_tool_tally())})
        link = Link(self.keys, self.carried.pop("link", {}))
        link.place(3, self.keys.of_cwd(self.cwd))
        self._emit("link", end, turn["id"], turn["id"], link.attrs("interactive"))
        self.carried.pop("agg", None)
        self.carried.pop("pend", None)       # a commit call unanswered at turn end never will be

    def record(self, o: dict, sess: EventSession) -> None:
        """Best effort: a record whose extraction raises is counted and skipped."""
        try:
            self._record(o, sess)
        except Exception as exc:
            self.errors[f"activity:{type(exc).__name__}"] += 1

    def _record(self, o: dict, sess: EventSession) -> None:
        carried, ts, eid = self.carried, o.get("timestamp"), o.get("id")
        if o.get("type") == "compaction":
            tb = o.get("tokensBefore")
            self._emit("compaction", ts, str(eid), sess.turn_id,
                       {"tokens_before": tb} if isinstance(tb, int) else {})
            return
        m = o.get("message")
        if o.get("type") != "message" or not isinstance(m, dict):
            return
        role, content = m.get("role"), m.get("content")
        open_ = carried.get("open")
        link = Link(self.keys, carried.setdefault("link", {}))
        if role != "user":           # a prompt's text belongs to the turn it opens
            link.text(text_of(content))
        if role == "user":
            self._prompt(ts, eid, content, sess, open_)
        elif role in ("assistant", "toolResult"):
            if open_:
                open_["n"] += 1
                open_["last"] = ts
            agg = carried.setdefault("agg", blank_tool_tally())
            if role == "assistant":
                self._assistant(m, ts, eid, sess, agg, open_)
            else:
                self._tool_result(m, ts, sess, agg, link)
        carried["prev"] = role

    def _prompt(self, ts: object, eid: object, content: object, sess: EventSession,
                open_: dict | None) -> None:
        carried = self.carried
        if open_:
            self._close(open_["last"])
        text = text_of(content)
        skill = SKILL.match(text)
        via = "skill" if text.lstrip().startswith('<skill name="') else \
            "queued" if carried.get("prev") == "toolResult" else "typed"
        self._emit("steer", ts, str(eid), sess.turn_id, {
            "via": via, "words": len(text.split()), "chars": len(text),
            "after_interrupt": bool(carried.pop("intr", False))})
        if skill:
            self._emit("invoke", ts, str(eid), sess.turn_id, {"skill": tool_key(skill[1])})
        carried["open"] = {"id": eid, "ts": ts, "n": 1, "last": ts}
        Link(self.keys, carried.setdefault("link", {})).text(text)
        carried["agg"] = blank_tool_tally()

    def _assistant(self, m: dict, ts: object, eid: object, sess: EventSession, agg: TurnAgg,
                   open_: dict | None) -> None:
        carried, content = self.carried, m.get("content")
        for b in content if isinstance(content, list) else ():
            if isinstance(b, dict) and b.get("type") == "toolCall" and isinstance(b.get("name"), str):
                self._tool_call(b, agg)
        stop = m.get("stopReason")
        carried["intr"] = stop == "aborted"
        if stop == "aborted":
            self._emit("interrupt", ts, str(eid), sess.turn_id,
                       {"during_tool": carried.get("prev") == "toolResult"})
        if stop in _ENDS and open_:
            self._close(ts)

    def _tool_call(self, b: dict, agg: TurnAgg) -> None:
        args = b.get("arguments")
        add_call(agg, b["name"], args)
        cmd = args.get("command") if isinstance(args, dict) else None
        if b["name"] == "bash" and isinstance(cmd, str) and isinstance(b.get("id"), str) \
                and (call := commit_call(cmd)):
            self.carried.setdefault("pend", {})[b["id"]] = [*call, commit_dir(cmd)]

    def _tool_result(self, m: dict, ts: object, sess: EventSession, agg: TurnAgg,
                     link: Link) -> None:
        err = m.get("isError") is True
        agg["errors"] += err
        tid = m.get("toolCallId")
        pend = self.carried.get("pend") or {}
        if isinstance(tid, str) and tid in pend:
            amend, before, target = pend.pop(tid)
            text = text_of(m.get("content"))
            found = commit_shas(text, err, bool(before))
            for sha, attrs in commit_rows(link, self.cwd, target, text, found, bool(amend)):
                self._emit("commit", ts, f"{tid}|{sha}", sess.turn_id, attrs)


def _upgrade_state(wm: dict) -> None:
    """Bring state saved by an older version to the current shape, once: the header kept in the
    entry, and pending commit calls with their target. A reset would re-read the file instead."""
    if wm.get("state_v") == _STATE_V:
        return
    for path, entry in wm.setdefault("files", {}).items():
        if safe_int(entry.get("off")) and "hdr" not in entry:
            first = first_record(path)
            if first and first.get("type") == "session":
                entry["hdr"] = header(first, path)
        pad_pending(entry.get("act"))
    wm["state_v"] = _STATE_V


def _note_model_change(o: dict, cur: dict) -> None:
    cur["model"] = str_or_none(o.get("modelId")) or str_or_none(o.get("model")) or cur.get("model")
    provider = str_or_none(o.get("provider"))
    if provider:
        cur["provider"] = provider


def _record_id(o: dict, path: str) -> object:
    return o.get("id") or f"{path}:{o.get('timestamp')}"


def _usage_event(o: dict, msg: dict, cur: dict, path: str, project: str | None,
                 sess: EventSession | None) -> UsageEvent | None:
    """The usage row of a record, None if it has no tokens. Raises on malformed token counts."""
    if o.get("type") == "message":
        u, model, provider = msg.get("usage"), msg.get("model"), msg.get("provider")
    elif o.get("type") == "compaction":      # the summary call's own usage; no model on the record
        u, model, provider = o.get("usage"), None, None
    else:
        return None
    if not isinstance(u, dict):
        return None
    inp = count_field(u, "input")
    out = count_field(u, "output")
    cr = count_field(u, "cacheRead")
    cw = count_field(u, "cacheWrite")
    total = int(u.get("totalTokens") or (inp + out + cr + cw))
    # a SUBSET of output: informational, never added to totals or cost
    rea = int(u["reasoning"]) if u.get("reasoning") is not None else None
    if total == 0:
        return None
    return UsageEvent(
        event_id=make_event_id(NAME, _record_id(o, path)),
        ts=o.get("timestamp"),
        agent=NAME,
        model=str_or_none(model) or cur["model"],
        provider=str_or_none(provider) or cur.get("provider"),
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        reasoning_tokens=rea,
        total_tokens=total,
        est_cost_usd=_cost_total(u.get("cost")),
        project=project,
        session=sess,
    )


def _session_of(o: dict, hdr: dict, cur: dict, is_prompt: bool) -> EventSession | None:
    """The record's session, a prompt opening a new turn; None for a fork's copied entry, whose
    usage id dedupes but whose session and activity rows belong to the parent."""
    if is_fork_copy(o, hdr):
        return None
    if is_prompt:
        cur["turn"] = str_or_none(o.get("id")) or cur.get("turn")
    return EventSession(hdr["id"], hdr["parent"], "unknown", cur.get("turn"))


def _chunk_rows(path: str, lines: list[bytes], cur: dict, keys: RepoKeys,
                skipped: Counter) -> list:
    """All rows of one file's new lines; updates `cur`, the private copy of its watermark entry."""
    hdr = cur.get("hdr") or header({}, path)
    act = _Activity(cur.setdefault("act", {}), hdr["id"], hdr["cwd"], keys)
    events: list = []
    for raw in lines:
        o = parse_record(raw)
        if o is None:
            skipped["malformed"] += 1
            continue
        t = o.get("type")
        if t == "session":
            if "hdr" not in cur:
                cur["hdr"] = hdr = header(o, path)
                act.sid, act.cwd = hdr["id"], hdr["cwd"]
            continue
        if t == "model_change":
            _note_model_change(o, cur)
            continue
        msg = dict_or_empty(o.get("message"))
        is_prompt = t == "message" and msg.get("role") == "user"
        sess = _session_of(o, hdr, cur, is_prompt)
        if sess is not None:
            act.record(o, sess)
        project = project_name(hdr["cwd"])
        if is_prompt:
            # A human prompt: plain text, no usage block. Emit as a human turn.
            events.append(UsageEvent(
                event_id=make_event_id(NAME, "human", _record_id(o, path)),
                ts=o.get("timestamp"), agent=NAME, model=None, provider=None,
                kind="human", project=project, session=sess,
            ))
            continue
        try:
            event = _usage_event(o, msg, cur, path, project, sess)
        except (ValueError, TypeError, OverflowError):
            skipped["malformed"] += 1
            continue
        if event:
            events.append(event)
    skipped.update(act.errors)
    return events + act.events


def _trim_state(cur: dict) -> None:
    if not cur["act"].get("link"):
        cur["act"].pop("link", None)
    if not cur["act"]:
        del cur["act"]


def collect(cfg: Config, wm: dict) -> Iterator[Batch]:
    if not sessions_dir(cfg.home).exists():
        return
    _upgrade_state(wm)
    files: dict = wm["files"]
    keys = RepoKeys(cfg.repo_key_salt)
    skipped: Counter = Counter()

    def fresh(st: os.stat_result) -> dict:
        return {"off": 0, "dev": st.st_dev, "ino": st.st_ino, "model": None}

    for path, cur, _off, lines, new_off in changed_files(session_files(cfg.home), files, fresh, skipped):
        events = _chunk_rows(path, lines, cur, keys, skipped)
        cur["off"] = new_off
        # shortcut: the last open turn of a file is emitted only when a later scan reads its end
        # (or the next prompt); a session left on an error or a tool call keeps it open for good.
        _trim_state(cur)

        def advance_watermark(p: str = path, c: dict = cur) -> None:
            files[p] = c

        yield events, advance_watermark
    warn_skipped(NAME, skipped)
    raise_if_unreadable(NAME, skipped)
