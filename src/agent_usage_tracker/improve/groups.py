"""Group failed and split-vote judgments by deterministic signals, across audits and days.

Read-only over existing judgments: no model runs, and a group report holds ids, signal names
and counts, never evidence text."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import AutError
from ..event import parse_ts
from .rubric import command_family, tool_name
from .store import Store, digest, repository
from .studies import shortlist

# First match wins, so specific classes come before the generic ones.
_CLASSES = [(name, re.compile(pattern, re.M)) for name, pattern in (
    ("command_not_found", r"command not found"),
    ("no_such_file", r"no such file"),
    ("missing_module", r"no module named"),
    ("permission_denied", r"permission denied|operation not permitted"),
    ("timeout", r"timed out"),
    ("unknown_tool", r"unknown tool"),
    ("user_declined", r"tool use was rejected"),
    ("shell_glob", r"no matches found"),
    ("push_rejected", r"non-fast-forward|\[rejected\]|\[remote rejected\]"),
    ("conflict", r"merge conflict|^conflict \("),
    ("syntax_error", r"syntaxerror"),
    ("test_failure", r"assertionerror|^failed |<n> failed"),
    ("nonzero_exit", r"exit(?:ed with)? code:? <n>"),
)]
_NAME = re.compile(r"[A-Za-z0-9_.:+@ -]{1,60}")


def _normalise(line: str) -> str:
    line = line.lower()
    line = re.sub(r"\"[^\"]*\"|'[^']*'|`[^`]*`", "<q>", line)
    line = re.sub(r"(?:[a-z]:)?(?:~|\.{1,2})?(?:[/\\][^\s:;,()<>]+)+", "<path>", line)
    line = re.sub(r"\b[0-9a-f]{7,}\b", "<hex>", line)
    line = re.sub(r"\d+", "<n>", line)
    return " ".join(line.split())


def error_class(text: str) -> str:
    """A table name, or `other:` and 8 hex of the normalised first line; never the text."""
    lines = [line for line in map(_normalise, text.splitlines()) if line]
    if not lines:
        return ""
    # The whole text, not only the first line: Claude Code and Codex put `Exit code 1` first.
    whole = "\n".join(lines)
    return next((name for name, pattern in _CLASSES if pattern.search(whole)),
                "other:" + digest(lines[0])[:8])


def _bounded(name: str) -> str:
    # Tool and command names come from transcripts; keep only short plain names in a record.
    return name if not name or _NAME.fullmatch(name) else "other"


def _call(anchor: dict[str, Any], *sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not anchor.get("call_id"):
        return None
    return next((event for events in sources for event in events
                 if event["kind"] == "call" and event.get("call_id") == anchor["call_id"]), None)


def _anchor_signals(anchor: dict[str, Any] | None, *sources: list[dict[str, Any]]
                    ) -> tuple[str, str, str]:
    """(tool, error_class, command_family) of a result event, from the call it answers."""
    if anchor is None:
        return "", "", ""
    call = _call(anchor, *sources) or {}
    family = command_family(call["text"], call.get("tool", "")) if call.get("text") else ""
    return (_bounded(tool_name(anchor.get("tool") or call.get("tool", ""))),
            error_class(anchor.get("text", "")), _bounded(family))


def signals(judgment: dict[str, Any], case: dict[str, Any],
            session: dict[str, Any]) -> dict[str, str]:
    review = judgment.get("review") or {}
    events = case["events"]
    by_id = {event["id"]: event for event in events}
    cited = [by_id[ref] for ref in review.get("evidence_ids", [])
             if ref in by_id and by_id[ref]["kind"] == "result"]
    errors = [e for e in events if e["kind"] == "result" and e.get("status") == "error"]
    anchor = cited[0] if cited else errors[-1] if errors else None
    tool, error, family = _anchor_signals(anchor, events, session.get("events", []))
    return {"rubric": judgment["rubric"], "cause": review.get("cause") or "unreviewed",
            "tool": tool, "error_class": error, "command_family": family}


def _journal(store: Store) -> list[dict[str, Any]]:
    try:
        lines = (store.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _work_day(case: dict[str, Any]) -> date | None:
    """Local date of the case's first timestamped event."""
    for event in case["events"]:
        stamp = parse_ts(event.get("timestamp"))
        if stamp is not None:
            return stamp.astimezone().date()
    return None


def _flagged(judgment: dict[str, Any]) -> bool:
    # aggregate() stored each vote's label with the rubric threshold already applied.
    votes = judgment.get("votes")
    return judgment.get("verdict") == "unknown" and isinstance(votes, list) and "fail" in votes


def _audit_incidents(store: Store, rows: list[dict[str, Any]]) -> tuple[str | None, Counter[Any]]:
    """Error results of the newest scheduled (not project) audit, counted by anchor signals."""
    for row in reversed(rows):
        if row.get("kind") != "audit" or not isinstance(row.get("audit_id"), str):
            continue
        if store.get("audits", row["audit_id"]).get("project"):
            continue
        counts: Counter[Any] = Counter()
        for incident in shortlist(store, row["audit_id"])["incidents"]:
            events = store.get("sessions", incident["snapshots"][0])["events"]
            event = next(e for e in events if e["id"] == incident["event_id"])
            counts[_anchor_signals(event, events)] += 1
        return row["audit_id"], counts
    return None, Counter()


def refresh(store: Store, *, window_days: int = 14, threshold: int = 3,
            today: date | None = None) -> dict[str, Any]:
    if not 1 <= window_days <= 366 or not 1 <= threshold <= 1000:
        raise AutError("improve: groups need 1..366 days and a threshold of 1..1000")
    today = today or date.today()
    start = today - timedelta(days=window_days - 1)
    rows = _journal(store)
    # Judgments carry no time; the last diagnosis that listed one orders same-version ties.
    order = {key: index for index, row in enumerate(rows) if row.get("kind") == "diagnosis"
             for key in row.get("judgment_ids") or []}
    newest: dict[tuple[str, str], tuple[tuple[int, int, str], dict[str, Any]]] = {}
    skipped = 0
    for key in store.keys("judgments"):
        try:
            row = store.get("judgments", key)
            version = row.get("rubric_version")
            rank = (version if isinstance(version, int) else 0, order.get(key, -1), key)
            slot = (row["case_id"], row["rubric"])
        except (AutError, KeyError, TypeError):
            skipped += 1
            continue
        if slot not in newest or rank > newest[slot][0]:
            newest[slot] = (rank, {**row, "judgment_id": key})
    cases: dict[str, dict[str, Any]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    projects: dict[str, str] = {}
    found: dict[str, dict[str, Any]] = {}
    for _, row in sorted(newest.values(), key=lambda item: item[0][2]):
        if row.get("verdict") != "fail" and not _flagged(row):
            continue
        try:
            case_id = row["case_id"]
            case = cases[case_id] = cases.get(case_id) or store.get("cases", case_id)
            day = _work_day(case)
            if day is None or not start <= day <= today:     # skewed clocks: no future days
                continue
            snapshot = case["session_snapshot"]
            session = sessions[snapshot] = sessions.get(snapshot) or store.get("sessions", snapshot)
            values = signals(row, case, session)
            task = digest([session["agent"], session["session_id"]])    # studies.identity()
            cwd = str(session.get("cwd") or "")
            if cwd not in projects:
                repo = repository(Path(cwd)) if cwd else None
                projects[cwd] = Path(repo).parent.name if repo else "(none)"
        except (AutError, KeyError, TypeError, ValueError):
            skipped += 1
            continue
        key = digest(values)
        group = found.setdefault(key, {"group_key": key, "signals": values, "judgment_ids": set(),
                                       "case_ids": set(), "tasks": set(), "projects": {},
                                       "days": set()})
        group["judgment_ids"].add(row["judgment_id"])
        group["case_ids"].add(case_id)
        group["tasks"].add(task)
        group["projects"].setdefault(projects[cwd], set()).add(task)
        group["days"].add(day.isoformat())
    try:
        audit_id, incidents = _audit_incidents(store, rows)
    except (AutError, KeyError, TypeError, StopIteration):
        audit_id, incidents = None, Counter()
    groups = []
    for group in found.values():
        values = group["signals"]
        groups.append({"group_key": group["group_key"], "signals": values,
            "judgment_ids": sorted(group["judgment_ids"]), "case_ids": sorted(group["case_ids"]),
            "distinct_tasks": len(group["tasks"]),
            "projects": {name: len(tasks) for name, tasks in sorted(group["projects"].items())},
            "first_work_day": min(group["days"]), "last_work_day": max(group["days"]),
            "recurring": len(group["tasks"]) >= threshold,
            "audit_incidents": incidents[(values["tool"], values["error_class"],
                                          values["command_family"])]})
    groups.sort(key=lambda g: g["group_key"])
    groups.sort(key=lambda g: (g["recurring"], g["distinct_tasks"], g["last_work_day"]),
                reverse=True)
    report = {"version": 1, "day": today.isoformat(), "window_days": window_days,
              "threshold": threshold, "audit_id": audit_id, "skipped": skipped, "groups": groups}
    report_id = store.put("group_reports", report)
    store.append("groups", report_id=report_id, groups=len(groups),
                 recurring=sum(g["recurring"] for g in groups), skipped=skipped)
    return {"report_id": report_id, **report}


def latest(store: Store) -> dict[str, Any] | None:
    """The newest group report, found through the journal."""
    row = next((row for row in reversed(_journal(store)) if row.get("kind") == "groups"), None)
    return store.get("group_reports", row["report_id"]) if row else None
