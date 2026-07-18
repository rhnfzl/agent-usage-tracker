"""aut CLI: scan | backfill | status.

  aut scan              incremental: read new usage since the watermark, push, advance
  aut scan --dry-run    parse + normalize + AGGREGATE, print a report, send/commit nothing
  aut backfill          reset the watermark for the selected agents, then scan everything
  aut status            show config + watermark state

The dry-run report doubles as the accuracy check: it prints per-agent, per-model token
sums straight from your real logs.
"""

from __future__ import annotations

import argparse
import sys

from .collectors import REGISTRY
from .config import Config, load_config
from .event import UsageEvent
from .push import make_sender
from .state import collector_wm, load_state, save_state

BATCH = 500


def _blank_agg() -> dict:
    return {"events": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "reasoning": 0, "total": 0, "skipped_no_ts": 0,
            "by_model": {}}


def _add(agg: dict, e: UsageEvent) -> None:
    agg["events"] += 1
    agg["input"] += e.input_tokens
    agg["output"] += e.output_tokens
    agg["cache_read"] += e.cache_read_tokens
    agg["cache_write"] += e.cache_write_tokens
    agg["reasoning"] += e.reasoning_tokens or 0
    agg["total"] += e.total_tokens
    m = agg["by_model"].setdefault(e.model or "unknown", {"events": 0, "total": 0})
    m["events"] += 1
    m["total"] += e.total_tokens


def run_scan(cfg: Config, agents: list[str], dry_run: bool) -> dict:
    state = load_state()
    sender = None if dry_run else make_sender(cfg)
    report: dict = {}

    for name in agents:
        collect = REGISTRY[name]
        wm = collector_wm(state, name)
        agg = report.setdefault(name, _blank_agg())
        buf_events: list[UsageEvent] = []
        buf_commits: list = []

        def flush() -> None:
            if not dry_run and buf_events and sender is not None:
                sender(buf_events)          # raises on delivery failure -> commits skipped
            if not dry_run:
                for commit in buf_commits:
                    commit()
            buf_events.clear()
            buf_commits.clear()

        try:
            for events, commit in collect(cfg, wm):
                for e in events:
                    e.machine = cfg.machine
                    if e.ts is None:
                        agg["skipped_no_ts"] += 1
                        continue
                    _add(agg, e)
                    if not dry_run:
                        buf_events.append(e)
                if not dry_run:
                    buf_commits.append(commit)
                    if len(buf_events) >= BATCH:
                        flush()
            if not dry_run:
                flush()
        finally:
            if not dry_run:
                save_state(state)

    return report


def _print_report(cfg: Config, report: dict, dry_run: bool) -> None:
    mode = "DRY-RUN (nothing sent)" if dry_run else "scan"
    print(f"\naut {mode}  machine={cfg.machine}\n" + "=" * 60)
    grand = 0
    for name, agg in report.items():
        if agg["events"] == 0 and agg["skipped_no_ts"] == 0:
            print(f"{name:<12} -  no new usage")
            continue
        grand += agg["total"]
        print(f"{name:<12} {agg['events']:>6} turns  {agg['total']:>12,} tokens "
              f"(in {agg['input']:,} / out {agg['output']:,} / "
              f"cacheR {agg['cache_read']:,} / cacheW {agg['cache_write']:,}"
              + (f" / reason {agg['reasoning']:,}" if agg['reasoning'] else "") + ")")
        for model, mv in sorted(agg["by_model"].items(), key=lambda kv: -kv[1]["total"]):
            print(f"    {model:<28} {mv['events']:>5} turns  {mv['total']:>12,} tokens")
        if agg["skipped_no_ts"]:
            print(f"    (skipped {agg['skipped_no_ts']} records with no timestamp)")
    print("-" * 60)
    print(f"{'TOTAL':<12} {grand:>32,} tokens")


def run_export(cfg: Config, agents: list[str], out) -> int:
    """Write all normalized events (from scratch, ignoring the watermark) as CSV to `out`.
    Column order matches push.COLUMNS / the sink table, so it loads via COPY. Useful for
    an offline bulk backfill or a bridged load where the live transport is not wired yet."""
    import csv

    from .push import COLUMNS

    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    n = 0
    for name in agents:
        for events, _commit in REGISTRY[name](cfg, {}):
            for e in events:
                e.machine = cfg.machine
                if e.ts is None:
                    continue
                d = e.to_dict()
                writer.writerow(["" if d[c] is None else d[c] for c in COLUMNS])
                n += 1
    return n


def cmd_status(cfg: Config, agents: tuple[str, ...]) -> None:
    state = load_state()
    print(f"machine       : {cfg.machine}")
    print(f"agents        : {', '.join(agents)}")
    print(f"webhook       : {'configured' if cfg.has_webhook else 'NOT set'}")
    print(f"pg fallback   : {'configured' if cfg.has_pg else 'not set'}")
    print("watermarks    :")
    for name in agents:
        wm = state.get("collectors", {}).get(name, {})
        if name == "cursor":
            detail = "full-scan each run (no watermark)"
        elif "files" in wm:
            detail = f"{len(wm['files'])} files tracked"
        else:
            detail = "never scanned"
        print(f"    {name:<12} {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aut", description="coding-agent token usage tracker")
    parser.add_argument("--agents", help="comma-separated subset override (default: config)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="push new usage since the watermark")
    p_scan.add_argument("--dry-run", action="store_true", help="parse + report, send nothing")
    sub.add_parser("backfill", help="reset watermark for selected agents, then scan all history")
    sub.add_parser("status", help="show config + watermark state")
    p_export = sub.add_parser("export", help="write all usage as CSV (bulk load / offline bridge)")
    p_export.add_argument("--out", help="output CSV file (default: stdout)")
    args = parser.parse_args(argv)

    cfg = load_config()
    agents = cfg.agents
    if args.agents:
        agents = tuple(a.strip() for a in args.agents.split(",") if a.strip())
        bad = [a for a in agents if a not in REGISTRY]
        if bad:
            parser.error(f"unknown agent(s): {bad}; valid = {list(REGISTRY)}")

    if args.cmd == "status":
        cmd_status(cfg, agents)
        return 0

    if args.cmd == "export":
        out = open(args.out, "w", newline="") if args.out else sys.stdout
        try:
            n = run_export(cfg, list(agents), out)
        finally:
            if args.out:
                out.close()
        dest = f" -> {args.out}" if args.out else ""
        print(f"exported {n} events{dest}", file=sys.stderr)
        return 0

    if args.cmd == "backfill":
        state = load_state()
        for name in agents:
            state.get("collectors", {}).pop(name, None)
        save_state(state)
        print(f"watermark reset for: {', '.join(agents)}")

    dry = getattr(args, "dry_run", False)
    report = run_scan(cfg, list(agents), dry)
    _print_report(cfg, report, dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
