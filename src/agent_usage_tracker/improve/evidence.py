"""Read local traces without advancing collection or sending their contents."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ..collectors import claude_code, codex
from ..collectors._util import dict_or_empty
from ..collectors.activity_common import result_status
from ..event import parse_ts
from ..redact import redact
from ..state import parse_record

TEXT_LIMIT = 6000
BYTE_LIMIT = 16 * 1024 * 1024
VERSION = 5


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(isinstance(b, str) for b in value):
            return json.dumps(value, ensure_ascii=False)
        return "\n".join(str(b.get("text", "")) for b in value if isinstance(b, dict))
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def read_session(path: Path, agent: str, max_bytes: int = BYTE_LIMIT,
                 since_epoch: float | None = None,
                 imports: dict[str, datetime] | None = None, *,
                 exclude_automated: bool = False) -> dict[str, Any] | None:
    if agent not in {"claude_code", "codex"} or max_bytes < 1:
        raise ValueError("choose a supported trace source and a positive byte limit")
    events: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], str] = {}
    counts: Counter[str] = Counter()
    digest = hashlib.sha256()
    session, cwd, origin = path.stem, "", "unknown"
    last_closed = False
    copied_until = 0
    model: str | None = None
    used = 0

    def in_window(record: dict[str, Any]) -> bool:
        if since_epoch is None:
            return True
        stamp = parse_ts(record.get("timestamp"))
        if stamp is None or stamp.tzinfo is None:
            counts["missing_timestamps"] += 1
            return False
        if stamp.timestamp() < since_epoch:
            counts["outside_window"] += 1
            return False
        return True

    def emit(kind: str, value: object, line: int, record: dict[str, Any], *,
             call: object = "", tool: object = "", status: str = "unknown",
             key: object = "") -> None:
        nonlocal last_closed
        raw = _text(value)
        identity = str(call or key or line)
        signature = hashlib.sha256(json.dumps({"text": raw, "status": status,
            "tool": str(tool or "")}, sort_keys=True).encode()).hexdigest()
        previous = seen.get((kind, identity))
        if previous:
            counts["duplicate_records" if previous == signature else "conflicting_records"] += 1
            return
        seen[kind, identity] = signature
        cleaned = redact(raw)
        clipped = len(cleaned) > TEXT_LIMIT
        counts["truncated_events"] += clipped
        eid = hashlib.sha256(f"{agent}|{session}|{kind}|{identity}".encode()).hexdigest()[:32]
        events.append({"id": eid, "kind": kind, "line": line,
                       "timestamp": record.get("timestamp"), "text": cleaned[:TEXT_LIMIT],
                       "truncated": clipped, "source_sha256": signature,
                       "call_id": str(call or ""), "tool": str(tool or ""), "status": status})
        if kind in {"call", "prompt"}:
            last_closed = False

    with path.open("rb") as stream:
        for line in range(1, 1_000_001):
            raw = stream.readline(max_bytes - used + 1)
            if not raw:
                break
            if used + len(raw) > max_bytes:
                counts["byte_cap_reached"] = 1
                break
            used += len(raw)
            digest.update(raw)
            if not raw.endswith(b"\n"):
                counts["partial_tail"] = 1
                break
            record = parse_record(raw)
            if record is None:
                counts["malformed_records"] += 1
                continue
            kind = record.get("type")
            if agent == "codex":
                payload = dict_or_empty(record.get("payload"))
                if line == 1 and kind == "session_meta":
                    session = str(payload.get("id") or payload.get("session_id") or session)
                    cwd = str(payload.get("cwd") or "")
                    origin = codex.run_origin(payload)
                    # Audits discard these sources; avoid normalizing their private bodies first.
                    if exclude_automated and origin in {"subagent", "automated"}:
                        return None
                    if payload.get("forked_from_id"):
                        copied_until = int(payload.get("subagent_history_start_ordinal") or 0)
                if line <= copied_until:
                    counts["copied_records"] += 1
                    continue
                if kind not in {"session_meta", "turn_context"} \
                        and codex.imported_record(record, (imports or {}).get(session)):
                    counts["imported_records"] += 1
                    continue
                if kind not in {"session_meta", "turn_context"} and not in_window(record):
                    continue
                sub = payload.get("type")
                if kind == "turn_context":
                    model = payload.get("model") or model
                elif kind == "response_item":
                    call_id = payload.get("call_id")
                    if sub in {"function_call", "custom_tool_call"}:
                        emit("call", payload.get("arguments", payload.get("input", "")),
                             line, record, call=call_id, tool=payload.get("name"))
                    elif sub in {"function_call_output", "custom_tool_call_output"}:
                        value = payload.get("output", "")
                        emit("result", value, line, record, call=call_id,
                             status=result_status(value))
                    elif sub == "message" and payload.get("role") in {"user", "assistant"}:
                        emit("prompt" if payload["role"] == "user" else "reply",
                             payload.get("content", ""), line, record, key=payload.get("id"))
                elif kind == "event_msg":
                    item = dict_or_empty(payload.get("item"))
                    if sub == "user_message":
                        message = payload.get("message", "")
                        if codex.is_scheduled_automation(message):
                            origin = "automated"
                        emit("prompt", message, line, record)
                    elif sub == "agent_message":
                        emit("reply", payload.get("message", ""), line, record)
                    elif sub in {"task_complete", "turn_aborted"}:
                        last_closed = sub == "task_complete"
                        emit("boundary", "turn_complete" if last_closed else "turn_aborted",
                             line, record)
                    elif sub == "item_completed" and item.get("type") == "CommandExecution":
                        identity = item.get("id") or f"command-{line}"
                        emit("call", item.get("command", ""), line, record,
                             call=identity, tool="exec_command")
                        emit("result", {"exit_code": item.get("exit_code"),
                                       "output": item.get("aggregated_output", "")},
                             line, record, call=identity,
                             status=result_status({"exit_code": item.get("exit_code")}))
                    elif sub == "item_completed" and item.get("type") == "McpToolCall":
                        identity = item.get("id") or f"mcp-{line}"
                        emit("call", item.get("arguments", {}), line, record, call=identity,
                             tool=item.get("tool") or item.get("name") or "mcp")
                        error = item.get("status") == "failed"
                        emit("result", item.get("error") if error else item.get("result", ""),
                             line, record, call=identity,
                             status="error" if error else "success" if item.get("status")
                             == "completed" else "unknown")
                    elif sub == "item_completed" and item.get("type") in {"UserMessage", "AgentMessage"}:
                        emit("prompt" if item["type"] == "UserMessage" else "reply",
                             item.get("content", item.get("text", "")), line, record,
                             key=item.get("id"))
                    elif sub == "item_completed" and item.get("type") == "ContextCompaction":
                        emit("compaction", "context was compacted", line, record, key=item.get("id"))
                continue
            session = str(record.get("sessionId") or session)
            cwd = str(record.get("cwd") or cwd)
            if record.get("entrypoint"):
                origin = "interactive" if record["entrypoint"] == "cli" else "automated"
            if record.get("isSidechain"):
                origin = "subagent"
            if kind not in {"user", "assistant"} and not (
                kind == "system" and record.get("subtype") in {"turn_duration", "compact_boundary"}
            ):
                continue
            if not in_window(record):
                continue
            if kind == "system" and record.get("subtype") == "turn_duration":
                last_closed = True
                emit("boundary", "turn_complete", line, record)
            message = dict_or_empty(record.get("message"))
            model = message.get("model") or model
            content = message.get("content", "")
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            if kind == "assistant":
                for index, block in enumerate(blocks):
                    if block.get("type") == "tool_use":
                        emit("call", block.get("input", {}), line, record,
                             call=block.get("id"), tool=block.get("name"))
                    elif block.get("type") == "text":
                        emit("reply", block.get("text", ""), line, record,
                             key=f"{record.get('uuid', line)}:{index}")
            elif kind == "user":
                for block in blocks:
                    if block.get("type") == "tool_result":
                        value = block.get("content", "")
                        emit("result", value, line, record, call=block.get("tool_use_id"),
                             status=result_status(value, block.get("is_error", False)))
                human = claude_code.human_text(record)
                if human is not None:
                    emit("prompt", human, line, record, key=record.get("uuid"))
            if kind == "system" and record.get("subtype") == "compact_boundary":
                emit("compaction", "context was compacted", line, record)

    calls = {e["call_id"] for e in events if e["kind"] == "call"}
    results = {e["call_id"] for e in events if e["kind"] == "result"}
    coverage: dict[str, Any] = dict(counts)
    for name in ("partial_tail", "byte_cap_reached"):
        coverage[name] = bool(counts[name])
    coverage.update(calls=len(calls), matched_results=len(calls & results),
                    unmatched_calls=len(calls - results), orphan_results=len(results - calls),
                    truncated_events=counts["truncated_events"],
                    copied_records=counts["copied_records"])
    coverage["complete"] = last_closed and not any(coverage.get(k) for k in (
        "unmatched_calls", "orphan_results", "partial_tail", "byte_cap_reached",
        "truncated_events", "malformed_records", "conflicting_records", "missing_timestamps"))
    return {"version": VERSION, "agent": agent, "session_id": session, "cwd": cwd,
            "origin": origin, "model": model, "events": events, "coverage": coverage,
            "source": {"path": str(path), "sha256": digest.hexdigest(), "bytes": used}}
