"""Register and render the offline usage report."""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Sequence

from . import summary
from .config import Config


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("summary", help="summarize local usage without contacting a sink")
    parser.add_argument("--agents", default=argparse.SUPPRESS, help="comma-separated source override")
    parser.add_argument("--period", choices=("today", "week", "month"),
                        help="ledger period to date (default: today)")
    parser.add_argument("--since", type=date.fromisoformat, help="first ledger date, inclusive")
    parser.add_argument("--until", type=date.fromisoformat, help="last ledger date, exclusive")
    parser.add_argument("--group-by", choices=summary.GROUPS, default="agent")
    parser.add_argument("--json", action="store_true", help="print the private summary as JSON")


def run(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    report = summary.build_summary(cfg, agents, group_by=args.group_by, period=args.period,
                                   since=args.since, until=args.until)
    print(json.dumps(report, sort_keys=True, allow_nan=False) if args.json else summary.render(report))
