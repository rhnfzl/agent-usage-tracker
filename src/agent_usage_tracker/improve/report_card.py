"""Let the operator inspect private improvement counts without a model or sink."""

from __future__ import annotations

import html
import json
import re
import tomllib
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from .. import coverage
from ..config import DEFAULT_LEDGER_TZ, AutError, _zone, config_path
from ..event import parse_ts
from ..state import atomic_write_text, load_json_dict
from . import decide, groups, judge, qualify, scores
from .store import Store, digest

KINDS = ("audits", "sessions", "cases", "judgments", "proposals", "trial_results",
         "adoptions", "human_labels", "decisions")
NOTICE = ("Advisory sampled judgments do not measure prevalence. "
          "Before/after counts, not a causal estimate. Benefit and payback remain unproven.")


def configured_zone() -> tzinfo:
    """Read the ledger setting alone, without resolving transport secrets."""
    path = config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise AutError("improve: report-card config is not readable TOML") from exc
    name = data.get("ledger_timezone", DEFAULT_LEDGER_TZ)
    if not isinstance(name, str):
        raise AutError("improve: ledger_timezone must be a string")
    return _zone(name)


def _day(value: Any, zone: tzinfo) -> date | None:
    stamp = parse_ts(value)
    if stamp is None:
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(zone).date()


def _case_day(case: dict[str, Any], zone: tzinfo) -> date | None:
    return next((day for e in case["events"] if (day := _day(e.get("timestamp"), zone))), None)


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {name: sum(row[name] for row in rows) for name in ("passed", "failed", "unknown")}


def _rate(counts: dict[str, int], minimum: int = 1) -> str:
    known = counts["passed"] + counts["failed"]
    return f"{counts['failed'] / known:.1%} (n={known}, unknown={counts['unknown']})" \
        if known >= minimum else f"too few cases (n={known}, unknown={counts['unknown']})"


def _enum(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else "other"


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None


def _rubric_id(value: str) -> str:
    return value if re.fullmatch(r"[a-z_]{1,40}", value) else "other"


def _coverage(store: Store, journal: list[dict[str, Any]], zone: tzinfo,
              start: date, today: date) -> dict[str, Any]:
    audit = next((store.get("audits", row["audit_id"]) for row in reversed(journal)
                  if row.get("kind") == "audit"), None)
    saved = load_json_dict(coverage.report_path())
    evidence = {store.get("sessions", key).get("agent") for key in store.keys("sessions")}
    usage = {row["collector"] for row in saved.get("collectors", [])
             if row.get("observation", {}).get("usage_events", 0)}
    newest = {}
    for rubric in judge._rubrics():
        for row in qualify.judgments(store, rubric):
            newest[(row["case_id"], rubric.id)] = row
    selected = [row for row in newest.values()
                if (day := _case_day(store.get("cases", row["case_id"]), zone)) is not None
                and start <= day <= today]
    raw = (audit or {}).get("coverage", {})
    return {"audit_available": audit is not None,
            "audit_counts": {name: raw.get(name, 0) for name in (
                "complete_sessions", "unread_sources", "unreadable_sources", "truncated_events",
                "automated_sources", "active_sources", "malformed_records")},
            "coverage_available": bool(saved),
            "open_issues": len(saved.get("issues", [])) if saved else None,
            "usage_without_evidence": sorted(_enum(v, ("pi", "cursor", "cursor_api", "cursor_cli",
                "codex", "claude_code", "t3")) for v in usage - evidence),
            "judgments": len(selected),
            "incomplete_evidence": sum(r.get("reason") == "incomplete_evidence" for r in selected)}


def _cost(journal: list[dict[str, Any]], start: date, today: date,
          zone: tzinfo) -> dict[str, Any]:
    rows = [r for r in journal if (day := _day(r.get("at"), zone)) is not None
            and start <= day <= today]
    calls = [r for r in rows if r.get("kind") == "model_call"]
    # A timed load is a separate journal row, not an inference call.
    return {"model_calls": sum(r.get("started_at") is not None or r.get("infer_s") is not None
                                for r in calls),
            "load_records": sum(r.get("load_s") is not None for r in calls),
            "untimed_call_records": sum(r.get("infer_s") is None and r.get("load_s") is None
                                        for r in calls),
            "diagnosis_reported_calls": sum(r.get("calls", 0) for r in rows
                                             if r.get("kind") == "diagnosis"),
            "diagnoses": sum(r.get("kind") == "diagnosis" for r in rows),
            "decisions": sum(r.get("kind") == "decision" for r in rows),
            "labels": sum(r.get("kind") == "label" for r in rows),
            "seconds": sum((r.get("load_s") or 0) + (r.get("infer_s") or 0) for r in calls)}


def _tasks(store: Store, signals: dict[str, Any], start: date, end: date,
           today: date, zone: tzinfo, group_key: str | None = None) -> int | None:
    match = {k: v for k, v in signals.items()
             if k in {"tool", "error_class", "command_family"} and v}
    members: set[str] | None = None
    if group_key:
        reports = [store.get("group_reports", key) for key in store.keys("group_reports")]
        known = [g for r in reports for g in r["groups"] if g["group_key"] == group_key]
        if not known:
            return None
        members = {key for g in known for key in g["case_ids"]}
    if members is None and not match:
        return None
    found = set()
    for key in store.keys("cases"):
        if members is not None and key not in members:
            continue
        case = store.get("cases", key)
        day = _case_day(case, zone)
        if day is None or not start <= day <= min(end, today):
            continue
        session = store.get("sessions", case["session_snapshot"])
        values = groups.signals({"rubric": ""}, case, session)
        if members is not None or all(values[k] == v for k, v in match.items()):
            found.add(digest([session["agent"], session["session_id"]]))
    return len(found)


def build(store: Store, days: int = 28, today: date | None = None, *,
          zone: tzinfo | None = None) -> dict[str, Any]:
    """Read stored evidence; qualification refresh belongs to write(), not this calculation."""
    if type(days) is not int or not 1 <= days <= 366:
        raise AutError("improve: report-card days must be 1..366")
    zone = zone or configured_zone()
    today = today or datetime.now(zone).date()
    start, previous_end = today - timedelta(days=days - 1), today - timedelta(days=days)
    journal = groups._journal(store)
    current = scores.daily(store, days=days, today=today, zone=zone)
    previous = scores.daily(store, days=days, today=previous_end, zone=zone)
    rubrics = judge._rubrics()
    slots = {(r.id, r.version, "all", "control") for r in rubrics}
    slots.update((r["rubric_id"], r["rubric_version"], r["harness"], r["trigger"])
                 for r in current + previous)
    rubric_rows = []
    for rid, version, harness, trigger in sorted(slots):
        windows = [_counts([r for r in rows if (r["rubric_id"], r["rubric_version"],
                    r["harness"], r["trigger"]) == (rid, version, harness, trigger)])
                   for rows in (current, previous)]
        if harness == "all" and any(r["rubric_id"] == rid and r["rubric_version"] == version
                                     for r in current + previous):
            continue
        rubric_rows.append({"rubric_id": _rubric_id(rid), "rubric_version": version,
            "harness": _enum(harness, ("all", "codex", "claude_code", "t3", "pi", "cursor",
                                        "cursor_cli")),
            "trigger": _enum(trigger, ("control", "error_result", "merge")),
            "current": windows[0], "previous": windows[1]})
    qualifications = []
    for rubric in rubrics:
        rows = [store.get("qualifications", key) for key in store.keys("qualifications")]
        latest = max((r for r in rows if (r["rubric_id"], r["rubric_version"]) ==
                      (rubric.id, rubric.version)), key=lambda r: r["computed_at"], default={})
        qualifications.append({"rubric_id": rubric.id, "rubric_version": rubric.version,
            "qualified": latest.get("qualified", False), "fail_labels": latest.get("fail_labels", 0),
            "minimum": rubric.qualify["min_fail_labels"], "counts": latest.get("counts", {})})
    proposals = {key: store.get("proposals", key) for key in store.keys("proposals")}
    private: dict[str, Any] = {"window": f"{start} to {today}",
        "previous_window": f"{previous_end - timedelta(days=days - 1)} to {previous_end}",
        "proposals": {}, "groups": {}, "adoptions": {}}
    prepared = {r["proposal_id"]: _day(r.get("at"), zone) for r in journal
                if r.get("kind") == "proposal_prepared"}
    open_rows = []
    for key in decide.open_proposals(store):
        row = proposals[key]
        born = prepared.get(key)
        short = decide.short_id(store, key)
        open_rows.append({"proposal_id": key, "status": "open",
                          "age_days": max(0, (today - born).days) if born else None,
                          "staged": decide.staged_at(store, key) is not None})
        private["proposals"][key] = {"title": row.get("title") or "Operator proposal",
            "owner": row.get("owner_component") or "unspecified",
            "accept": f"{decide.command(store)} inbox accept {short}",
            "reject": f'{decide.command(store)} inbox reject {short} --reason "..."'}
    report = groups.latest(store) or {}
    status = decide.group_status(store)
    group_rows = []
    for row in [g for g in report.get("groups", []) if g["recurring"]][:5]:
        key = row["group_key"]
        state = status.get(key, "open")
        group_rows.append({"group_key": _id(key), "status": "rejected" if
            state.startswith("rejected on ") else _enum(state, ("open", "proposed", "accepted",
            "adopted")), "distinct_tasks": row["distinct_tasks"],
            "projects": len(row["projects"]), "audit_incidents": row["audit_incidents"]})
        private["groups"][key] = next((p["title"] for p in proposals.values()
            if p.get("group_key") == key and p.get("title")),
            " / ".join(str(v) for v in row["signals"].values() if v))
    private["group_window"] = f"{report.get('window_days', 0)} days up to {report.get('day', 'unknown')}"
    adoptions = []
    for key in store.keys("adoptions"):
        row = store.get("adoptions", key)
        adopted = date.fromisoformat(row["adopted_at"])
        group: dict[str, Any] = next((g for g in report.get("groups", [])
                      if g["group_key"] == row.get("group_key")), {})
        signals = row.get("signals") or group.get("signals") or {}
        private["adoptions"][key] = row["title"]
        for rid in row["rubric_ids"]:
            before = scores.daily(store, days=14, today=adopted - timedelta(days=1), zone=zone)
            after = scores.daily(store, days=14, today=adopted + timedelta(days=14), zone=zone)
            versions = {r["rubric_version"] for r in before + after if r["rubric_id"] == rid}
            versions.update(r.version for r in rubrics if r.id == rid)
            for version in sorted(versions):
                counts = [_counts([r for r in rows if r["rubric_id"] == rid and
                    r["rubric_version"] == version and r["trigger"] == "control"
                    and date.fromisoformat(r["day"]) <= today]) for rows in (before, after)]
                adoptions.append({"adoption_id": key, "rubric_id": _rubric_id(rid),
                    "rubric_version": version, "before": counts[0], "after": counts[1],
                    "tasks_before": _tasks(store, signals, adopted - timedelta(days=14),
                                           adopted - timedelta(days=1), today, zone, row.get("group_key")),
                    "tasks_after": _tasks(store, signals, adopted + timedelta(days=1),
                                          adopted + timedelta(days=14), today, zone, row.get("group_key"))})
    cycle = load_json_dict(store.root / "last_cycle.json")
    progress = cycle.get("diagnosis", cycle.get("progress", {}).get("diagnosis", {}))
    cost = _cost(journal, start, today, zone)
    private["model_seconds"] = cost.pop("seconds")
    dated_proposals = [p for k, p in proposals.items()
                       if (born := prepared.get(k)) is not None and start <= born <= today]
    private["drafter_cost_usd"] = sum((p.get("drafter") or {}).get("cost_usd") or 0
                                      for p in dated_proposals)
    private["drafter_unknown_cost"] = sum((p.get("drafter") or {}).get("cost_usd") is None
                                          for p in dated_proposals if p.get("drafter"))
    return {"days": days, "records": {k: len(store.keys(k)) for k in KINDS},
            "cycle": {"status": _enum(cycle.get("status", "missing"),
                ("missing", "ok", "deferred", "timeout", "error", "budget")),
                **{k: progress.get(k, 0) for k in
                   ("finished", "total", "optional_finished", "optional_total", "calls")}},
            "qualifications": qualifications, "rubrics": rubric_rows, "groups": group_rows,
            "proposals": open_rows, "adoptions": adoptions,
            "coverage": _coverage(store, journal, zone, start, today), "cost": cost,
            "private": private}


def to_json(card: dict[str, Any]) -> str:
    public = {k: v for k, v in card.items() if k != "private"}

    def check(value: Any, field: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                check(item, key)
        elif isinstance(value, list):
            for item in value:
                check(item, field)
        elif isinstance(value, str):
            if field not in {"rubric_id", "harness", "trigger", "status", "group_key",
                              "proposal_id", "adoption_id", "usage_without_evidence"}:
                raise AutError("improve: report-card JSON contains a non-count value")
        elif value is not None and type(value) not in (int, bool):
            raise AutError("improve: report-card JSON contains a non-count value")

    check(public)
    return json.dumps(public, sort_keys=True)


def _sections(card: dict[str, Any]) -> list[tuple[str, list[str], list[list[Any]]]]:
    private = card["private"]
    result = [("Overview", ["Item", "Value"], [
        ["Current window", private["window"]], ["Previous window", private["previous_window"]],
        ["Last cycle", card["cycle"]], ["Store records", card["records"]],
        ["Judges qualified", f"{sum(r['qualified'] for r in card['qualifications'])} of "
         f"{len(card['qualifications'])}"]])]
    result.append(("Rubrics", ["Rubric/version", "Harness", "Trigger", "Current pass/fail/unknown",
        "Previous pass/fail/unknown", "Current failure rate", "Previous failure rate", "Change"],
        [[f"{r['rubric_id']} v{r['rubric_version']}", r["harness"], r["trigger"],
          list(r["current"].values()), list(r["previous"].values()), _rate(r["current"]),
          _rate(r["previous"]), _change(r["current"], r["previous"])] for r in card["rubrics"]]))
    result.append(("Qualification", ["Rubric/version", "Qualified", "Fail labels", "Recall", "False alarm"],
        [[f"{r['rubric_id']} v{r['rubric_version']}", r["qualified"],
          f"{r['fail_labels']}/{r['minimum']}",
          _fraction(r["counts"].get("detected_failures", 0), r["counts"].get("reference_fail", 0))
          if not r["counts"].get("missing") else "unavailable",
          _fraction(r["counts"].get("false_alarms", 0), r["counts"].get("reference_pass", 0))
          if not r["counts"].get("missing") else "unavailable"] for r in card["qualifications"]]))
    result.append(("Top recurring groups (" + private["group_window"] + ")",
        ["Group", "Tasks", "Projects", "Last audit incidents", "Status"],
        [[private["groups"].get(r["group_key"], "Unknown group"), r["distinct_tasks"],
          r["projects"], r["audit_incidents"], r["status"]] for r in card["groups"]]))
    result.append(("Open proposals", ["Title", "Owner", "Age days", "Status", "Staged", "Accept", "Reject"],
        [[private["proposals"][r["proposal_id"]]["title"],
          private["proposals"][r["proposal_id"]]["owner"], r["age_days"], r["status"], r["staged"],
          private["proposals"][r["proposal_id"]]["accept"],
          private["proposals"][r["proposal_id"]]["reject"]] for r in card["proposals"]]))
    result.append(("Adopted fixes, 14 ledger dates per side (adoption day excluded)",
        ["Title", "Rubric/version", "Before controls", "After controls", "Tasks before/after", "Meaning"],
        [[private["adoptions"][r["adoption_id"]], f"{r['rubric_id']} v{r['rubric_version']}",
          _rate(r["before"], 10), _rate(r["after"], 10),
          f"{r['tasks_before']} / {r['tasks_after']}", "before/after counts, not a causal estimate"]
         for r in card["adoptions"]]))
    gaps = card["coverage"]
    result.append(("Coverage gaps", ["Item", "Value"], [[k, v] for k, v in gaps.items()] + [
        ["Incomplete evidence share (current-version judgments)",
         _fraction(gaps["incomplete_evidence"], gaps["judgments"])],
        ["Scope", "Newest audit and cached coverage; missing coverage is unknown, not zero."]]))
    result.append(("Loop cost", ["Item", "Value"], [[k, v] for k, v in card["cost"].items()] + [
        ["Recorded load and inference seconds", private["model_seconds"]],
        ["Drafter USD", private["drafter_cost_usd"]],
        ["Drafter records without known cost", private["drafter_unknown_cost"]],
        ["Counting", "Recorded calls and diagnosis-reported calls overlap; do not add them. "
         "Missing timing is unknown. Labels include undo events. Tasks mean distinct sessions."]]))
    return result


def _fraction(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.1%} ({numerator}/{denominator})" if denominator else "unavailable (0/0)"


def _change(current: dict[str, int], previous: dict[str, int]) -> str:
    a, b = current["passed"] + current["failed"], previous["passed"] + previous["failed"]
    return f"{100 * (current['failed'] / a - previous['failed'] / b):+.1f} percentage points" \
        if a and b else "unavailable"


def _md(value: Any) -> str:
    text = html.escape(str(value), quote=True)
    text = re.sub(r"([\\`*_{}\[\]()#!|~])", r"\\\1", text)
    return text.replace("\r", " ").replace("\n", "<br>")


def to_markdown(card: dict[str, Any]) -> str:
    parts = ["# Private improvement report card", NOTICE]
    for title, headers, rows in _sections(card):
        table = ["| " + " | ".join(headers) + " |",
                 "| " + " | ".join("---" for _ in headers) + " |"]
        table += ["| " + " | ".join(_md(v) for v in row) + " |" for row in rows]
        parts += ["## " + _md(title), "\n".join(table)]
        if not rows:
            parts.append("No records. Adoption rates need 10 known cases per side; too few cases.")
    return "\n\n".join(parts) + "\n"


def to_html(card: dict[str, Any]) -> str:
    parts = ["<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Private improvement report card</title><style>"
        "body{font:16px system-ui;margin:2rem;max-width:75rem;line-height:1.5}"
        "td,th{text-align:left;padding:.7rem;border-bottom:1px solid #ccc}"
        "table{border-collapse:collapse;width:100%}.scroll{overflow:auto}"
        "</style><h1>Private improvement report card</h1><p>" + NOTICE + "</p>"]
    for title, headers, rows in _sections(card):
        parts.append("<h2>" + html.escape(title) + "</h2><div class=scroll role=region tabindex=0 "
            "aria-label='" + html.escape(title, quote=True) + "'><table><tr>" +
            "".join("<th>" + html.escape(h) + "</th>" for h in headers) + "</tr>" +
            "".join("<tr>" + "".join("<td>" + html.escape(str(v)) + "</td>" for v in row) +
                    "</tr>" for row in rows) + "</table></div>")
        if not rows:
            parts.append("<p>No records. Adoption rates need 10 known cases per side; too few cases.</p>")
    return "".join(parts) + "</html>\n"


def write(store: Store, *, days: int = 28, format: str = "md") -> str:
    if format not in {"md", "html", "json"}:
        raise AutError("improve: report-card format must be md, html or json")
    if type(days) is not int or not 1 <= days <= 366:
        raise AutError("improve: report-card days must be 1..366")
    zone = configured_zone()
    for rubric in judge._rubrics():
        qualify.run(store, rubric.id)
    card = build(store, days=days, zone=zone)
    markdown, page = to_markdown(card), to_html(card)
    atomic_write_text(store.root / "report-card.md", markdown)
    atomic_write_text(store.root / "report-card.html", page)
    return {"md": markdown, "html": page, "json": to_json(card)}[format]
