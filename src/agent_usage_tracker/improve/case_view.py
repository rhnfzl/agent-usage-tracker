"""Inspect frozen local evidence without changing model eligibility or opening raw sources."""

from __future__ import annotations

import hashlib
from html import escape
from pathlib import Path
from typing import Any

from ..config import AutError
from ..state import atomic_write_text
from .retries import FINGERPRINT, references, retries
from .store import Store

_READ_TOOLS = {"read", "read_file"}


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


def _link(index: int, label: str) -> str:
    return f'<a href="#event-{index}">{escape(label)}</a>'


def _context(events: list[dict[str, Any]], session: dict[str, Any],
             snapshot: Path, revision: str) -> str:
    context_bytes = 0
    output_bytes = None
    sizes: list[str] = []
    compactions: list[str] = []
    reads: dict[tuple[str, str], dict[str, int]] = {}
    for index, event in enumerate(events):
        kind = event["kind"]
        if kind in {"prompt", "result"} and isinstance(event.get("text"), str):
            size = len(event["text"].encode("utf-8", "replace"))
            context_bytes += size
            if kind == "result":
                output_bytes = (output_bytes or 0) + size
            linked = _link(index, "User request" if kind == "prompt" else "Tool output")
            clipped = " (clipped)" if event.get("truncated") else ""
            sizes.append(f"<tr><td>{linked}</td><td>{size:,}{clipped}</td>"
                         f"<td>{context_bytes:,}</td></tr>")
        elif kind == "compaction":
            compactions.append(_link(index, f"Compaction {len(compactions) + 1}"))
        elif kind == "call":
            tool = str(event.get("tool", "")).casefold()
            fingerprint, call_id = event.get("source_sha256"), event.get("call_id")
            if (tool in _READ_TOOLS and isinstance(call_id, str) and call_id
                    and isinstance(fingerprint, str) and FINGERPRINT.fullmatch(fingerprint)):
                reads.setdefault((tool, fingerprint), {}).setdefault(call_id, index)

    output_report = ("<p>No tool output size is available in this capture.</p>"
                     if output_bytes is None else
                     f"<p>Captured tool output <strong>{output_bytes:,} UTF-8 bytes</strong>.</p>")
    size_report = (
        "<table><thead><tr><th scope='col'>Captured event</th><th scope='col'>Bytes</th>"
        "<th scope='col'>Cumulative bytes</th></tr></thead><tbody>"
        + "".join(sizes) + "</tbody></table>"
        if sizes else "<p>Prompt and tool output sizes are unavailable in this capture.</p>"
    )
    repeated = [", ".join(_link(index, f"Read {n}") for n, index in enumerate(calls.values(), 1))
                for calls in reads.values() if len(calls) > 1]
    repeat_report = (f"<ul>{''.join(f'<li>{links}</li>' for links in repeated)}</ul>" if repeated
                     else "<p>No repeated explicit file read was observed.</p>")
    if compactions:
        compaction_report = f"<p>{len(compactions):,} observed. {', '.join(compactions)}</p>"
    elif session.get("agent") in {"claude_code", "codex", "pi", "cursor_cli"}:
        compaction_report = "<p>No explicit compaction was observed in this attempt.</p>"
    else:
        compaction_report = "<p>Compaction capture is unsupported for this evidence stream.</p>"
    coverage = session.get("coverage")
    incomplete = ("<p>Source capture is incomplete. Missing records can hide events.</p>"
                  if not isinstance(coverage, dict) or not coverage.get("complete") else "")
    clipped_report = ("<p>Clipped text contributes only the bytes saved in this revision.</p>"
                      if any(event.get("truncated") for event in events) else "")
    return (
        '<section aria-labelledby="context-growth"><h2 id="context-growth">Context growth</h2>'
        '<p>Saved prompt and tool result sizes estimate their contribution to context. '
        'Actual model context tokens are unknown.</p>'
        f'<p>Frozen session revision <a href="{escape(snapshot.as_uri(), quote=True)}">'
        f'<code>{escape(revision)}</code></a>.</p>'
        + incomplete + clipped_report + output_report + size_report
        + "<h3>Repeated file reads</h3><p>Matches use an exact request fingerprint from explicit "
        "file-read tools. Shell commands and other read tools are outside this check.</p>"
        + repeat_report + "<h3>Explicit compactions</h3>" + compaction_report + "</section>"
    )


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
    repeats = list(retries(events, session.get("retry_keys")))
    retry_rows = []
    for first, error, retry, result, match in repeats:
        outcome = (_link(result, str(events[result].get("status", "unknown")))
                   if result is not None else "Result unavailable")
        label = "Repeated request" if match == "identical" else "Changed arguments for same target"
        links = (_link(first, events[first]["tool"]), _link(error, "Error"), _link(retry, label), outcome)
        retry_rows.append("<li>" + " · ".join(links) + "</li>")
    source = session.get("source", {})
    if not isinstance(source, dict):
        raise AutError("improve: source metadata is not a source record")
    path = Path(str(source.get("path", "")))
    source_link = (f'<a href="{escape(path.as_uri(), quote=True)}">Original local trace</a>'
                   if path.is_absolute() else "Original trace link unavailable")
    supplements = []
    for key in session.get("supplemental_sources", []):
        store.get("sessions", key)
        link = store.path("sessions", key).as_uri()
        supplements.append(f'<p><a href="{escape(link, quote=True)}">Linked native snapshot</a>'
                           ' (supplemental evidence, not a separate judge observation).</p>')
    blocks = []
    labels = {"prompt": "User request", "call": "Tool call", "result": "Tool result",
              "reply": "Assistant reply", "boundary": "Turn boundary", "compaction": "Compaction"}
    for index, event in enumerate(events):
        seen = event["id"] in selected
        location = (f"line {event['line']}" if event.get("line") is not None else
                    f"byte {event['byte_offset']}" if event.get("byte_offset") is not None else
                    f"{event['table']} record {event['record_id']}" if event.get("record_id") else
                    "source position unavailable")
        title = labels.get(event["kind"], str(event["kind"]))
        linked = event.get("source_result")
        provenance = ""
        if linked and linked["session_id"] in session.get("supplemental_sources", []):
            native = store.path("sessions", linked["session_id"]).as_uri()
            provenance = (f'<p class="reference">Native result <a href="{escape(native, quote=True)}">'
                          f'{escape(linked["event_id"])}</a></p>')
        details = "Used in model input" if seen else "Captured context omitted from model input"
        if event.get("truncated"):
            details += "; body clipped in the frozen snapshot"
        blocks.append(f'<section id="event-{index}" class="{"selected" if seen else "context"}">'
                      f'<h2>{escape(title)}</h2><p>{escape(details)}</p>'
                      f'<p class="reference">{escape(location)} · event '
                      f'<code>{escape(str(event["id"]))}</code> · '
                      f'{escape(str(event.get("status", "unknown")))}</p>{provenance}'
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
            'th,td{text-align:left;padding:.35rem .55rem;border-bottom:1px solid #c6cfdb}'
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
            + ''.join(supplements) + '</details>'
            + '<section><h2>Requests repeated after a tool error</h2>'
            + ("<ul>" + "".join(retry_rows) + "</ul>" if repeats else
               "<p>No matching sequence in the captured attempt.</p>")
            + "</section>"
            + _context(events, session, snapshot, case["session_snapshot"])
            + ''.join(blocks) + '</main></body></html>')
    target = store.path("case_views", case_id).with_suffix(".html")
    atomic_write_text(target, page)
    return {"case_id": case_id, "path": str(target), "captured_events": len(events),
            "model_events": len(selected), "omitted_model_events": omitted,
            "clipped_events": clipped, "eligible": eligible, "private": True,
            "repeated_after_error": sum(match[-1] == "identical" for match in repeats),
            "changed_arguments_after_error": sum(match[-1] == "changed_arguments" for match in repeats),
            "repeat_events": [references(events, match) for match in repeats[:100]],
            "html_sha256": hashlib.sha256(page.encode()).hexdigest()}
