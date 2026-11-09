"""Read local agent text against collector identities, without advancing ingestion state."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Iterable

from ..collectors import claude_code, codex, cursor_cli, pi
from ..collectors.activity_common import first_record, text_of
from ..collectors.cursor import connect_ro
from ..cursor_run import automated_sessions
from ..event import CollectorUnavailable, make_event_id
from ..repo_keys import RepoKeys
from ..state import parse_record

AGENTS = frozenset({"claude_code", "codex", "cursor_cli", "pi"})


def session_paths(home: Path, agent: str) -> dict[str, list[tuple[int, str]]]:
    """Canonical session identities and paths in discovery order, without retaining text."""
    discover = {"claude_code": claude_code.main_session_files, "codex": codex.session_files,
                "cursor_cli": cursor_cli.chat_stores, "pi": pi.session_files}[agent]
    index: dict[str, list[tuple[int, str]]] = {}
    for order, path in enumerate(discover(home)):
        sid: Any = None
        if agent == "claude_code":
            sid = Path(path).stem
        elif agent == "cursor_cli":
            sid = Path(path).parent.name
        else:
            head = first_record(path)
            if agent == "codex":
                payload = head.get("payload") if head else None
                if isinstance(payload, dict):
                    sid = payload.get("id") or payload.get("session_id")
            elif head and head.get("type") == "session":
                sid = pi.header(head, path)["id"]
        if isinstance(sid, str):
            index.setdefault(sid, []).append((order, path))
    return index


@dataclass(frozen=True)
class Context:
    text: str
    before: str = ""
    after: str = ""
    prompt_ordinal: int | None = None


def _records(path: str) -> Iterable[dict[str, Any]]:
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            record = parse_record(line)
            if record is not None:
                yield record


def _in_time(record: dict[str, Any], until: datetime | None) -> bool:
    if until is None:
        return True
    try:
        return datetime.fromisoformat(str(record.get("timestamp")).replace("Z", "+00:00")) < until
    except (ValueError, TypeError):
        return False


def _join(events: Iterable[tuple[str | None, str, int | None]], want: set[str]) -> dict[str, Context]:
    out: dict[str, Context] = {}
    last, current = "", None
    for eid, text, ordinal in events:
        if eid is not None:
            current = eid if eid in want else None
            if current:
                out[current] = Context(text, last, prompt_ordinal=ordinal)
        elif text.strip():
            last = text
            if current:
                previous = out[current]
                out[current] = Context(previous.text, previous.before, text, previous.prompt_ordinal)
    return out


def _codex_records(path: str, cur: codex.FileState) -> Iterable[codex.ParsedRecord]:
    with open(path, "rb") as stream:
        offset = 0
        while lines := list(islice(stream, 1000)):
            yield from codex.parsed_records(lines, offset, cur, Counter())
            offset += len(lines)


def _codex_prefixes(path: str, keys: RepoKeys, want: set[str]) -> list[int]:
    # An incrementally collected old file can acquire user_message records later. Resolve
    # the retained legacy prefix against existing IDs, rather than renumber those inputs.
    cur = codex._fresh_state(Path(path).stat())
    legacy: list[str] = []
    seen_modern = False
    modern: list[tuple[str, str, int]] = []
    for record, kind, payload, copied in _codex_records(path, cur):
        codex._note_context(kind, payload, copied, cur, keys)
        if copied:
            continue
        is_modern = kind == "event_msg" and payload.get("type") == "user_message"
        seen_modern |= is_modern
        prompt = codex.prompt_of(kind, payload, seen_modern)
        if prompt is None or codex.is_scheduled_automation(prompt[0]):
            continue
        seed = cur["session_id"] or Path(path).name
        stamp = record.get("timestamp") or ""
        if is_modern:
            modern.append((seed, stamp, len(modern) + 1))
        else:
            legacy.append(make_event_id("codex", "steer", seed, stamp, len(legacy) + 1))
    if not modern:
        return [len(legacy)] if set(legacy) & want else []
    profiles: dict[tuple[tuple[str, str, int], ...], tuple[int, int]] = {}
    for offset in range(len(legacy) + 1):
        matched = [(eid, "legacy", i) for i, eid in enumerate(legacy[:offset]) if eid in want]
        matched.extend((eid, "modern", ordinal) for seed, stamp, ordinal in modern
                       if (eid := make_event_id("codex", "steer", seed, stamp,
                                                offset + ordinal)) in want)
        # Equivalent interpretations need only one bounded replay of the log.
        signature = tuple(sorted(matched))
        profiles[signature] = len({eid for eid, kind, index in matched}), offset
    best = max(count for count, offset in profiles.values())
    return [offset for count, offset in profiles.values() if count == best] if best else []


def _codex_events(path: str, until: datetime | None, keys: RepoKeys, prefix: int,
                   imported_until: datetime | None = None,
                   ) -> Iterable[tuple[str | None, str, int | None]]:
    cur = codex._fresh_state(Path(path).stat())
    legacy, modern_n, seen_modern = 0, 0, False
    for record, kind, payload, copied in _codex_records(path, cur):
        codex._note_context(kind, payload, copied, cur, keys)
        if copied:
            continue
        is_modern = kind == "event_msg" and payload.get("type") == "user_message"
        seen_modern |= is_modern
        prompt = codex.prompt_of(kind, payload, seen_modern)
        if prompt is not None and codex.is_scheduled_automation(prompt[0]):
            yield "", "", None  # A software prompt ends the preceding human prompt's reply interval.
        elif prompt is not None:
            if is_modern:
                modern_n += 1
                ordinal = prefix + modern_n
            else:
                legacy += 1
                if legacy > prefix:
                    yield "", "", None
                    continue
                ordinal = legacy
            if codex.imported_record(record, imported_until):
                yield "", "", None
            elif _in_time(record, until):
                yield make_event_id("codex", "steer", cur["session_id"] or Path(path).name,
                                    record.get("timestamp") or "", ordinal), prompt[0], ordinal
        elif not codex.imported_record(record, imported_until) and _in_time(record, until):
            if kind == "response_item" and payload.get("type") == "message" \
                    and payload.get("role") == "assistant":
                content = payload.get("content")
                blocks = content if isinstance(content, list) else []
                yield None, "\n".join(b["text"] for b in blocks if isinstance(b, dict)
                                     and b.get("type") in {"text", "output_text"}
                                     and isinstance(b.get("text"), str)), None
            elif kind == "event_msg" and payload.get("type") == "agent_message":
                message = payload.get("message")
                if isinstance(message, str):
                    yield None, message, None


def codex_context(home: Path, sessions: set[str], want: set[str],
                  until: datetime | None, keys: RepoKeys, *,
                  paths: Iterable[str] | None = None) -> dict[str, Context]:
    out: dict[str, Context] = {}
    imports = codex.imported_until(home)
    for path in codex.session_files(home) if paths is None else paths:
        head = first_record(path)
        payload = head.get("payload") if head else None
        if not isinstance(payload, dict):
            continue
        sid = payload.get("id") or payload.get("session_id")
        if not isinstance(sid, str) or sid not in sessions:
            continue
        if codex.run_origin(payload) in {"subagent", "automated"}:
            continue
        imported_until = imports.get(sid)
        profiles = [_join(_codex_events(path, until, keys, prefix, imported_until), want)
                    for prefix in _codex_prefixes(path, keys, want)]
        if not profiles:
            continue
        for eid in set.intersection(*(set(p) for p in profiles)):
            values = [p[eid] for p in profiles]
            if len({v.text for v in values}) == 1:
                out[eid] = Context(values[0].text,
                    values[0].before if len({v.before for v in values}) == 1 else "",
                    values[0].after if len({v.after for v in values}) == 1 else "",
                    values[0].prompt_ordinal if len({v.prompt_ordinal for v in values}) == 1 else None)
    return out


def _pi_context(path: str, want: set[str], until: datetime | None) -> dict[str, Context]:
    head = first_record(path)
    if not head or head.get("type") != "session":
        return {}
    hdr = pi.header(head, path)
    nodes: dict[str, dict[str, Any]] = {}
    for record in _records(path):
        if pi.is_fork_copy(record, hdr) or not _in_time(record, until):
            continue
        rid = record.get("id")
        if isinstance(rid, str):
            nodes[rid] = record

    def ancestors(record: dict[str, Any]) -> Iterable[str]:
        parent = record.get("parentId")
        seen = set()
        while isinstance(parent, str) and parent in nodes and parent not in seen:
            seen.add(parent)
            yield parent
            parent = nodes[parent].get("parentId")

    def message(record: dict[str, Any]) -> dict[str, Any]:
        value = record.get("message")
        return value if record.get("type") == "message" and isinstance(value, dict) else {}

    replies: dict[str, list[str]] = {}
    for rid, record in nodes.items():
        msg = message(record)
        if msg.get("role") == "assistant" and text_of(msg.get("content")).strip():
            for parent in ancestors(record):
                if message(nodes[parent]).get("role") == "user":
                    replies.setdefault(parent, []).append(rid)
                    break
    out: dict[str, Context] = {}
    for rid, record in nodes.items():
        msg = message(record)
        eid = make_event_id("pi", "steer", f"{hdr['id']}|{rid}")
        if msg.get("role") != "user" or eid not in want:
            continue
        before = next((text_of(message(nodes[p]).get("content")) for p in ancestors(record)
                       if message(nodes[p]).get("role") == "assistant"
                       and text_of(message(nodes[p]).get("content")).strip()), "")
        candidates = replies.get(rid, [])
        terminal = [p for p in candidates if not any(
            p in ancestors(nodes[q]) for q in candidates if p != q)]
        # An append-only Pi file can contain several branches. Only a unique reply path is safe.
        after = text_of(message(nodes[terminal[0]]).get("content")) if len(terminal) == 1 else ""
        out[eid] = Context(text_of(msg.get("content")), before, after)
    return out


def pi_context(home: Path, sessions: set[str], want: set[str],
               until: datetime | None, *, paths: Iterable[str] | None = None) -> dict[str, Context]:
    out: dict[str, Context] = {}
    for path in pi.session_files(home) if paths is None else paths:
        head = first_record(path)
        if not head or head.get("type") != "session" or pi.header(head, path)["id"] not in sessions:
            continue
        out.update(_pi_context(path, want, until))
    return out


def cursor_context(home: Path, sessions: set[str], want: set[str],
                   until: datetime | None, keys: RepoKeys, *,
                   paths: Iterable[str] | None = None) -> dict[str, Context]:
    out: dict[str, Context] = {}
    ambiguous: set[str] = set()
    for path in cursor_cli.chat_stores(home) if paths is None else paths:
        if Path(path).parent.name not in sessions:
            continue
        con = connect_ro(path)
        if con is None:
            raise CollectorUnavailable("cursor_cli: context store unreadable")
        try:
            meta = cursor_cli.chat_meta(con)
            if not meta or meta.get("subagentInfo"):
                continue
            sid = meta.get("agentId") or Path(path).parent.name
            if sid not in sessions:
                continue
            lists = cursor_cli.root_lists(con)
            messages = cursor_cli.messages(con, {i for seq in lists for i in seq})
            if sid in automated_sessions():
                continue
            first_ids: set[str] = set()
            listed = {mid for seq in lists for mid in seq}
            for seq in lists:
                seen_requests: set[str] = set()
                for mid in seq:
                    rid = cursor_cli.request_id(messages.get(mid, {}))
                    if rid and rid not in seen_requests:
                        first_ids.add(mid)
                        seen_requests.add(rid)
            bases: dict[str, set[str]] = {}
            prompts: list[tuple[str, str]] = []
            for mid, prompt in messages.items():
                rid = cursor_cli.request_id(prompt)
                if not rid:
                    continue
                text = text_of(prompt.get("content"))
                query = cursor_cli.USER_QUERY.search(text)
                text = query[1] if query else text
                # Blob-suffixed IDs identify their text even if a later root starts with that blob.
                prompts.append((make_event_id("cursor_cli", "steer", f"{sid}|{rid}|{mid}"), text))
                if mid in first_ids or mid not in listed:
                    bases.setdefault(rid, set()).add(text)
            for rid, texts in bases.items():
                eid = make_event_id("cursor_cli", "steer", f"{sid}|{rid}")
                if len(texts) != 1:
                    ambiguous.add(eid)
                else:
                    prompts.append((eid, next(iter(texts))))
            for eid, text in prompts:
                if eid not in want:
                    continue
                value = Context(text)
                # Reply order is unknown; conflicting base prompt interpretations are omitted too.
                if eid in out and out[eid] != value:
                    ambiguous.add(eid)
                out[eid] = value
        except (sqlite3.DatabaseError, TypeError, ValueError, IndexError) as exc:
            raise CollectorUnavailable("cursor_cli: invalid context store") from exc
        finally:
            con.close()
    return {eid: value for eid, value in out.items() if eid not in ambiguous}
