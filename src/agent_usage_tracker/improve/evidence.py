"""Read local traces without advancing collection or sending their contents."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
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
VERSION = 8
ATTACHMENT_TYPES = ("image", "file", "document", "audio", "video", "resource")
_CODEX_UNCOUNTED_RESPONSE_TOOLS = {
    "web_search_call", "image_generation_call", "local_shell_call",
    "tool_search_call", "tool_search_output",
}
_CODEX_UNCOUNTED_COMPLETED_ITEMS = {
    "CollabAgentToolCall", "Extension", "FileChange", "FunctionCallOutput",
    "ImageGeneration", "ImageView", "WebSearch",
}


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(isinstance(b, str) for b in value):
            return json.dumps(value, ensure_ascii=False)
        return "\n".join(str(b.get("text", "")) for b in value
                         if isinstance(b, dict) and b.get("type") not in ATTACHMENT_TYPES)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def has_attachment(content: object) -> bool:
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type")
                                            in ATTACHMENT_TYPES for block in content)


def trace_event(agent: str, session: str, kind: str, identity: str, value: object,
                timestamp: object, *, call: object = "", tool: object = "",
                status: str = "unknown", omitted_attachment: bool = False,
                capture_full: bool = False) -> dict[str, Any]:
    raw = _text(value)
    omitted_attachment = omitted_attachment or has_attachment(value)
    if omitted_attachment:
        raw = "[Attachment omitted]\n" + raw
    signature = hashlib.sha256(json.dumps({"text": raw, "status": status,
        "tool": str(tool or "")}, sort_keys=True).encode()).hexdigest()
    cleaned = redact(raw)
    content = {"raw_text": raw, "attachment_omitted": omitted_attachment} if capture_full else {}
    return {"id": hashlib.sha256(f"{agent}|{session}|{kind}|{identity}".encode()).hexdigest()[:32],
            "kind": kind, "timestamp": timestamp, "text": cleaned[:TEXT_LIMIT],
            "truncated": omitted_attachment or len(cleaned) > TEXT_LIMIT, "source_sha256": signature,
            "call_id": str(call or ""), "tool": str(tool or ""), "status": status, **content}


def _codex_message_text(item: dict[str, Any]) -> str:
    text = _text(item.get("content", item.get("text", "")))
    citation = item.get("memory_citation")
    if not isinstance(citation, dict) or set(citation) != {"entries", "rolloutIds"}:
        return text
    entries, rollouts = citation["entries"], citation["rolloutIds"]
    if not isinstance(entries, list) or not isinstance(rollouts, list) \
            or not all(isinstance(value, str) for value in rollouts) \
            or not (entries or rollouts):
        return text
    lines = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "lineStart", "lineEnd", "note"} \
                or not all(isinstance(entry[key], str) for key in ("path", "note")) \
                or type(entry["lineStart"]) is not int or type(entry["lineEnd"]) is not int \
                or not 1 <= entry["lineStart"] <= entry["lineEnd"]:
            return text
        lines.append(f"{entry['path']}:{entry['lineStart']}-{entry['lineEnd']}"
                     f"|note=[{entry['note']}]")
    # Native AgentMessage stores citations separately; response_item renders them in the text.
    return (text + "<oai-mem-citation>\n<citation_entries>\n" + "\n".join(lines)
            + "\n</citation_entries>\n<rollout_ids>\n" + "\n".join(rollouts)
            + "\n</rollout_ids>\n</oai-mem-citation>")


def _recent_records(path: Path, budget: int,
                    observation: dict[str, int] | None = None) -> Iterator[tuple[int, int, bytes]]:
    """Keep the first metadata record and a tail, using byte offsets for unknown ordinals."""
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        if observation is not None:
            observation["size"] = size
        stream.seek(0)
        if size <= budget:
            offset = 0
            for line in range(1, 1_000_001):
                if offset >= size:
                    break
                raw = stream.readline(min(size - offset, budget - offset))
                if not raw:
                    break
                yield line, offset, raw
                offset += len(raw)
            return
        header = stream.readline(min(budget, size))
        yield 1, 0, header
        if not header.endswith(b"\n"):
            return
        start = max(len(header), size - (budget - len(header)))
        stream.seek(start)
        tail = stream.read(min(budget - len(header), size - start))
        # The preceding byte is outside the budget, so discard the first fragment.
        end = tail.find(b"\n")
        if end < 0:
            return
        offset = start + end + 1
        for raw in tail[end + 1:].splitlines(keepends=True):
            yield -offset, offset, raw
            offset += len(raw)


def read_session(path: Path, agent: str, max_bytes: int = BYTE_LIMIT,
                 since_epoch: float | None = None,
                 imports: dict[str, datetime] | None = None, *,
                 exclude_automated: bool = False, recent: bool = False,
                 capture_full: bool = False, track_workspaces: bool = False) -> dict[str, Any] | None:
    if agent not in {"claude_code", "codex", "pi"} or max_bytes < 1:
        raise ValueError("choose a supported trace source and a positive byte limit")
    events: list[dict[str, Any]] = []
    workspaces: set[str] = set()
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    digest = hashlib.sha256()
    session, cwd, origin = path.stem, "", "unknown"
    last_closed = False
    copied_until = 0
    model: str | None = None
    active_turn: str | None = None
    active_turn_key: str | None = None
    recorded_context: dict[str, Any] | None = None
    used = 0
    ranges: list[list[int]] = []
    observation: dict[str, int] = {}
    prefix_omitted = False
    unknown_copy_boundary = False
    pi_previous: str | None = None
    pi_fork_time: datetime | None = None

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
             key: object = "", context: dict[str, Any] | None = None,
             tool_representation: str = "") -> None:
        nonlocal last_closed
        identity = str(call or key or line)
        event = trace_event(agent, session, kind, identity, value, record.get("timestamp"),
                            call=call, tool=tool, status=status, capture_full=capture_full)
        signature = event["source_sha256"]
        previous = seen.get((kind, identity))
        if previous is not None:
            counts["duplicate_records" if previous["source_sha256"] == signature
                   else "conflicting_records"] += 1
            if kind in {"call", "result"}:
                previous["tool_event_ambiguous"] = True
                emit("tool_gap", "Repeated tool record identity", line, record)
            return
        counts["truncated_events"] += event["truncated"]
        event.update({"line": line if line > 0 else None,
                      "byte_offset": -line if line < 0 else None,
                      **({"turn_context": context} if context is not None else {}),
                      **({"tool_representation": tool_representation}
                         if tool_representation else {}),
                      **({"native_turn_key": active_turn_key} if active_turn_key else {})})
        seen[kind, identity] = event
        events.append(event)
        if kind in {"call", "prompt"}:
            last_closed = False

    def prefix_records() -> Iterator[tuple[int, int, bytes]]:
        with path.open("rb") as stream:
            offset = 0
            for line in range(1, 1_000_001):
                raw = stream.readline(max_bytes - offset + 1)
                if not raw:
                    return
                yield line, offset, raw
                offset += len(raw)

    records = _recent_records(path, max_bytes, observation) if recent else prefix_records()
    for line, offset, raw in records:
        prefix_omitted = recent and observation["size"] > max_bytes
        if used + len(raw) > max_bytes:
            counts["byte_cap_reached"] = 1
            break
        if ranges and ranges[-1][1] == offset:
            ranges[-1][1] += len(raw)
        else:
            ranges.append([offset, offset + len(raw)])
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
        if track_workspaces:
            context = dict_or_empty(record.get("payload")) if agent == "codex" else record
            if (workspace := context.get("cwd")) and isinstance(workspace, str):
                workspaces.add(workspace)
        if agent == "pi":
            if line == 1:
                if kind != "session" or not isinstance(record.get("id"), str):
                    counts["source_incomplete"] = 1
                    continue
                session = record["id"]
                cwd = str(record.get("cwd") or "")
                if record.get("parentSession"):
                    pi_fork_time = parse_ts(record.get("timestamp"))
                    if pi_fork_time is None or pi_fork_time.tzinfo is None:
                        counts["source_incomplete"] = 1
                        break
                continue
            if pi_fork_time is not None:
                stamp = parse_ts(record.get("timestamp"))
                if stamp is None or stamp.tzinfo is None:
                    counts["source_incomplete"] = 1
                    continue
                if stamp <= pi_fork_time:
                    counts["copied_records"] += 1
                    continue
            if isinstance(entry := record.get("id"), str):
                if pi_previous is not None and record.get("parentId") != pi_previous:
                    counts["source_incomplete"] = 1
                pi_previous = entry
            if kind not in {"message", "compaction"} or not in_window(record):
                continue
            if kind == "compaction":
                emit("compaction", "context was compacted", line, record, key=entry)
                continue
            message = dict_or_empty(record.get("message"))
            role, content = message.get("role"), message.get("content", "")
            model = message.get("model") or model
            if role == "user":
                emit("prompt", content, line, record, key=entry)
            elif role == "toolResult":
                emit("result", content, line, record, call=message.get("toolCallId"),
                     tool=message.get("toolName"),
                     status=result_status(_text(content), message.get("isError")))
            elif role == "assistant":
                for index, block in enumerate(content if isinstance(content, list) else []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "toolCall":
                        emit("call", block.get("arguments", {}), line, record,
                             call=block.get("id"), tool=block.get("name"), key=f"{entry}:{index}")
                    elif block.get("type") == "text":
                        emit("reply", block.get("text", ""), line, record, key=f"{entry}:{index}")
                if message.get("stopReason") in {"stop", "aborted", "length"}:
                    last_closed = message["stopReason"] == "stop"
                    emit("boundary", "turn_complete" if last_closed else "turn_aborted",
                         line, record, key=entry)
            continue
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
                    ordinal = payload.get("subagent_history_start_ordinal")
                    unknown_copy_boundary = not isinstance(ordinal, int) or ordinal < 1
                    copied_until = ordinal if isinstance(ordinal, int) else 0
            if unknown_copy_boundary or (line < 0 and copied_until):
                counts["unknown_copy_boundary"] += 1
                continue
            if 0 < line <= copied_until:
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
                context_model, context_cwd = payload.get("model"), payload.get("cwd")
                if active_turn and active_turn == payload.get("turn_id"):
                    if (not isinstance(context_model, str) or not context_model
                            or not isinstance(context_cwd, str) or not Path(context_cwd).is_absolute()):
                        recorded_context = {"status": "ambiguous"}
                    elif recorded_context is None:
                        recorded_context = {
                            "status": "observed", "model": context_model, "cwd": context_cwd,
                            "record": {"line": line if line > 0 else None,
                                       "byte_offset": -line if line < 0 else None},
                        }
                    elif (recorded_context.get("model"), recorded_context.get("cwd")) != (
                            context_model, context_cwd):
                        recorded_context = {"status": "ambiguous"}
            elif kind == "response_item":
                call_id = payload.get("call_id")
                if sub in {"function_call", "custom_tool_call"}:
                    emit("call", payload.get("arguments", payload.get("input", "")),
                         line, record, call=call_id, tool=payload.get("name"),
                         tool_representation="model_call")
                elif sub in {"function_call_output", "custom_tool_call_output"}:
                    value = payload.get("output", "")
                    emit("result", value, line, record, call=call_id,
                         status=result_status(value))
                elif sub == "message" and payload.get("role") in {"user", "assistant"}:
                    emit("prompt" if payload["role"] == "user" else "reply",
                         payload.get("content", ""), line, record, key=payload.get("id"))
                elif sub in {"compaction", "context_compaction"}:
                    emit("compaction", "context was compacted", line, record)
                elif sub in _CODEX_UNCOUNTED_RESPONSE_TOOLS:
                    emit("tool_gap", "Unsupported Codex tool record", line, record)
            elif kind == "compacted":
                emit("compaction", "context was compacted", line, record)
            elif kind == "event_msg":
                item = dict_or_empty(payload.get("item"))
                if sub == "task_started":
                    turn_id = payload.get("turn_id")
                    active_turn = turn_id if isinstance(turn_id, str) and turn_id else None
                    active_turn_key = (hashlib.sha256(active_turn.encode()).hexdigest()[:32]
                                       if active_turn else None)
                    recorded_context = None
                elif sub == "user_message":
                    message = payload.get("message", "")
                    if codex.is_scheduled_automation(message):
                        origin = "automated"
                    emit("prompt", message, line, record)
                elif sub == "agent_message":
                    emit("reply", payload.get("message", ""), line, record)
                elif sub in {"task_complete", "turn_aborted"}:
                    last_closed = sub == "task_complete"
                    completed_turn = payload.get("turn_id")
                    completed_context = None
                    if completed_turn or active_turn:
                        completed_context = recorded_context if active_turn == completed_turn else None
                        completed_context = completed_context or {"status": "missing"}
                    emit("boundary", "turn_complete" if last_closed else "turn_aborted",
                         line, record, context=completed_context)
                    active_turn, active_turn_key, recorded_context = None, None, None
                elif sub == "item_completed" and item.get("type") == "CommandExecution":
                    identity = item.get("id") or f"command-{line}"
                    emit("call", item.get("command", ""), line, record,
                         call=identity, tool="exec_command", tool_representation="execution_item")
                    emit("result", {"exit_code": item.get("exit_code"),
                                   "output": item.get("aggregated_output", "")},
                         line, record, call=identity,
                         status=result_status({"exit_code": item.get("exit_code")}))
                elif sub == "item_completed" and item.get("type") == "McpToolCall":
                    identity = item.get("id") or f"mcp-{line}"
                    emit("call", item.get("arguments", {}), line, record, call=identity,
                         tool=item.get("tool") or item.get("name") or "mcp",
                         tool_representation="execution_item")
                    error = item.get("status") == "failed"
                    emit("result", item.get("error") if error else item.get("result", ""),
                         line, record, call=identity,
                         status="error" if error else "success" if item.get("status")
                         == "completed" else "unknown")
                elif sub == "item_completed" and item.get("type") in {"UserMessage", "AgentMessage"}:
                    emit("prompt" if item["type"] == "UserMessage" else "reply",
                         _codex_message_text(item) if item["type"] == "AgentMessage"
                         else item.get("content", item.get("text", "")), line, record,
                         key=item.get("id"))
                elif sub == "context_compacted" or (
                        sub == "item_completed" and item.get("type") == "ContextCompaction"):
                    emit("compaction", "context was compacted", line, record, key=item.get("id"))
                elif sub in {"web_search_end", "image_generation_end"} or (
                        sub == "item_completed"
                        and item.get("type") in _CODEX_UNCOUNTED_COMPLETED_ITEMS):
                    emit("tool_gap", "Unsupported Codex tool record", line, record)
            continue
        session = str(record.get("sessionId") or session)
        cwd = str(record.get("cwd") or cwd)
        if record.get("entrypoint"):
            origin = "interactive" if record["entrypoint"] == "cli" else "automated"
        if record.get("isSidechain"):
            origin = "subagent"
        if prefix_omitted and line == 1:
            continue
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

    if prefix_omitted:
        counts["byte_cap_reached"] = 1
        counts["prefix_omitted"] = 1
    calls = {e["call_id"] for e in events if e["kind"] == "call"}
    results = {e["call_id"] for e in events if e["kind"] == "result"}
    coverage: dict[str, Any] = dict(counts)
    coverage["tool_events_checked"] = True
    for name in ("partial_tail", "byte_cap_reached"):
        coverage[name] = bool(counts[name])
    coverage.update(calls=len(calls), matched_results=len(calls & results),
                    unmatched_calls=len(calls - results), orphan_results=len(results - calls),
                    truncated_events=counts["truncated_events"],
                    copied_records=counts["copied_records"])
    coverage["complete"] = last_closed and not any(coverage.get(k) for k in (
        "unmatched_calls", "orphan_results", "partial_tail", "byte_cap_reached",
        "truncated_events", "malformed_records", "conflicting_records", "missing_timestamps",
        "unknown_copy_boundary", "source_incomplete"))
    return {"version": VERSION, "agent": agent, "session_id": session, "cwd": cwd,
            "origin": origin, "model": model, "events": events, "coverage": coverage,
            **({"observed_workspaces": sorted(workspaces)} if track_workspaces else {}),
            "source": {"path": str(path), "sha256": digest.hexdigest(), "bytes": used,
                       "ranges": ranges,
                       "size": observation["size"] if recent else path.stat().st_size}}


def event_links(events: list[dict[str, Any]]) -> dict[str, str]:
    # Event references survive masking even when native call and run IDs do not.
    pending: dict[tuple[str, str], str] = {}
    links = {}
    for event in events:
        if not event.get("call_id"):
            continue
        key = (event.get("run_id", ""), event["call_id"])
        if event["kind"] == "call":
            pending[key] = event["id"]
        elif event["kind"] == "result" and key in pending:
            links[pending.pop(key)] = event["id"]
    return links
