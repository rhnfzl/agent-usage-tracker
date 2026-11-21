"""The private static inbox page: open proposals, recurring failure groups, the last cycle's
judgments and adopted fixes. Decisions are made with `aut improve inbox accept|reject`."""

from __future__ import annotations

import html
import json
import re
from typing import Any

from ..config import AutError
from ..state import _locked, atomic_write_text
from . import decide, groups
from .case_view import write_case_view
from .store import Store

TOP_GROUPS = 10
TOP_PROPOSALS = 20
TOP_ADOPTIONS = 5


def _case_link(store: Store, case_id: str) -> str:
    try:
        write_case_view(store, case_id)     # written first, so the link works
    except Exception:   # an old or broken case keeps its id, without a link
        return f"<code>{html.escape(case_id[:12])}</code>"
    return (f"<a href='case_views/{html.escape(case_id)}.html'>"
            f"<code>{html.escape(case_id[:12])}</code></a>")


def _proposal(store: Store, key: str, report: dict[str, Any]) -> str:
    value = store.get("proposals", key)
    short = decide.short_id(store, key)
    if not re.fullmatch(r"[0-9a-f]{8,64}", short):     # only hex reaches a copied command
        raise AutError("improve: invalid proposal id")
    group = report.get(value.get("group_key") or "")
    facts = [("Target", f"{value['scope']} : {value['target']}"),
             ("Owner component", value.get("owner_component")), ("Cause", value.get("cause")),
             ("Expected effect", value.get("expected_effect")), ("Rollback", value.get("rollback"))]
    if group:
        facts += [("Group", " · ".join(v for v in group["signals"].values() if v)),
                  ("Tasks", str(group["distinct_tasks"])),
                  ("Projects", ", ".join(f"{n} ({c})" for n, c in group["projects"].items()))]
    drafter = value.get("drafter")
    if isinstance(drafter, dict):
        facts.append(("Drafter", f"{drafter.get('cli')} {drafter.get('model')}, "
                                 f"cost {drafter.get('cost_usd')} USD"))
    staged = decide.staged_at(store, key)
    facts.append(("Staged", str(staged) if staged else "not staged"))
    evidence = value.get("evidence")
    cases = (evidence.get("case_ids") if isinstance(evidence, dict) else None) or [value["case_id"]]
    command = decide.command(store)
    return (f"<article><h3><code>{short}</code> "
            f"{html.escape(str(value.get('title') or 'Operator proposal'))}</h3><dl>"
            + "".join(f"<dt>{name}</dt><dd>{html.escape(str(text))}</dd>"
                      for name, text in facts if text)
            + "<dt>Cases</dt><dd>" + ", ".join(_case_link(store, c) for c in cases[:5])
            + "</dd></dl><pre>" + html.escape(value["diff"]) + "</pre>"
            f"<pre><code>{html.escape(command)} inbox accept {short}\n"
            f"{html.escape(command)} inbox reject {short} --reason \"…\"</code></pre></article>")


def _safe_proposal(store: Store, key: str, report: dict[str, Any]) -> str:
    try:    # one bad record must not hide the other proposals
        return _proposal(store, key, report)
    except Exception:
        return f"<p>Proposal <code>{html.escape(key[:8])}</code> could not be read.</p>"


def _proposals(store: Store) -> str:
    head = ("<h2>Proposals to review</h2><p>Copy a command into a terminal. Accept commits only "
            "inside the candidate; you apply, review and merge the change yourself.</p>")
    try:
        keys = decide.open_proposals(store)
        if not keys:
            return head + "<p>No open proposals.</p>"
        try:
            report = {g["group_key"]: g for g in (groups.latest(store) or {}).get("groups", [])}
        except Exception:
            report = {}
        more = (f"<p>{len(keys) - TOP_PROPOSALS} more: <code>aut improve inbox list</code></p>"
                if len(keys) > TOP_PROPOSALS else "")
        return head + "".join(_safe_proposal(store, key, report)
                              for key in keys[:TOP_PROPOSALS]) + more
    except Exception:
        return head + "<p>The proposals could not be read.</p>"


def _adoptions(store: Store) -> str:
    head = "<h2>Adopted fixes</h2><p>Adopted, benefit unproven.</p>"
    try:
        rows = sorted((store.get("adoptions", key) for key in store.keys("adoptions")),
                      key=lambda row: row["adopted_at"], reverse=True)[:TOP_ADOPTIONS]
        if not rows:
            return head + "<p>No adoptions recorded.</p>"
        return head + "<ul>" + "".join(
            f"<li>{html.escape(row['adopted_at'])}: {html.escape(row['title'])}</li>"
            for row in rows) + "</ul>"
    except Exception:
        return head + "<p>The adoptions could not be read.</p>"


def summary(store: Store) -> dict[str, Any]:
    """Ids and counts only, no text: open proposals and the newest failure groups."""
    status = decide.group_status(store)
    proposals = [{"id": key, "short_id": decide.short_id(store, key),
                  "case_id": store.get("proposals", key)["case_id"],
                  "group_key": store.get("proposals", key).get("group_key"),
                  "staged": decide.staged_at(store, key) is not None}
                 for key in decide.open_proposals(store)]
    report = groups.latest(store) or {"groups": []}
    return {"proposals": proposals, "groups": [
        {"group_key": g["group_key"], "status": status.get(g["group_key"], "open"),
         "distinct_tasks": g["distinct_tasks"], "judgments": len(g["judgment_ids"]),
         "recurring": g["recurring"]} for g in report["groups"]]}


def rewrite(store: Store) -> None:
    """Rebuild the page after a decision, with the last cycle's judgments."""
    try:
        report = json.loads((store.root / "last_cycle.json").read_text(encoding="utf-8"))
        diagnosis = report.get("diagnosis", report.get("progress", {}).get("diagnosis", {}))
        ids = list(diagnosis.get("judgment_ids", []))
    except (OSError, ValueError, AttributeError, TypeError):
        ids = []
    write(store, ids)


def _groups(store: Store) -> str:
    head = ("<h2>Recurring failure groups</h2>"
            "<p>Fail and split-vote judgments grouped by rubric, cause, tool, command and error "
            "class. Tasks counts distinct sessions, so one long session is one task. "
            "Last audit counts unsampled error results with the same tool, command and error.</p>")
    try:    # the page must render even when the group report is missing, old or malformed
        return head + _group_table(groups.latest(store), decide.group_status(store))
    except Exception:
        return head + "<p>The group report could not be read.</p>"


def _group_table(report: dict[str, Any] | None, status: dict[str, str]) -> str:
    if not report or not report["groups"]:
        return "<p>No failure groups yet.</p>"
    rows = []
    for group in report["groups"][:TOP_GROUPS]:
        values = group["signals"]
        title = " · ".join(values[name] for name in ("rubric", "cause", "tool",
                           "command_family", "error_class") if values[name])
        projects = ", ".join(f"{name} ({count})" for name, count in group["projects"].items())
        cells = [title, status.get(group["group_key"], "open"), str(group["distinct_tasks"]), str(len(group["judgment_ids"])), projects,
                 f"{group['first_work_day']} to {group['last_work_day']}",
                 "yes" if group["recurring"] else "no", str(group["audit_incidents"])]
        rows.append("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in cells) + "</tr>")
    return (f"<p>Last {int(report['window_days'])} days up to {html.escape(report['day'])}; "
            f"recurring at {int(report['threshold'])} or more tasks.</p>"
            "<div class=scroll role=region aria-label='Failure groups' tabindex=0><table>"
            "<tr><th>Group</th><th>Status</th><th>Tasks</th><th>Judgments</th><th>Projects</th>"
            "<th>Work days</th><th>Recurring</th><th>Last audit</th></tr>"
            + "".join(rows) + "</table></div>")


def _trigger(store: Store, case_id: str) -> str:
    try:
        return str(store.get("cases", case_id).get("trigger") or "")
    except AutError:
        return ""


def write(store: Store, judgment_ids: list[str]) -> None:
    # One render-and-write at a time, so a decision's rewrite and the nightly cycle never
    # interleave an older view over a newer one.
    with _locked(store.root / "inbox"):
        rows: list[dict[str, Any]] = [store.get("judgments", key) for key in judgment_ids]
        entries = []
        for row in rows:
            review = row.get("review") or {}
            cells = [row["rubric"], str(row.get("rubric_version") or ""),
                     _trigger(store, row["case_id"]), row["verdict"],
                     review.get("rationale", "Local decision-model estimate")]
            entries.append("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in cells)
                           + "<td><code>" + html.escape(row["case_id"][:12]) + "</code></td></tr>")
        page = ("<!doctype html><html lang=en><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width, initial-scale=1'>"
                "<title>Private improvement inbox</title><style>"
                "body{font:16px system-ui;margin:2rem;max-width:75rem;line-height:1.5}"
                "td,th{text-align:left;padding:.7rem;border-bottom:1px solid #ccc}"
                "table{border-collapse:collapse;width:100%} .scroll,pre{overflow:auto}"
                "pre{background:#f4f4f4;padding:.7rem} dt{font-weight:600}"
                "article{border-top:1px solid #ccc;margin-top:1rem}"
                "</style><h1>Private improvement inbox</h1>"
                "<p>Local model estimates. Unknowns remain unresolved. These are not human labels "
                "or proof of task quality. Routine review does not require a human.</p>"
                + _proposals(store) + _groups(store)
                + "<h2>Judgments from the last cycle</h2>"
                "<div class=scroll role=region aria-label='Model judgments' tabindex=0><table>"
                "<tr><th>Criterion</th><th>Version</th><th>Trigger</th><th>Estimate</th>"
                "<th>Evidence review</th><th>Case</th></tr>"
                + "".join(entries) + "</table></div>"
                + _adoptions(store) + "</html>")
        atomic_write_text(store.root / "inbox.html", page)
