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
import json
import os
import sys
from typing import Callable, Iterator

from ..config import Config
from ..event import ActivityEvent, EventSession, UsageEvent, make_event_id
from ..repo_keys import RepoKeys
from .activity_common import (Link, add_call, blank_agg, commit_call, commit_dir,
                              commit_repo, commit_shas, first_record, ms_between, repo_identity,
                              text_of, turn_attrs)
from ._util import basename, read_new_lines, safe_int

NAME = "codex"


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


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _set_meta(cur: dict, p: dict, keys: RepoKeys) -> None:
    """Identity, origin and cwd from the FIRST session_meta only: a forked file repeats its
    parent's meta on a later line. Its git block gives the link fields every turn carries: the
    repo from the cwd, else the remote URL (hashed, never kept), and the branch, as keys."""
    cur["meta"] = {"id": _str(p.get("id")), "parent": _str(p.get("parent_thread_id")),
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


def _activity(o: dict, t: object, p: dict, a: dict, sess: EventSession, keys: RepoKeys,
              out: list, cur: dict) -> None:
    """Non-prompt activity of one record: tool aggregates, interrupt, turn, commit, compaction,
    link."""
    ts = o.get("timestamp")
    link = Link(keys, a.setdefault("link", {}))

    def emit(kind: str, seed: str, attrs: dict) -> None:
        out.append(ActivityEvent(event_id=make_event_id(NAME, kind, f"{sess.session_id}|{seed}"),
                                 ts=ts, agent=NAME, kind=kind, session_id=sess.session_id,
                                 turn_id=sess.turn_id, attrs=attrs))

    agg = a.setdefault("agg", blank_agg())
    if t == "response_item":
        link.text(p.get("output"))           # a tool call's output
        name = p.get("name")
        if p.get("type") in ("function_call", "custom_tool_call") and isinstance(name, str):
            # an MCP namespace is a server label (free text); other namespaces are codex's own.
            # Older CLI files name MCP tools `<server>__<tool>` with no namespace; codex's own
            # tool names never contain `__`.
            ns = p.get("namespace")
            mcp = (isinstance(ns, str) and ns.startswith("mcp__")) or "__" in name
            if mcp:     # Desktop may log the same call again as an McpToolCall item
                agg.setdefault("mcp_ids", []).append(p.get("call_id"))
            add_call(agg, "mcp" if mcp else name, p.get("arguments", p.get("input")))
        return
    if t != "event_msg":
        return
    pt, it = p.get("type"), p.get("item")
    if pt in ("task_complete", "turn_aborted"):
        link.merge((cur.get("meta") or {}).get("link") or {})
        emit("link", str(p.get("turn_id") or ts), link.attrs(sess.run_origin))
        link.reset()
    if pt == "agent_message":
        link.text(p.get("message"))
    elif pt == "task_started":
        a["agg"], a["start"] = blank_agg(), ts
    elif pt == "turn_aborted" and p.get("reason") == "interrupted":
        a["intr"] = True
        emit("interrupt", str(p.get("turn_id") or ts), {})
    elif pt == "task_complete":
        d = p.get("duration_ms")
        dur = d if isinstance(d, int) and not isinstance(d, bool) else ms_between(a.get("start"), ts)
        emit("turn", str(p.get("turn_id") or ts), {"duration_ms": dur, **turn_attrs(agg)})
        a["agg"] = blank_agg()
        a.pop("start", None)
    elif pt == "item_completed" and isinstance(it, dict):
        typ = it.get("type")
        if typ == "CommandExecution":
            code = it.get("exit_code")
            err = isinstance(code, int) and code != 0
            agg["errors"] += err
            script = script_of(it.get("command"))
            call = commit_call(script) if script else None
            out_text = it.get("aggregated_output")
            out_text = out_text if isinstance(out_text, str) else ""
            link.text(out_text)
            if call:
                found = commit_shas(out_text, err, call[1])
                key = commit_repo(keys, it.get("cwd"), commit_dir(script), None) if found else None
                if found:
                    link.commit(key, out_text, found)
                for sha in found:
                    emit("commit", f"{it.get('id')}|{sha}",
                         {"sha": sha, "repo_key": key, "amend": call[0]})
        elif typ == "McpToolCall":
            # Desktop logs most MCP calls only as this item; one that pairs (same id) with an
            # MCP function_call of the turn was already counted
            if it.get("id") not in agg.get("mcp_ids", ()):
                add_call(agg, "mcp", it.get("arguments"))
            agg["errors"] += it.get("status") == "failed"
        elif typ == "ContextCompaction":
            emit("compaction", str(it.get("id") or ts), {})


def collect(cfg: Config, wm: dict) -> Iterator[tuple[list, Callable[[], None]]]:
    base = cfg.home / ".codex" / "sessions"
    if not base.exists():
        return
    files: dict = wm.setdefault("files", {})
    keys = RepoKeys(cfg.repo_key_salt)
    act_errors = 0

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
        # A deep copy: only commit() may publish this chunk's state into the watermark.
        cur = copy.deepcopy(prev)
        # resumed from a state saved before identity (or its link fields) was kept
        if off and "link" not in (cur.get("meta") or {}):
            first = first_record(path)
            if first and first.get("type") == "session_meta" \
                    and isinstance(first.get("payload"), dict):
                if cur.get("meta"):           # identity stays as it was first read
                    cur["meta"]["link"] = _meta_link(first["payload"], keys)
                else:
                    _set_meta(cur, first["payload"], keys)
        recs = []
        # A forked child's file opens with a copy of its parent's history: lines before the first
        # meta's subagent_history_start_ordinal (0-based). `skip` is how many copied lines are
        # still unread. Only a fork: a spawned child can carry an ordinal too, with no copy.
        skip = cur.pop("skip", 0)
        for i, raw in enumerate(lines):
            copied, skip = skip > 0, max(skip - 1, 0)
            try:
                o = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            p = o.get("payload") if isinstance(o, dict) else None
            if isinstance(p, dict):
                if not off and not i and o.get("type") == "session_meta" \
                        and p.get("forked_from_id"):
                    skip = max(safe_int(p.get("subagent_history_start_ordinal")) - 1, 0)
                    copied = True
                recs.append((o, o.get("type"), p, copied))
        if skip:
            cur["skip"] = skip
        # shortcut: judged per chunk (plus earlier chunks of the file), so an older-format turn
        # split exactly between its UserMessage item and its user_message counts twice; older
        # files are complete on disk and read in one chunk, upgrade if that changes.
        if any(t == "event_msg" and p.get("type") == "user_message" for _o, t, p, _c in recs):
            cur["has_um"] = True
        a: dict = cur.setdefault("act", {})
        events: list = []
        for o, t, p, copied in recs:
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
                cur["turn"] = _str(p.get("turn_id")) or cur.get("turn")
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
                # Known gap: some Desktop rows carry only total_tokens (input and output 0) and
                # no model; they are dropped here. See CONTRACT.md.
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
                    session=_session(cur, path),
                ))
                continue
            if copied:      # the parent's turns, not this thread's (usage above is unchanged)
                continue
            prompt = prompt_of(t, p, bool(cur.get("has_um")))
            if prompt is not None:
                after_intr = bool(a.pop("intr", False))
                text, images = prompt
                Link(keys, a.setdefault("link", {})).text(text)
                sess = _session(cur, path)
                # A prompt someone typed. Threads a program drives (subagents, `codex exec`, MCP
                # calls, another agent's integration, scheduled automations) are not human turns.
                if sess.run_origin in ("subagent", "automated") or is_scheduled_automation(text):
                    continue
                # No tokens; a stable per-session ordinal makes the id deterministic so a
                # re-read (file reset) re-emits the same id and the sink dedups it. The ordinal
                # advances only for kept messages, which is fine: it just has to be stable for a
                # given file, and the screening above is deterministic from the same bytes.
                cur["um_ord"] = cur.get("um_ord", 0) + 1
                ts = o.get("timestamp")
                sid = cur["session_id"] or os.path.basename(path)
                events.append(UsageEvent(
                    event_id=make_event_id(NAME, "human", sid, ts or "", cur["um_ord"]),
                    ts=ts, agent=NAME, model=None, provider=None,
                    kind="human", project=basename(cur["cwd"]), session=sess,
                ))
                events.append(ActivityEvent(
                    event_id=make_event_id(NAME, "steer", sid, ts or "", cur["um_ord"]),
                    ts=ts, agent=NAME, kind="steer", session_id=sess.session_id,
                    turn_id=sess.turn_id, attrs={"via": "typed", "words": len(text.split()),
                                                 "chars": len(text), "images": images,
                                                 "after_interrupt": after_intr}))
                continue
            # Best effort: a record whose activity extraction raises is counted and skipped, so
            # activity never blocks the usage and session rows of the same chunk.
            try:
                _activity(o, t, p, a, _session(cur, path), keys, events, cur)
            except Exception:
                act_errors += 1
        cur["off"] = new_off
        agg = a.get("agg")
        if agg is not None and not (agg["calls"] or agg["errors"]):
            del a["agg"]
        if not a.get("link"):
            a.pop("link", None)
        if not any(a.values()):
            cur.pop("act", None)

        def commit(p: str = path, c: dict = cur) -> None:
            files[p] = c

        yield events, commit
    if act_errors:
        print(f"aut: {NAME}: {act_errors} activity record(s) skipped (extraction failed); usage "
              "and session rows are unaffected.", file=sys.stderr)
