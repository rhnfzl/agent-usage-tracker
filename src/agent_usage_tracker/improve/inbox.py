"""The private static inbox page: the last cycle's judgments and the recurring failure groups."""

from __future__ import annotations

import html
from typing import Any

from ..state import atomic_write_text
from . import groups
from .store import Store

TOP_GROUPS = 10


def _groups(store: Store) -> str:
    head = ("<h2>Recurring failure groups</h2>"
            "<p>Fail and split-vote judgments grouped by rubric, cause, tool, command and error "
            "class. Tasks counts distinct sessions, so one long session is one task. "
            "Last audit counts unsampled error results with the same tool, command and error.</p>")
    try:    # the page must render even when the group report is missing, old or malformed
        return head + _group_table(groups.latest(store))
    except Exception:
        return head + "<p>The group report could not be read.</p>"


def _group_table(report: dict[str, Any] | None) -> str:
    if not report or not report["groups"]:
        return "<p>No failure groups yet.</p>"
    rows = []
    for group in report["groups"][:TOP_GROUPS]:
        values = group["signals"]
        title = " · ".join(values[name] for name in ("rubric", "cause", "tool",
                           "command_family", "error_class") if values[name])
        projects = ", ".join(f"{name} ({count})" for name, count in group["projects"].items())
        cells = [title, str(group["distinct_tasks"]), str(len(group["judgment_ids"])), projects,
                 f"{group['first_work_day']} to {group['last_work_day']}",
                 "yes" if group["recurring"] else "no", str(group["audit_incidents"])]
        rows.append("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in cells) + "</tr>")
    return (f"<p>Last {int(report['window_days'])} days up to {html.escape(report['day'])}; "
            f"recurring at {int(report['threshold'])} or more tasks.</p>"
            "<div class=scroll role=region aria-label='Failure groups' tabindex=0><table>"
            "<tr><th>Group</th><th>Tasks</th><th>Judgments</th><th>Projects</th>"
            "<th>Work days</th><th>Recurring</th><th>Last audit</th></tr>"
            + "".join(rows) + "</table></div>")


def write(store: Store, judgment_ids: list[str]) -> None:
    rows: list[dict[str, Any]] = [store.get("judgments", key) for key in judgment_ids]
    entries = []
    for row in rows:
        review = row.get("review") or {}
        entries.append("<tr><td>" + html.escape(row["rubric"]) + "</td><td>"
                       + html.escape(row["verdict"]) + "</td><td>"
                       + html.escape(review.get("rationale", "Local decision-model estimate"))
                       + "</td><td><code>" + html.escape(row["case_id"][:12]) + "</code></td></tr>")
    page = ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            "<title>Private improvement inbox</title><style>"
            "body{font:16px system-ui;margin:2rem;max-width:75rem;line-height:1.5}"
            "td,th{text-align:left;padding:.7rem;border-bottom:1px solid #ccc}"
            "table{border-collapse:collapse;width:100%} .scroll{overflow:auto}"
            "</style><h1>Private improvement inbox</h1>"
            "<p>Local model estimates. Unknowns remain unresolved. These are not human labels "
            "or proof of task quality. Routine review does not require a human.</p>"
            "<h2>Judgments from the last cycle</h2>"
            "<div class=scroll role=region aria-label='Model judgments' tabindex=0><table>"
            "<tr><th>Criterion</th><th>Estimate</th><th>Evidence review</th><th>Case</th></tr>"
            + "".join(entries) + "</table></div>"
            + _groups(store) + "</html>")
    atomic_write_text(store.root / "inbox.html", page)
