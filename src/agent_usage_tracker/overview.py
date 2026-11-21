"""Render the private usage summary and local source health."""

from __future__ import annotations

from html import escape
from typing import Any, Iterable

from .local_page import head
from .summary import GROUPS, Summary, Totals

PERIODS = ("today", "week", "month")
_CSS = """
h1 { margin: 0 0 .25rem; font-size: clamp(1.95rem, 5vw, 3rem); letter-spacing: -.04em; }
h2 { margin-top: 2rem; margin-bottom: .35rem; }
main { padding: 1rem 0 2rem; }
form, dl { display: flex; gap: 1rem; flex-wrap: wrap; align-items: end; }
form, dl, .panel { padding: .9rem 1rem; border: 1px solid var(--line); border-radius: .7rem; }
label { display: grid; gap: .2rem; }
select, input, button { font: inherit; padding: .4rem .55rem; }
button { cursor: pointer; }
dt, .muted { color: var(--muted); }
dd { margin-left: 0; font-size: 1.2rem; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; vertical-align: top; padding: .65rem .55rem; border-bottom: 1px solid var(--line); }
th { white-space: nowrap; }
a { color: var(--acc); }
"""


def _cell(value: Any) -> str:
    return escape(f"{value:,}" if type(value) is int else "unknown" if value is None else str(value))


def _table(title: str, columns: tuple[str, ...], rows: Iterable[Iterable[Any]]) -> str:
    headings = "".join(f'<th scope="col">{escape(label)}</th>' for label in columns)
    body = "".join("<tr>" + "".join(f"<td>{_cell(value)}</td>" for value in row) + "</tr>"
                   for row in rows)
    if not body:
        return "<p>No records in this view.</p>"
    return (f'<div class="scroll" role="region" tabindex="0" aria-label="{escape(title)}">'
            f'<table><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div>')


def _options(values: tuple[str, ...], selected: str) -> str:
    return "".join(f'<option value="{name}"{" selected" if name == selected else ""}>'
                   f'{name.capitalize()}</option>' for name in values)


def _tokens(totals: Totals, key: str) -> int | str | None:
    value, missing = totals["tokens"][key], totals["unknown_events"][key]
    if value is not None and missing:
        return f"{value:,} known ({missing:,} unknown)"
    return value


def _pricing(report: Summary) -> str:
    logged = report["totals"]["logged_cost_usd"]
    display = f"${logged:.6g}" if logged is not None else "unknown"
    rates = _cell(report["rate_status"])
    synced = _cell(report["rate_synced_at"])
    seconds = report["rate_age_seconds"]
    age = "unknown" if seconds is None else f"{seconds:,} s"
    models = (
        ("All models", report["totals"]["pricing"]),
        *((model["model"], model) for model in report["prices_by_model"]),
    )
    costs = _table(
        "API-equivalent cost by model",
        ("Model", "Priced tokens", "Unpriced tokens", "Coverage basis",
         "Cost for priced tokens (USD)", "Estimated total (USD)"),
        ((label, values["priced_tokens"], values["unpriced_tokens"], values["coverage_tokens"],
          values["known_api_cost_usd"], values["estimated_api_cost_usd"])
         for label, values in models),
    )
    return (
        '<h2>Pricing</h2><div class="panel">'
        f'<p>Rate file {rates} · last updated {synced} · age {age}</p>{costs}'
        f'<p>Logged cost (USD) <strong>{display}</strong></p>'
        '<p class="muted">Estimates use local API rates. Provider billing is separate.</p></div>'
    )


def render(report: Summary, health: dict[str, Any], root: str, period: str,
           private_evidence: bool, *, error: str | None = None) -> bytes:
    window, group_by, totals = report["window"], report["group_by"], report["totals"]
    since = window["since"] if period == "custom" else ""
    until = window["until"] if period == "custom" else ""
    filters = (
        f'<form method="get" action="{escape(root)}">'
        f'<label>Period <select name="period">{_options((*PERIODS, "custom"), period)}'
        '</select></label>'
        f'<label>First date <input type="date" name="since" value="{escape(since)}"></label>'
        f'<label>End date (exclusive) <input type="date" name="until" value="{escape(until)}"></label>'
        f'<label>Group by <select name="group">{_options(GROUPS, group_by)}</select></label>'
        '<button type="submit">Update view</button></form>'
    )
    usage = _table(
        "Usage by group",
        (group_by.capitalize(), "Events", "Total tokens", "Fresh input", "Output",
         "Cache read", "Cache write", "Reasoning"),
        ((group["key"], group["events"], *(_tokens(group, key) for key in (
            "total_tokens", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "reasoning_tokens"))) for group in report["groups"]),
    )
    receipts = {item["collector"]: item["observation"] for item in health["collectors"]}
    source_health = _table(
        "Source health",
        ("Source", "Usage coverage", "Events in range", "Latest usage (UTC)",
         "Scan receipt", "Last scan"),
        ((source["source"], source["status"].replace("_", " "), source["events"],
          source["latest_event_at"],
          receipts.get(source["source"], {}).get("receipt_status", "missing"),
          receipts.get(source["source"], {}).get("finished_at"))
         for source in report["sources"]),
    )
    issues = health.get("issues", [])
    issue_list = (
        f'<details><summary>{len(issues):,} local coverage issues</summary>'
        + _table("Local coverage issues", ("Source", "Observation"),
                 ((issue["source_id"], issue["code"].replace("_", " ")) for issue in issues))
        + "</details>"
        if issues else ""
    )
    link = (f'<a href="{escape(root)}/improvement">Private improvement report card</a>'
            if private_evidence else 'Run <code>aut improve report-card</code> for improvement evidence.')
    return (
        head("Agent usage overview", "74rem", _CSS) + "<main><h1>Agent usage overview</h1>"
        f'<p class="muted">Private local report · {escape(window["since"])} to '
        f'{escape(window["until"])} (exclusive) · {escape(window["timezone"])}</p>'
        + filters
        + (f'<p role="alert">{escape(error)}</p>' if error else "")
        + f'<dl><div><dt>Tokens</dt><dd>{_cell(_tokens(totals, "total_tokens"))}</dd></div>'
        f'<div><dt>Usage events</dt><dd>{_cell(totals["events"])}</dd></div></dl>'
        f'<h2>Usage by {escape(group_by)}</h2>{usage}'
        '<p class="muted">Reasoning is part of output. Counts reflect observed records.</p>'
        + _pricing(report)
        + f'<h2>Source health</h2>{source_health}'
        + issue_list
        + '<p class="muted">Run <code>aut doctor --json</code> for full coverage evidence.</p>'
        f'<h2>Private evidence</h2><p>{link}</p></main></body></html>'
    ).encode("utf-8")
