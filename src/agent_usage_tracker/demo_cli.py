"""Run the real offline report against synthetic native agent logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from . import demo, summary


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("demo", help="try the usage report with synthetic agent logs")
    p.add_argument("--group-by", choices=summary.GROUPS, default="agent")
    p.add_argument("--json", action="store_true", help="print the synthetic report as JSON")


def run(args: argparse.Namespace) -> None:
    with TemporaryDirectory(prefix="aut-demo-") as directory:
        cfg = demo.fixture(Path(directory))
        report = summary.build_summary(cfg, since=demo.SINCE, until=demo.UNTIL,
                                       group_by=args.group_by, now=demo.NOW,
                                       prices_path=demo.sample_prices(cfg.home))
    if args.json:
        print(json.dumps({"synthetic": True, **report}, sort_keys=True, allow_nan=False))
    else:
        print("Synthetic demo with sample rates\n" + summary.render(report))
