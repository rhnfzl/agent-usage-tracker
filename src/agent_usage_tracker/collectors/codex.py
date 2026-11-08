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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple, TypedDict

from ..config import Config
from ..event import (Row, ActivityKind, Batch, ActivityEvent, CollectorUnavailable, EventSession,
                     RunOrigin, UsageEvent, make_event_id, parse_ts)
from ..repo_keys import RepoKeys, repo_identity
from ..state import parse_record
from .activity_common import (Link, TurnAgg, add_call, best_effort, blank_tool_tally, drop_raw_shas,
                              first_record, human_event, ms_between, record_commits, text_of,
                              turn_attrs)
from .commits import commit_call, commit_dir, commit_shas
from ._util import (MALFORMED_RECORD, count_field, dict_or_empty, file_batches, project_name,
                    safe_int, str_or_none)

NAME = "codex"
# The watermark's format. Older state is upgraded once by _upgrade_state; delete a step from there
# once no machine can still hold state that old.
_STATE_V = 2


class FileState(TypedDict, total=False):
    """One file's watermark entry. An entry saved by an older version also holds an unread `dev`."""
    off: int
    ino: int
    model: str | None
    provider: str | None
    session_id: str | None    # the usage event_id seed; see _note_session_meta
    cwd: str | None
    tc_ord: int               # token_count records so far
    um_ord: int               # kept user messages so far
    meta: dict[str, Any]      # identity from the first session_meta; see _set_meta
    turn: str | None
    has_um: bool              # the file has user_message records
    skip: int                 # copied lines of a fork still unread; see parsed_records
    act: dict[str, Any]       # _Activity's carried state
    import_checked: str       # import boundary used to rebuild carried state
    import_has_um: bool       # imported prefix format, independent of the later native format


def sessions_dir(home: Path) -> Path:
    return home / ".codex" / "sessions"


def session_files(home: Path) -> list[str]:
    return glob.glob(str(sessions_dir(home) / "**" / "rollout-*.jsonl"), recursive=True)


def imported_until(home: Path) -> dict[str, datetime]:
    """Imported history has import-time timestamps, not new native work timestamps."""
    try:
        value = json.loads((home / ".codex/external_agent_session_imports.json").read_text(
            encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            raise ValueError("Invalid import index")
        out: dict[str, datetime] = {}
        for record in value["records"]:
            if (not isinstance(record, dict)
                    or not isinstance(record.get("imported_thread_id"), str)
                    or not record["imported_thread_id"]
                    or type(record.get("imported_at")) is not int or record["imported_at"] < 0):
                raise ValueError("Invalid import record")
            # shortcut: second precision omits same-second work, use offsets when Codex records them.
            bound = datetime.fromtimestamp(record["imported_at"] + 1, timezone.utc)
            sid = record["imported_thread_id"]
            out[sid] = max(out.get(sid, bound), bound)
        return out
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, OverflowError) as exc:
        raise CollectorUnavailable("codex: import index is unreadable or invalid") from exc


def imported_record(record: dict[str, Any], bound: datetime | None) -> bool:
    if bound is None:
        return False
    stamp = parse_ts(record.get("timestamp"))
    return stamp is None or stamp.tzinfo is None or stamp < bound


def thread_names(home: Path) -> dict[str, str]:
    """session id -> the name Codex gave the thread (session_index.jsonl). Best effort."""
    names: dict[str, str] = {}
    try:
        with open(home / ".codex" / "session_index.jsonl", encoding="utf-8") as fh:
            for line in fh:
                o = parse_record(line)
                if o and isinstance(o.get("id"), str) and isinstance(o.get("thread_name"), str):
                    names[o["id"]] = o["thread_name"]
    except (OSError, ValueError):       # no index, or a bad byte: threads show what it had so far
        return names
    return names


def _fresh_state(st: os.stat_result) -> FileState:
    return {"off": 0, "ino": st.st_ino, "model": None, "provider": "openai",
            "session_id": None, "cwd": None, "tc_ord": 0, "um_ord": 0}


# session_meta.source values that mean the thread was started by software, not by a person:
# 'exec' is a scripted `codex exec`, 'mcp' is a programmatic MCP call, and a dict carrying
# 'subagent' is a thread another agent spawned (whose opening prompt is the parent agent
# talking, and which would double-count the human prompt that started the parent anyway).
_PROGRAM_SOURCES = {"exec", "mcp"}


def run_origin(p: dict[str, Any]) -> RunOrigin:
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


def _set_meta(cur: FileState, p: dict[str, Any], keys: RepoKeys) -> None:
    """Identity, origin and cwd from the FIRST session_meta only: a forked file repeats its
    parent's meta on a later line. Its git block gives the link fields every turn carries: the
    repo from the cwd, else the remote URL (hashed, never kept), and the branch, as keys."""
    cur["meta"] = {"id": str_or_none(p.get("id")), "parent": str_or_none(p.get("parent_thread_id")),
                   "origin": run_origin(p)}
    cur["cwd"] = p.get("cwd") or cur.get("cwd")
    cur["meta"]["link"] = _meta_link(p, keys)


def _meta_link(p: dict[str, Any], keys: RepoKeys) -> dict[str, Any]:
    g = dict_or_empty(p.get("git"))
    rk = keys.of_cwd(p.get("cwd"))
    if not rk and isinstance(g.get("repository_url"), str):
        try:
            rk = keys.of(repo_identity(g["repository_url"]))
        except ValueError:      # a malformed URL names no repo: the key stays NULL
            rk = None
    # shortcut: a later turn_context cwd in another repo keeps the first meta's repo; take the
    # turn's cwd if Codex threads start moving between repos
    link = Link(keys, {})
    link.place(2, rk, g.get("branch"))
    return link.s


def _session(cur: FileState, path: str) -> EventSession:
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


def prompt_of(rtype: object, payload: dict[str, Any], has_um: bool) -> tuple[str, int] | None:
    """(text, images) when the record is a prompt: a user_message, or a UserMessage item in a
    file that has no user_message (older files carry both for one prompt)."""
    if rtype != "event_msg":
        return None
    if payload.get("type") == "user_message":
        imgs = payload.get("local_images")
        msg = payload.get("message")
        return (msg if isinstance(msg, str) else ""), len(imgs) if isinstance(imgs, list) else 0
    it = payload.get("item")
    if payload.get("type") == "item_completed" and isinstance(it, dict) \
            and it.get("type") == "UserMessage" and not has_um:
        content = it.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        return text_of(content), sum(b.get("type") != "text" for b in blocks)
    return None


class _Activity:
    """Per-file activity extractor over `cur`, the private copy of the file's watermark entry."""

    # the record being read, set by record()
    sess: EventSession
    link: Link
    agg: TurnAgg

    def __init__(self, cur: FileState, path: str, keys: RepoKeys) -> None:
        self.cur, self.path, self.keys = cur, path, keys
        self.carried: dict[str, Any] = cur.setdefault("act", {})
        self.events: list[ActivityEvent] = []
        self.errors: Counter[str] = Counter()      # records whose extraction raised (skipped)

    def _emit(self, kind: ActivityKind, ts: object, seed: str, attrs: dict[str, Any]) -> None:
        sess = self.sess
        self.events.append(ActivityEvent(
            event_id=make_event_id(NAME, kind, f"{sess.session_id}|{seed}"), ts=str_or_none(ts),
            agent=NAME, kind=kind, session_id=sess.session_id, turn_id=sess.turn_id, attrs=attrs))

    def record(self, o: dict[str, Any]) -> None:
        """Non-prompt activity of one record: tool aggregates, interrupt, turn, commit, compaction,
        link."""
        record_type, payload = o.get("type"), dict_or_empty(o.get("payload"))
        ts = o.get("timestamp")
        self.sess = _session(self.cur, self.path)
        self.link = Link(self.keys, self.carried.setdefault("link", {}))
        self.agg = self.carried.setdefault("agg", blank_tool_tally())
        if record_type == "response_item":
            self.link.note_text(payload.get("output"))           # a tool call's output
            self._tool_call(payload)
        elif record_type == "event_msg":
            self._event_msg(payload, ts)

    def _event_msg(self, payload: dict[str, Any], ts: object) -> None:
        kind, item = payload.get("type"), payload.get("item")
        turn_seed = str(payload.get("turn_id") or ts)
        if kind in ("task_complete", "turn_aborted"):
            self._close_link(ts, turn_seed)
        if kind == "agent_message":
            self.link.note_text(payload.get("message"))
        elif kind == "task_started":
            self.carried["agg"], self.carried["start"] = blank_tool_tally(), ts
        elif kind == "turn_aborted" and payload.get("reason") == "interrupted":
            self.carried["intr"] = True
            self._emit("interrupt", ts, turn_seed, {})
        elif kind == "task_complete":
            self._turn(payload.get("duration_ms"), ts, turn_seed)
        elif kind == "item_completed" and isinstance(item, dict):
            self._item(item, ts)

    def _close_link(self, ts: object, seed: str) -> None:
        self.link.merge((self.cur.get("meta") or {}).get("link") or {})
        self._emit("link", ts, seed, self.link.attrs(self.sess.run_origin))
        self.link.reset()

    def _turn(self, d: object, ts: object, seed: str) -> None:
        dur = d if isinstance(d, int) and not isinstance(d, bool) \
            else ms_between(self.carried.get("start"), ts)
        self._emit("turn", ts, seed, {"duration_ms": dur, **turn_attrs(self.agg)})
        self.carried["agg"] = blank_tool_tally()
        self.carried.pop("start", None)

    def _tool_call(self, payload: dict[str, Any]) -> None:
        name = payload.get("name")
        if payload.get("type") in ("function_call", "custom_tool_call") and isinstance(name, str):
            # an MCP namespace is a server label (free text); other namespaces are codex's own.
            # Older CLI files name MCP tools `<server>__<tool>` with no namespace; codex's own
            # tool names never contain `__`.
            ns = payload.get("namespace")
            mcp = (isinstance(ns, str) and ns.startswith("mcp__")) or "__" in name
            if mcp:     # Desktop may log the same call again as an McpToolCall item
                self.agg.setdefault("mcp_ids", []).append(payload.get("call_id"))
            add_call(self.agg, "mcp" if mcp else name, payload.get("arguments", payload.get("input")))

    def _item(self, item: dict[str, Any], ts: object) -> None:
        typ = item.get("type")
        if typ == "CommandExecution":
            code = item.get("exit_code")
            err = isinstance(code, int) and code != 0
            self.agg["errors"] += err
            script = script_of(item.get("command"))
            call = commit_call(script) if script else None
            out_text = item.get("aggregated_output")
            out_text = out_text if isinstance(out_text, str) else ""
            self.link.note_text(out_text)
            if call and script:
                found = commit_shas(out_text, err, call[1])
                for sha, attrs in record_commits(self.link, item.get("cwd"), commit_dir(script),
                                                 out_text, found, amend=call[0]):
                    self._emit("commit", ts, f"{item.get('id')}|{sha}", attrs)
        elif typ == "McpToolCall":
            # Desktop logs most MCP calls only as this item; one that pairs (same id) with an
            # MCP function_call of the turn was already counted
            if item.get("id") not in self.agg.get("mcp_ids", ()):
                add_call(self.agg, "mcp", item.get("arguments"))
            self.agg["errors"] += item.get("status") == "failed"
        elif typ == "ContextCompaction":
            self._emit("compaction", ts, str(item.get("id") or ts), {})


def _upgrade_state(wm: dict[str, Any], keys: RepoKeys) -> None:
    """Bring state saved by an older version to the current shape, once. An entry from before
    um_ord existed is dropped, so the file is re-read from 0 and user_message ordinals (and thus
    their event_ids) stay consistent if it is later reset and re-read; that is safe because
    event_id is content-deterministic and re-emitted rows dedup at the sink. An entry from before
    identity (or its link fields) was kept takes them from the file's first record. Link SHAs
    saved before they were keyed are dropped. Each step is safe on state already upgraded."""
    if wm.get("state_v") == _STATE_V:
        return
    files = wm.setdefault("files", {})
    for path, entry in list(files.items()):
        if "um_ord" not in entry:
            del files[path]
            continue
        drop_raw_shas(entry.get("act"))
        if safe_int(entry.get("off")) and "link" not in (entry.get("meta") or {}):
            first = first_record(path)
            if first and first.get("type") == "session_meta" \
                    and isinstance(first.get("payload"), dict):
                if entry.get("meta"):           # identity stays as it was first read
                    entry["meta"]["link"] = _meta_link(first["payload"], keys)
                else:
                    _set_meta(entry, first["payload"], keys)
    wm["state_v"] = _STATE_V


class ParsedRecord(NamedTuple):
    record: dict[str, Any]
    type: object
    payload: dict[str, Any]
    copied: bool


def parsed_records(lines: list[bytes], off: int, cur: FileState,
                   skipped: Counter[str]) -> list[ParsedRecord]:
    """Each new line that has a payload. A forked child's file
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
            recs.append(ParsedRecord(o, o.get("type"), p, copied))
    if skip:
        cur["skip"] = skip
    return recs


def _note_session_meta(payload: dict[str, Any], cur: FileState, keys: RepoKeys) -> None:
    # the usage event_id seed keeps its old rule (payload.session_id, any meta line),
    # so stored usage rows never change id; session rows use the thread id instead
    cur["session_id"] = payload.get("session_id") or payload.get("id") or cur["session_id"]
    cur["provider"] = str_or_none(payload.get("model_provider")) or cur["provider"]
    if "meta" not in cur:
        _set_meta(cur, payload, keys)


def _note_context(rtype: object, payload: dict[str, Any], copied: bool, cur: FileState, keys: RepoKeys) -> None:
    """Streaming state from a record: session identity, model, cwd, the current turn."""
    if rtype == "session_meta":
        _note_session_meta(payload, cur, keys)
    elif rtype == "turn_context":
        cur["model"] = str_or_none(payload.get("model")) or cur["model"]
        if payload.get("cwd"):
            cur["cwd"] = payload.get("cwd")
    elif rtype == "event_msg" and payload.get("type") == "task_started" and not copied:
        cur["turn"] = str_or_none(payload.get("turn_id")) or cur.get("turn")


def _token_event(o: dict[str, Any], payload: dict[str, Any], cur: FileState, path: str) -> UsageEvent | None:
    """The usage row of a token_count record, None if it has no tokens. Raises on malformed counts."""
    info = payload.get("info")
    last = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(last, dict):
        return None
    inp = count_field(last, "input_tokens")
    cached = count_field(last, "cached_input_tokens")
    out = count_field(last, "output_tokens")
    rea = count_field(last, "reasoning_output_tokens")
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


def _prompt_rows(o: dict[str, Any], prompt: tuple[str, int], cur: FileState, path: str,
                 act: _Activity, imported: bool = False) -> list[Row]:
    """The human and steer rows of a prompt someone typed. Threads a program drives (subagents,
    `codex exec`, MCP calls, another agent's integration, scheduled automations) are not human
    turns, and get none."""
    after_intr = bool(act.carried.pop("intr", False))
    text, images = prompt
    if not imported:
        Link(act.keys, act.carried.setdefault("link", {})).note_text(text)
    sess = _session(cur, path)
    if sess.run_origin in ("subagent", "automated") or is_scheduled_automation(text):
        return []
    # No tokens; a stable per-session ordinal makes the id deterministic so a
    # re-read (file reset) re-emits the same id and the sink dedups it. The ordinal
    # Includes imported human messages so existing native-tail IDs stay unchanged.
    cur["um_ord"] += 1
    if imported:
        return []
    ts = o.get("timestamp")
    sid = cur["session_id"] or os.path.basename(path)
    return [
        human_event(NAME, (sid, ts or "", cur["um_ord"]), ts, sess, project_name(cur["cwd"])),
        ActivityEvent(
            event_id=make_event_id(NAME, "steer", sid, ts or "", cur["um_ord"]),
            ts=str_or_none(ts), agent=NAME, kind="steer", session_id=sess.session_id,
            turn_id=sess.turn_id, attrs={"via": "typed", "words": len(text.split()),
                                         "chars": len(text), "images": images,
                                         "after_interrupt": after_intr}),
    ]


def _chunk_rows(path: str, recs: list[ParsedRecord], cur: FileState, keys: RepoKeys,
                skipped: Counter[str], imports: dict[str, datetime] | None = None) -> list[Row]:
    """All rows of one file's new records; updates `cur`, the private copy of its watermark entry."""
    # shortcut: judged per chunk (plus earlier chunks of the file), so an older-format turn
    # split exactly between its UserMessage item and its user_message counts twice; older
    # files are complete on disk and read in one chunk, upgrade if that changes.
    if any(t == "event_msg" and p.get("type") == "user_message" for _o, t, p, _c in recs):
        cur["has_um"] = True
    act = _Activity(cur, path, keys)
    events: list[Row] = []
    for o, t, p, copied in recs:
        try:
            previous_turn = cur.get("turn")
            _note_context(t, p, copied, cur, keys)
            imported = imported_record(o, (imports or {}).get(_session(cur, path).session_id))
            if imported:
                cur["turn"] = previous_turn
            if t == "event_msg" and p.get("type") == "token_count":
                event = _token_event(o, p, cur, path)
                if event and not imported:
                    events.append(event)
                continue
            if copied:      # the parent's turns, not this thread's (usage above is unchanged)
                continue
            prompt = prompt_of(t, p, bool(cur.get("import_has_um") if imported
                                        else cur.get("has_um")))
            if prompt is not None:
                events.extend(_prompt_rows(o, prompt, cur, path, act, imported))
                continue
            if imported:
                continue
            best_effort(act.errors, act.record, o)
        except MALFORMED_RECORD:    # one bad line must not wedge every later scan at this offset
            skipped["malformed"] += 1
    skipped.update(act.errors)
    return events + act.events


def _prepare_import(path: str, cur: FileState, off: int, keys: RepoKeys,
                    imports: dict[str, datetime]) -> None:
    sid = (cur.get("meta") or {}).get("id")
    if not isinstance(sid, str):
        head = first_record(path)
        payload = head.get("payload") if head else None
        sid = payload.get("id") or payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(sid, str) or sid not in imports:
        return
    bound = imports[sid]
    if cur.get("import_checked") == bound.isoformat():
        return
    # A later native format must not renumber the completed imported prefix.
    modern = False
    try:
        with open(path, "rb") as stream:
            for line in stream:
                record = parse_record(line)
                if record and imported_record(record, bound) and record.get("type") == "event_msg":
                    p = record.get("payload")
                    if isinstance(p, dict) and p.get("type") == "user_message":
                        modern = True
                        break
        if off:
            prior = _fresh_state(Path(path).stat())
            prior["import_has_um"] = modern
            with open(path, "rb") as stream:
                consumed, chunk_start = 0, 0
                lines: list[bytes] = []
                for line in stream:
                    if consumed + len(line) > off:
                        break
                    lines.append(line)
                    consumed += len(line)
                    if len(lines) == 1000 or consumed == off:
                        records = parsed_records(lines, chunk_start, prior, Counter())
                        _chunk_rows(path, records, prior, keys, Counter(), imports)
                        lines = []
                        chunk_start = consumed
            cur["act"] = prior.get("act", {})
            cur["turn"] = prior.get("turn")
    except OSError as exc:
        raise CollectorUnavailable("codex: imported carried state cannot be checked") from exc
    cur["import_has_um"], cur["import_checked"] = modern, bound.isoformat()


def _trim_state(cur: FileState) -> None:
    carried = cur["act"]
    agg = carried.get("agg")
    if agg is not None and not (agg["calls"] or agg["errors"]):
        del carried["agg"]
    if not carried.get("link"):
        carried.pop("link", None)
    if not any(carried.values()):
        cur.pop("act", None)


def collect(cfg: Config, wm: dict[str, Any]) -> Iterator[Batch]:
    if not sessions_dir(cfg.home).exists():
        return
    keys = RepoKeys(cfg.repo_key_salt)
    imports = imported_until(cfg.home)
    _upgrade_state(wm, keys)

    def chunk_rows(path: str, cur: FileState, off: int, lines: list[bytes],
                   skipped: Counter[str]) -> list[Row]:
        if imports:
            _prepare_import(path, cur, off, keys, imports)
        events = _chunk_rows(path, parsed_records(lines, off, cur, skipped), cur, keys, skipped,
                             imports)
        _trim_state(cur)
        return events

    yield from file_batches(NAME, session_files(cfg.home), wm["files"], _fresh_state, chunk_rows)
