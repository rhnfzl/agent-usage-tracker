"""Inspect frozen local evidence without changing model eligibility or opening raw sources."""

from __future__ import annotations

import hashlib
from html import escape
from pathlib import Path
from typing import Any

from ..config import AutError
from ..state import atomic_write_text
from .store import Store


def _attempt(session: dict[str, Any], case: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        events, selected = session["events"], case["events"]
        if not isinstance(events, list) or not isinstance(selected, list) or not selected \
                or len({event["id"] for event in events}) != len(events) \
                or len({event["id"] for event in selected}) != len(selected):
            raise ValueError
        indices = [events.index(event) for event in selected]
        if indices != sorted(indices):
            raise ValueError
        first = indices[0]
        start = max((i + 1 for i in range(first) if events[i]["kind"] == "boundary"), default=0)
        start = max((i for i in range(start, first + 1) if events[i]["kind"] == "prompt"),
                    default=start)
        end = next((i if events[i]["kind"] == "prompt" else i + 1
                    for i in range(start + 1, len(events))
                    if events[i]["kind"] in {"prompt", "boundary"}), len(events))
        if indices[-1] >= end:
            raise ValueError
        return list(events[start:end])
    except (KeyError, TypeError, ValueError) as exc:
        raise AutError("improve: case does not match one captured source attempt") from exc


def write_case_view(store: Store, case_id: str) -> dict[str, Any]:
    case = store.get("cases", case_id)
    try:
        session = store.get("sessions", case["session_snapshot"])
    except KeyError as exc:
        raise AutError("improve: case has no source snapshot") from exc
    events = _attempt(session, case)
    selected = {event["id"] for event in case["events"]}
    omitted = len(events) - len(selected)
    clipped = sum(bool(event.get("truncated")) for event in events)
    eligible = bool(case.get("coverage", {}).get("complete") and not omitted
                    and not case.get("omitted_task_events") and not clipped)
    source = session.get("source", {})
    if not isinstance(source, dict):
        raise AutError("improve: source metadata is not a source record")
    path = Path(str(source.get("path", "")))
    source_link = (f'<a href="{escape(path.as_uri(), quote=True)}">Original local trace</a>'
                   if path.is_absolute() else "Original trace link unavailable")
    blocks = []
    labels = {"prompt": "User request", "call": "Tool call", "result": "Tool result",
              "reply": "Assistant reply", "boundary": "Turn boundary", "compaction": "Compaction"}
    for index, event in enumerate(events):
        seen = event["id"] in selected
        location = (f"line {event['line']}" if event.get("line") is not None else
                    f"byte {event['byte_offset']}" if event.get("byte_offset") is not None else
                    "source position unavailable")
        title = labels.get(event["kind"], str(event["kind"]))
        details = "Used in model input" if seen else "Captured context omitted from model input"
        if event.get("truncated"):
            details += "; body clipped in the frozen snapshot"
        blocks.append(f'<section id="event-{index}" class="{"selected" if seen else "context"}">'
                      f'<h2>{escape(title)}</h2><p>{escape(details)}</p>'
                      f'<p class="reference">{escape(location)} · event '
                      f'<code>{escape(str(event["id"]))}</code> · '
                      f'{escape(str(event.get("status", "unknown")))}</p>'
                      f'<pre>{escape(str(event.get("text", "")))}</pre></section>')
    status = ("Model input passes basic completeness checks; this does not establish task success."
              if eligible else "Model input remains incomplete; this view does not upgrade a judgment.")
    snapshot = store.path("sessions", case["session_snapshot"])
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer">'
            '<title>AUT private case evidence</title><style>'
            'body{font:16px/1.6 system-ui;margin:0;background:#f6f8fb;color:#172033}'
            'main{max-width:64rem;margin:auto;padding:1.5rem}h1{line-height:1.2}'
            'section{background:white;padding:1rem;margin:1rem 0;border-left:4px solid #aab4c2}'
            '.selected{border-color:#226fb2}.status{padding:1rem;background:#fff1cf}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.9rem}'
            'code,.reference{overflow-wrap:anywhere}.reference{font-size:.85rem;color:#43516a}'
            'a{color:#175b94}a:focus-visible{outline:3px solid #175b94}'
            '</style></head><body><main><h1>Private case evidence</h1>'
            '<p>Review the captured request, commands, recovery and final reply together.</p>'
            f'<p class="status">{escape(status)}</p>'
            f'<p>Captured events: {len(events)}. Model input events: {len(selected)}. '
            f'Omitted from model input: {omitted}. Clipped bodies: {clipped}.</p>'
            '<details><summary>Source references</summary>'
            '<p>This view uses the frozen snapshot. The original link opens the current local file.</p>'
            f'<p>{source_link}</p><p><code>{escape(str(source.get("path", "unavailable")))}</code></p>'
            f'<p>Recorded source hash: <code>{escape(str(source.get("sha256", "unavailable")))}</code></p>'
            f'<p><a href="{escape(snapshot.as_uri(), quote=True)}">Frozen source snapshot</a></p>'
            '</details>' + ''.join(blocks) + '</main></body></html>')
    target = store.path("case_views", case_id).with_suffix(".html")
    atomic_write_text(target, page)
    return {"case_id": case_id, "path": str(target), "captured_events": len(events),
            "model_events": len(selected), "omitted_model_events": omitted,
            "clipped_events": clipped, "eligible": eligible, "private": True,
            "html_sha256": hashlib.sha256(page.encode()).hexdigest()}
