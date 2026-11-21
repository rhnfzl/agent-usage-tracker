"""Open date and grouping controls on the private local usage page."""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs

from . import local_page, overview
from .config import AutError, Config, ledger_zone
from .coverage import build_report
from .improve import report_card
from .improve.store import Store
from .label.core import data_dir
from .summary import GROUPS, Group, build_summary


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("overview", help="open a private local usage overview")
    p.add_argument("--period", choices=overview.PERIODS, help="ledger period to date (default today)")
    p.add_argument("--since", type=date.fromisoformat, help="first ledger date, inclusive")
    p.add_argument("--until", type=date.fromisoformat, help="last ledger date, exclusive")
    p.add_argument("--group-by", choices=GROUPS, default="agent")
    p.add_argument("--no-browser", action="store_true", help="print the private URL for manual open")


def run(cfg: Config, args: argparse.Namespace) -> None:
    initial = build_summary(cfg, group_by=args.group_by, period=args.period,
                            since=args.since, until=args.until)
    initial_period = args.period or ("custom" if args.since else "today")
    health = build_report(cfg)
    improvement_root = Path(os.environ.get("AUT_IMPROVE_ROOT", str(data_dir() / "improve")))
    has_evidence = (improvement_root.expanduser() / ".aut-improve").is_file()
    token = local_page.new_token()
    root = f"/{token}"

    def on_get(path: str) -> bytes | None:
        if not path:
            return overview.render(initial, health, root, initial_period, has_evidence)
        if path == "/improvement" and has_evidence:
            try:
                evidence = Store(improvement_root, read_only=True)
                return report_card.to_html(report_card.build(evidence, zone=ledger_zone(cfg))).encode()
            except (AutError, OSError, ValueError):
                return b"<p>Private improvement evidence is unreadable.</p>"
        if not path.startswith("?") or len(path) > 1024:
            return None
        try:
            query = parse_qs(path[1:], keep_blank_values=True, max_num_fields=4)
            period, = query.get("period", ["today"])
            grouping, = query.get("group", [initial["group_by"]])
            if grouping not in GROUPS:
                raise ValueError
            group = cast(Group, grouping)
            if period == "custom":
                since, = query.get("since", [""])
                until, = query.get("until", [""])
                report = build_summary(cfg, group_by=group, since=date.fromisoformat(since),
                                       until=date.fromisoformat(until))
            elif period in overview.PERIODS:
                report = build_summary(cfg, group_by=group, period=period)
            else:
                raise ValueError
        except (AutError, ValueError):
            return overview.render(initial, health, root, initial_period, has_evidence,
                                   error="Choose two dates in order for Custom, or select a period.")
        return overview.render(report, health, root, period, has_evidence)

    def ready(url: str) -> None:
        if args.no_browser:
            print(url, flush=True)

    local_page.serve(token, on_get, lambda _: None,
                     "private usage overview at {url}. Press Ctrl-C to close.",
                     open_browser=not args.no_browser, on_ready=ready)
