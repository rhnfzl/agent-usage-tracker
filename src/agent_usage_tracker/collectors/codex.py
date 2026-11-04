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

import copy
import glob
import os
from collections import Counter
from typing import Iterator

from ..config import Config
from ..event import Batch, ActivityEvent, EventSession, UsageEvent, make_event_id
from ..repo_keys import RepoKeys, repo_identity
from .activity_common import (Link, TurnAgg, add_call, blank_agg, commit_rows, first_record,
                              ms_between, text_of, turn_attrs)
from .commits import commit_call, commit_dir, commit_shas
from ._util import (parse_record, project_name, read_new_lines, safe_int, same_file, str_or_none,
                    warn_skipped)

NAME = "codex"
# The watermark's format. Older state is upgraded once by _upgrade_state; delete a step from there
# once no machine can still hold state that old.
_STATE_V = 1


def _fresh_state(dev: int | None = None, ino: int | None = None) -> dict:
    return {"off": 0, "dev": dev, "ino": ino, "model": None, "provider": "openai",
            "session_id": None, "cwd": None, "tc_ord": 0, "um_ord": 0}


# session_meta.source values that mean the thread was started by software, not by a person:
# 'exec' is a scripted `codex exec`, 'mcp' is a programmatic MCP call, and a dict carrying
# 'subagent' is a thread another agent spawned (whose opening prompt is the parent agent
# talking, and which would double-count the human prompt that started the parent anyway).
_PROGRAM_SOURCES = {"exec", "mcp"}


def run_origin(p: dict) -> str:
    """run_origin from the thread's FIRST session_meta. Structural signals only: originator,
    source and thread_source are recorded by codex itself.

    Prompts of a subagent or automated thread are not human turns. Without this every
    user_message counts as 'a prompt you sent', which on a machine that runs scheduled
    automations, `codex exec` harnesses and agent-spawned subagents overstates human input
    several-fold, and since the showcase reports agent_turns_per_human_turn, an inflated
    denominator silently understates leverage."""
    src, orig = p.get("source"), p.get("originator")
    if isinstance(src, dict):
        return "subagent" if "subagent" in src else "unknown"
    if (src in _PROGRAM_SOURCES or p.get("thread_source") == "automation"
            # another coding agent driving codex through its own integration (rescue/review)
            or (isinstance(orig, str) and orig.strip().lower() == "claude code")):
        return "automated"
    return "interactive" if src in ("vscode", "cli") else "unknown"


def is_scheduled_automation(message: object) -> bool:
    """True for the preamble older Codex Desktop files put on a scheduled automation run (newer
    files flag it structurally with thread_source). Both markers are required so an ordinary
    message that merely opens with the word 'Automation' is not swallowed."""
    if not isinstance(message, str):
        return False
    head = message.lstrip()[:200]
    return head.startswith("Automation:") and "Automation ID:" in head


def _set_meta(cur: dict, p: dict, keys: RepoKeys) -> None:
    """Identity, origin and cwd from the FIRST session_meta only: a forked file repeats its
    parent's meta on a later line. Its git block gives the link fields every turn carries: the
    repo from the cwd, else the remote URL (hashed, never kept), and the branch, as keys."""
    cur["meta"] = {"id": str_or_none(p.get("id")), "parent": str_or_none(p.get("parent_thread_id")),
                   "origin": run_origin(p)}
    cur["cwd"] = p.get("cwd") or cur.get("cwd")
    cur["meta"]["link"] = _meta_link(p, keys)


def _meta_link(p: dict, keys: RepoKeys) -> dict:
    g = p.get("git") if isinstance(p.get("git"), dict) else {}
    rk = keys.of_cwd(p.get("cwd"))
    if not rk and isinstance(g.get("repository_url"), str):
        try:
            rk = keys.of(repo_identity(g["repository_url"]))
        except ValueError:
            pass
    # shortcut: a later turn_context cwd in another repo keeps the first meta's repo; take the
    # turn's cwd if Codex threads start moving between repos
    link = Link(keys, {})
    link.place(2, rk, g.get("branch"))
    return link.s


def _session(cur: dict, path: str) -> EventSession:
    m = cur.get("meta") or {}
    return EventSession(m.get("id") or cur.get("session_id") or os.path.basename(path),
                        m.get("parent"), m.get("origin") or "unknown", cur.get("turn"))


def script_of(cmd: object) -> str | None:
    """The shell script of a CommandExecution: codex runs `[shell, -c|-lc, script]`."""
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
        return cmd[2] if len(cmd) == 3 and cmd[1] in ("-c", "-lc") else " ".join(cmd)
    return None


def prompt_of(t: object, p: dict, has_um: bool) -> tuple[str, int] | None:
    """(text, images) when the record is a prompt: a user_message, or a UserMessage item in a
    file that has no user_message (older files carry both for one prompt)."""
    if t != "event_msg":
        return None
    if p.get("type") == "user_message":
        imgs = p.get("local_images")
        msg = p.get("message")
        return (msg if isinstance(msg, str) else ""), len(imgs) if isinstance(imgs, list) else 0
    it = p.get("item")
    if p.get("type") == "item_completed" and isinstance(it, dict) \
            and it.get("type") == "UserMessage" and not has_um:
        c = it.get("content")
        blocks = [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []
        return text_of(c), sum(b.get("type") != "text" for b in blocks)
    return None


class _Activity:
    """Per-file activity extractor over `cur`, the private copy of the file's watermark entry."""

    def __init__(self, cur: dict, path: str, keys: RepoKeys) -> None:
        self.cur, self.path, self.keys = cur, path, keys
        self.carried: dict = cur.setdefault("act", {})
        self.events: list[ActivityEvent] = []
        self.errors = 0                       # records whose extraction raised (skipped)

    def _emit(self, kind: str, ts: object, seed: str, sess: EventSession, attrs: dict) -> None:
        self.events.append(ActivityEvent(
            event_id=make_event_id(NAME, kind, f"{sess.session_id}|{seed}"), ts=str_or_none(ts),
            agent=NAME, kind=kind, session_id=sess.session_id, turn_id=sess.turn_id, attrs=attrs))

    def record(self, record: dict) -> None:
        """Best effort: a record whose extraction raises is counted and skipped, so activity never
        blocks the usage and session rows of the same chunk."""
        try:
            self._record(record)
        except Exception:
            self.errors += 1

    def _record(self, record: dict) -> None:
        """Non-prompt activity of one record: tool aggregates, interrupt, turn, commit, compaction,
        link."""
        record_type, payload = record.get("type"), record.get("payload")
        ts = record.get("timestamp")
        sess = _session(self.cur, self.path)
        link = Link(self.keys, self.carried.setdefault("link", {}))
        agg: TurnAgg = self.carried.setdefault("agg", blank_agg())
        if record_type == "response_item":
            link.text(payload.get("output"))           # a tool call's output
            self._tool_call(payload, agg)
            return
        if record_type != "event_msg":
            return
        kind, item = payload.get("type"), payload.get("item")
        turn_seed = str(payload.get("turn_id") or ts)
        if kind in ("task_complete", "turn_aborted"):
            link.merge((self.cur.get("meta") or {}).get("link") or {})
            self._emit("link", ts, turn_seed, sess, link.attrs(sess.run_origin))
            link.reset()
        if kind == "agent_message":
            link.text(payload.get("message"))
        elif kind == "task_started":
            self.carried["agg"], self.carried["start"] = blank_agg(), ts
        elif kind == "turn_aborted" and payload.get("reason") == "interrupted":
            self.carried["intr"] = True
            self._emit("interrupt", ts, turn_seed, sess, {})
        elif kind == "task_complete":
            d = payload.get("duration_ms")
            dur = d if isinstance(d, int) and not isinstance(d, bool) \
                else ms_between(self.carried.get("start"), ts)
            self._emit("turn", ts, turn_seed, sess, {"duration_ms": dur, **turn_attrs(agg)})
            self.carried["agg"] = blank_agg()
            self.carried.pop("start", None)
        elif kind == "item_completed" and isinstance(item, dict):
            self._item(item, ts, sess, link, agg)

    @staticmethod
    def _tool_call(payload: dict, agg: TurnAgg) -> None:
        name = payload.get("name")
        if payload.get("type") in ("function_call", "custom_tool_call") and isinstance(name, str):
            # an MCP namespace is a server label (free text); other namespaces are codex's own.
            # Older CLI files name MCP tools `<server>__<tool>` with no namespace; codex's own
            # tool names never contain `__`.
            ns = payload.get("namespace")
            mcp = (isinstance(ns, str) and ns.startswith("mcp__")) or "__" in name
            if mcp:     # Desktop may log the same call again as an McpToolCall item
                agg.setdefault("mcp_ids", []).append(payload.get("call_id"))
            add_call(agg, "mcp" if mcp else name, payload.get("arguments", payload.get("input")))

    def _item(self, item: dict, ts: object, sess: EventSession, link: Link, agg: TurnAgg) -> None:
        typ = item.get("type")
        if typ == "CommandExecution":
            code = item.get("exit_code")
            err = isinstance(code, int) and code != 0
            agg["errors"] += err
            script = script_of(item.get("command"))
            call = commit_call(script) if script else None
            out_text = item.get("aggregated_output")
            out_text = out_text if isinstance(out_text, str) else ""
            link.text(out_text)
            if call:
                found = commit_shas(out_text, err, call[1])
                for sha, attrs in commit_rows(link, item.get("cwd"), commit_dir(script), out_text,
                                              found, call[0]):
                    self._emit("commit", ts, f"{item.get('id')}|{sha}", sess, attrs)
        elif typ == "McpToolCall":
            # Desktop logs most MCP calls only as this item; one that pairs (same id) with an
            # MCP function_call of the turn was already counted
            if item.get("id") not in agg.get("mcp_ids", ()):
                add_call(agg, "mcp", item.get("arguments"))
            agg["errors"] += item.get("status") == "failed"
        elif typ == "ContextCompaction":
            self._emit("compaction", ts, str(item.get("id") or ts), sess, {})


def _upgrade_state(wm: dict, keys: RepoKeys) -> None:
    """Bring state saved by an older version to the current shape, once. An entry from before
    um_ord existed is dropped, so the file is re-read from 0 and user_message ordinals (and thus
    their event_ids) stay consistent if it is later reset and re-read; that is safe because
    event_id is content-deterministic and re-emitted rows dedup at the sink. An entry from before
    identity (or its link fields) was kept takes them from the file's first record."""
    if wm.get("state_v") == _STATE_V:
        return
    files = wm.setdefault("files", {})
    for path, entry in list(files.items()):
        if "um_ord" not in entry:
            del files[path]
        elif safe_int(entry.get("off")) and "link" not in (entry.get("meta") or {}):
            first = first_record(path)
            if first and first.get("type") == "session_meta" \
                    and isinstance(first.get("payload"), dict):
                if entry.get("meta"):           # identity stays as it was first read
                    entry["meta"]["link"] = _meta_link(first["payload"], keys)
                else:
                    _set_meta(entry, first["payload"], keys)
    wm["state_v"] = _STATE_V


def _parsed_records(lines: list[bytes], off: int, cur: dict, skipped: Counter) -> list[tuple]:
    """(record, type, payload, copied) of each new line that has a payload. A forked child's file
    opens with a copy of its parent's history: lines before the first meta's
    subagent_history_start_ordinal (0-based). `skip` is how many copied lines are still unread.
    Only a fork: a spawned child can carry an ordinal too, with no copy."""
    recs = []
    skip = cur.pop("skip", 0)
    for i, raw in enumerate(lines):
        copied, skip = skip > 0, max(skip - 1, 0)
        o = parse_record(raw)
        if o is None:
            skipped["malformed"] += 1
            continue
        p = o.get("payload")
        if isinstance(p, dict):
            if not off and not i and o.get("type") == "session_meta" and p.get("forked_from_id"):
                skip = max(safe_int(p.get("subagent_history_start_ordinal")) - 1, 0)
                copied = True
            recs.append((o, o.get("type"), p, copied))
    if skip:
        cur["skip"] = skip
    return recs


def _note_context(t: object, p: dict, copied: bool, cur: dict, keys: RepoKeys) -> None:
    """Streaming state from a record: session identity, model, cwd, the current turn."""
    if t == "session_meta":
        # the usage event_id seed keeps its old rule (payload.session_id, any meta line),
        # so stored usage rows never change id; session rows use the thread id instead
        cur["session_id"] = p.get("session_id") or p.get("id") or cur["session_id"]
        cur["provider"] = p.get("model_provider") or cur["provider"]
        if "meta" not in cur:
            _set_meta(cur, p, keys)
    elif t == "turn_context":
        if p.get("model"):
            cur["model"] = p.get("model")
        if p.get("cwd"):
            cur["cwd"] = p.get("cwd")
    elif t == "event_msg" and p.get("type") == "task_started" and not copied:
        cur["turn"] = str_or_none(p.get("turn_id")) or cur.get("turn")


def _token_event(o: dict, p: dict, cur: dict, path: str) -> UsageEvent | None:
    """The usage row of a token_count record, None if it has no tokens. Raises on malformed counts."""
    info = p.get("info")
    last = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(last, dict):
        return None
    inp = int(last.get("input_tokens") or 0)
    cached = int(last.get("cached_input_tokens") or 0)
    out = int(last.get("output_tokens") or 0)
    rea = int(last.get("reasoning_output_tokens") or 0)
    total = int(last.get("total_tokens") or (inp + out))
    cur["tc_ord"] += 1
    # Known gap: some Desktop rows carry only total_tokens (input and output 0) and
    # no model; they are dropped here. See CONTRACT.md.
    if inp + out + rea == 0:
        return None
    ts = o.get("timestamp")
    sid = cur["session_id"] or os.path.basename(path)
    # content hash, not position; only fall back to the ordinal if ts is absent.
    tiebreak = "" if ts else cur["tc_ord"]
    return UsageEvent(
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
        project=project_name(cur["cwd"]),
        session=_session(cur, path),
    )


def _prompt_rows(o: dict, prompt: tuple[str, int], cur: dict, path: str, act: _Activity) -> list:
    """The human and steer rows of a prompt someone typed. Threads a program drives (subagents,
    `codex exec`, MCP calls, another agent's integration, scheduled automations) are not human
    turns, and get none."""
    after_intr = bool(act.carried.pop("intr", False))
    text, images = prompt
    Link(act.keys, act.carried.setdefault("link", {})).text(text)
    sess = _session(cur, path)
    if sess.run_origin in ("subagent", "automated") or is_scheduled_automation(text):
        return []
    # No tokens; a stable per-session ordinal makes the id deterministic so a
    # re-read (file reset) re-emits the same id and the sink dedups it. The ordinal
    # advances only for kept messages, which is fine: it just has to be stable for a
    # given file, and the screening above is deterministic from the same bytes.
    cur["um_ord"] += 1
    ts = o.get("timestamp")
    sid = cur["session_id"] or os.path.basename(path)
    return [
        UsageEvent(
            event_id=make_event_id(NAME, "human", sid, ts or "", cur["um_ord"]),
            ts=ts, agent=NAME, model=None, provider=None,
            kind="human", project=project_name(cur["cwd"]), session=sess,
        ),
        ActivityEvent(
            event_id=make_event_id(NAME, "steer", sid, ts or "", cur["um_ord"]),
            ts=str_or_none(ts), agent=NAME, kind="steer", session_id=sess.session_id,
            turn_id=sess.turn_id, attrs={"via": "typed", "words": len(text.split()),
                                         "chars": len(text), "images": images,
                                         "after_interrupt": after_intr}),
    ]


def _chunk_rows(path: str, recs: list[tuple], cur: dict, keys: RepoKeys, skipped: Counter) -> list:
    """All rows of one file's new records; updates `cur`, the private copy of its watermark entry."""
    # shortcut: judged per chunk (plus earlier chunks of the file), so an older-format turn
    # split exactly between its UserMessage item and its user_message counts twice; older
    # files are complete on disk and read in one chunk, upgrade if that changes.
    if any(t == "event_msg" and p.get("type") == "user_message" for _o, t, p, _c in recs):
        cur["has_um"] = True
    act = _Activity(cur, path, keys)
    events: list = []
    for o, t, p, copied in recs:
        _note_context(t, p, copied, cur, keys)
        if t == "event_msg" and p.get("type") == "token_count":
            try:
                event = _token_event(o, p, cur, path)
            except (ValueError, TypeError, AttributeError, OverflowError):
                skipped["malformed"] += 1
                continue
            if event:
                events.append(event)
            continue
        if copied:      # the parent's turns, not this thread's (usage above is unchanged)
            continue
        prompt = prompt_of(t, p, bool(cur.get("has_um")))
        if prompt is not None:
            events.extend(_prompt_rows(o, prompt, cur, path, act))
            continue
        act.record(o)
    skipped["activity"] += act.errors
    return events + act.events


def _trim_state(cur: dict) -> None:
    carried = cur["act"]
    agg = carried.get("agg")
    if agg is not None and not (agg["calls"] or agg["errors"]):
        del carried["agg"]
    if not carried.get("link"):
        carried.pop("link", None)
    if not any(carried.values()):
        cur.pop("act", None)


def collect(cfg: Config, wm: dict) -> Iterator[Batch]:
    base = cfg.home / ".codex" / "sessions"
    if not base.exists():
        return
    keys = RepoKeys(cfg.repo_key_salt)
    _upgrade_state(wm, keys)
    files: dict = wm["files"]
    skipped: Counter = Counter()

    for path in glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = files.get(path) or {}
        if not same_file(prev, st):
            prev = _fresh_state(st.st_dev, st.st_ino)
        off = safe_int(prev.get("off"))
        if st.st_size <= off:
            continue

        lines, new_off = read_new_lines(path, off)
        # A deep copy: only advance_watermark() may publish this chunk's state into the watermark.
        cur = copy.deepcopy(prev)
        recs = _parsed_records(lines, off, cur, skipped)
        events = _chunk_rows(path, recs, cur, keys, skipped)
        cur["off"] = new_off
        _trim_state(cur)

        def advance_watermark(p: str = path, c: dict = cur) -> None:
            files[p] = c

        yield events, advance_watermark
    warn_skipped(NAME, skipped)
