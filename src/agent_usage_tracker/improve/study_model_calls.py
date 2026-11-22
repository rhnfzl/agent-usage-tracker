"""Verify a native Codex tool-request count for an ordinary-work attempt."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..config import AutError
from ..event import parse_ts
from .evidence import source_signature
from .store import digest

_MISSING_COVERAGE = ("malformed_records", "missing_timestamps", "divergent_copy",
                     "source_incomplete", "unknown_copy_boundary")


def native_count(session: dict[str, Any], events: list[dict[str, Any]], coverage: dict[str, Any]
                 ) -> tuple[int, datetime] | None:
    if (session["agent"] != "codex" or session.get("origin") != "interactive"
            or not session.get("session_id") or not events
            or any(coverage.get(gap) for gap in _MISSING_COVERAGE)):
        return None
    end = events[-1]
    turn = end.get("native_turn_key")
    receipt = end.get("codex_model_call_receipt")
    if (end["kind"] != "boundary" or end.get("text") != "turn_complete"
            or (end.get("turn_context") or {}).get("status") != "observed"
            or events[0]["kind"] != "prompt" or not turn
            or any(event.get("native_turn_key") != turn for event in events)
            or not isinstance(receipt, dict) or receipt.get("thread_origin") != "interactive"):
        return None
    count, started = receipt.get("count"), parse_ts(receipt.get("started_at"))
    if type(count) is not int or count < 1 or not started or not started.tzinfo:
        return None
    # The receipt covers the whole turn, including work before the selected prompt.
    opening = events[0].get("source_sha256", events[0].get("text"))
    worked = False
    for event in session["events"]:
        if event.get("native_turn_key") != turn:
            continue
        kind = event["kind"]
        if kind == "prompt" and (worked or event.get("source_sha256", event.get("text")) != opening):
            return None
        worked = worked or kind in {"call", "result", "reply"}
    for event in events:
        stamp = parse_ts(event.get("timestamp"))
        if not stamp or not stamp.tzinfo or stamp < started:
            return None
    return count, started


def t3_count(session: dict[str, Any], events: list[dict[str, Any]], coverage: dict[str, Any],
             origin: tuple[str, str, str], lookup: Callable[[str], dict[str, Any] | None]
             ) -> tuple[int, datetime] | None:
    prompt, end = events[0], events[-1]
    source, source_end, turn = origin
    run = end.get("run_id")
    if (session.get("origin") != "interactive" or not session.get("session_id")
            or prompt["kind"] != "prompt" or not run or end.get("text") != "turn_complete"
            or any(coverage.get(gap) for gap in _MISSING_COVERAGE)
            or any(event.get("run_id") != run for event in events)
            or sum(event["kind"] == "prompt" and event.get("run_id") == run
                   for event in session["events"]) != 1):
        return None
    text = prompt.get("text")
    if not isinstance(text, str) or not text or prompt.get("truncated") or digest(text) != prompt.get("source_sha256"):
        return None
    try:
        native = lookup(source)
    except AutError:
        return None
    if not native or native.get("session_id") != session.get("native_runs", {}).get(run, {}).get("session_id"):
        return None
    native_events = [event for event in native["events"] if event.get("native_turn_key") == turn]
    if (not native_events or native_events[-1]["id"] != source_end
            or sum(event["kind"] == "boundary" for event in native_events) != 1):
        return None
    native_end = native_events[-1]
    context = end["turn_context"]
    if any((native_end.get("turn_context") or {}).get(key) != context.get(key) for key in ("model", "cwd")):
        return None
    measured = native_count(native, native_events, native["coverage"])
    if not measured:
        return None
    opened, finished, native_finished = (parse_ts(event.get("timestamp")) for event in (prompt, end, native_end))
    if not (opened and opened.tzinfo and finished and finished.tzinfo
            and native_finished and native_finished.tzinfo and opened <= measured[1] <= native_finished <= finished):
        return None
    signature = source_signature(text, "unknown", "")
    worked = False
    for event in native_events:
        if event["kind"] == "prompt" and (worked or event.get("truncated")
                                         or event.get("source_sha256") != signature):
            return None
        worked = worked or event["kind"] in {"call", "result", "reply"}
    return measured
