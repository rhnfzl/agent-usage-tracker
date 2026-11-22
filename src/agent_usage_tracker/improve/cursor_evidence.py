"""Capture the latest native Cursor CLI chat root in a private read-only snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from ..collectors import cursor_cli
from ..collectors._util import dict_or_empty, epoch_ms_to_iso
from ..collectors.activity_common import result_status, text_of
from ..cursor_run import automated_sessions
from ..event import parse_ts
from .evidence import VERSION, has_attachment, trace_event


def read(path: Path, since: float, max_bytes: int, *, capture_full: bool = False) -> dict[str, Any] | None:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max_bytes)
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT length(value) FROM meta WHERE key='0'").fetchone()
        if not row or row[0] is None or row[0] >= max_bytes:
            raise ValueError("Cursor metadata missing or over budget")
        meta = cursor_cli.chat_meta(db)
        if not meta or not isinstance(root := meta.get("latestRootBlobId"), str):
            raise ValueError("Cursor chat root unavailable")
        session = str(meta.get("agentId") or path.parent.name)
        if meta.get("subagentInfo") or session in automated_sessions():
            return None
        budget = max_bytes - row[0]
        fingerprint = hashlib.sha256(json.dumps(
            [session, root, meta.get("createdAt"), meta.get("cwd")]).encode())
        counts: Counter[str] = Counter()
        events: list[dict[str, Any]] = []
        timestamp = None
        request = None
        pending_summary = None
        active = False
        replied = False
        seen: set[tuple[str, str]] = set()

        def blob(identity: str) -> bytes | None:
            nonlocal budget
            found = db.execute("SELECT length(data), CASE WHEN length(data)<=? THEN data END "
                               "FROM blobs WHERE id=?", (budget, identity)).fetchone()
            if not found or found[0] is None:
                counts["source_incomplete"] = 1
                return None
            if not isinstance(found[1], bytes):
                counts["source_incomplete"] = 1
                counts["byte_cap_reached"] += found[0] > budget
                return None
            budget -= found[0]
            fingerprint.update(found[1])
            return found[1]

        def emit(kind: str, key: str, value: object, record_id: str, **kwargs: Any) -> None:
            identity = (kind, str(kwargs.get("call") or key))
            if identity in seen:
                counts["source_incomplete"] = 1
                return
            seen.add(identity)
            events.append({**trace_event("cursor_cli", session, kind, identity[1], value,
                                         timestamp, capture_full=capture_full, **kwargs),
                           "table": "blobs", "record_id": record_id})

        sequence = blob(root)
        if sequence is None:
            raise ValueError("Cursor chat root payload unavailable")
        try:
            messages = cursor_cli._field1_ids(sequence, limit=20001)
        except IndexError as exc:
            raise ValueError("Cursor chat root is truncated") from exc
        if messages:
            for ordinal, mid in enumerate(messages):
                if ordinal == 20000:
                    counts["source_incomplete"] = 1
                    break
                raw = blob(mid)
                if raw is None:
                    if counts["byte_cap_reached"]:
                        break
                    continue
                message = json.loads(raw)
                if not isinstance(message, dict):
                    counts["source_incomplete"] = 1
                    continue
                role = message.get("role")
                body = message.get("content")
                omitted = has_attachment(body)
                options = cursor_cli._cursor_options(message)
                if options.get("isSummary"):
                    counts["source_incomplete"] = 1
                    if active:
                        emit("compaction", mid, "context was compacted", mid)
                    else:
                        pending_summary = mid
                    continue
                if rid := cursor_cli.request_id(message):
                    if rid == request:
                        # An in-turn user message can change the goal mid-attempt.
                        counts["source_incomplete"] = 1
                    elif active and replied:
                        emit("boundary", mid, "turn_complete", mid)
                    request = rid
                    text = text_of(body)
                    timestamp = cursor_cli.prompt_ts(text, epoch_ms_to_iso(meta.get("createdAt")))
                    stamp = parse_ts(timestamp)
                    counts["missing_timestamps"] += stamp is None
                    active = bool(stamp is not None and stamp.timestamp() >= since)
                    replied = False
                    if active:
                        query = cursor_cli.USER_QUERY.search(text)
                        emit("prompt", mid, query[1] if query else text, mid, omitted_attachment=omitted)
                        if pending_summary:
                            emit("compaction", pending_summary, "context was compacted", pending_summary)
                            pending_summary = None
                    continue
                if not active:
                    continue
                if role not in {"assistant", "tool"}:
                    continue
                if isinstance(body, str) and role == "assistant" and body:
                    emit("reply", mid, body, mid)
                    replied = True
                    continue
                for index, block in enumerate(body if isinstance(body, list) else []):
                    if not isinstance(block, dict):
                        continue
                    key = f"{mid}:{index}"
                    tool, call = block.get("toolName"), block.get("toolCallId")
                    if role == "assistant" and block.get("type") == "text" and block.get("text"):
                        emit("reply", key, block["text"], mid, omitted_attachment=omitted)
                        replied = True
                    elif role == "assistant" and block.get("type") == "tool-call":
                        emit("call", key, block.get("args", {}), mid, call=call, tool=tool)
                        replied = False
                    elif role == "tool" and block.get("type") == "tool-result":
                        value = block.get("result", "")
                        error = dict_or_empty(options.get("highLevelToolCallResult")).get("isError")
                        status = result_status(value, error)
                        if tool == "Shell" and isinstance(value, str) and (ex := cursor_cli.EXIT_LINE.match(value)):
                            status = "error" if error is True or int(ex[1]) else "success"
                        emit("result", key, value, mid, call=call, tool=tool, status=status,
                             omitted_attachment=omitted)
                        replied = False

    calls = {e["call_id"] for e in events if e["kind"] == "call"}
    results = {e["call_id"] for e in events if e["kind"] == "result"}
    coverage = {**counts, "complete": False, "calls": len(calls),
                "matched_results": len(calls & results), "unmatched_calls": len(calls - results),
                "orphan_results": len(results - calls),
                "truncated_events": sum(e["truncated"] for e in events)}
    # The latest root has no explicit terminal marker for its trailing request.
    return {"version": VERSION, "agent": "cursor_cli", "session_id": session,
            "cwd": str(meta.get("cwd") or ""), "origin": "interactive", "model": None,
            "events": events, "coverage": coverage,
            "source": {"path": str(path), "sha256": fingerprint.hexdigest(),
                       "bytes": max_bytes - budget}}
