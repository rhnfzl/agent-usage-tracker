"""aut CLI.

  aut scan              incremental: read new rows since the watermark, push, advance
  aut scan --dry-run    parse + normalize + AGGREGATE, print a report, send/commit nothing
  aut backfill          reset the watermark for the selected agents, then scan everything
  aut backfill --sessions-only [--dry-run]
                        re-read all history and send ONLY session rows; watermark untouched
  aut backfill --activity-only [--dry-run]
                        the same for activity rows (steers, commits, turns, ...); never usage
  aut status            show config + watermark state
  aut export [--out F]  write all usage as CSV (bulk load, offline bridge); never the watermark
  aut sync-prices [--out F]
                        refresh the API-equivalent model price map
  aut report            retired: warns and writes nothing
  aut time add --start <local> --end <local> | --minutes N [--at <local end>] [--not-relevant]
                        record a manual active-time interval (needs transport.pg_dsn)
  aut outcomes [--fetch] [--dry-run]
                        score every commit row against the local clones -> agents.commit_outcome
  aut outcome <shipped|ruled-out|open> [--session ID]
                        the manual outcome of this project's newest interactive session
  aut episodes review [--month YYYY-MM]
                        hand-check a month's episodes on a local page -> agents.episode_correction
  aut inspect --episode ID | --day YYYY-MM-DD | --label UNIT_ID
                        why an episode, a day's episodes or a label exist, on a local read-only page
  aut label run [--task T] [--since YYYY-MM-DD] [--dry-run]
                        a local model labels new units (optional extra aut[label]) -> label rows
  aut label backfill --task T --days N
                        the same over the last N days; the nightly watermark is not moved back
  aut label teach --task T --n N [--budget-usd X]
                        teacher (silver) labels for a stratified sample -> eval/<task>.silver.jsonl
  aut label review [--task T]
                        label gold answers on a local page -> eval/<task>.jsonl (never text)
  aut label eval --task T | choose --task T | train-encoder --task T
                        bake-off on gold -> eval record; the winner -> label.json; the encoder

The dry-run report doubles as the accuracy check: it prints per-agent, per-model token
sums straight from your real logs.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from . import pricing, scan
from .collectors import REGISTRY
from .config import ALL_AGENTS, AutError, Config, load_config, config_path
from .event import TimeInterval, make_event_id
from .label.names import DAYS, DECISION_TASKS, TASK_NAMES
from .push import insert_pg
from .state import load_state


def cmd_scan(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    report = scan.run_scan(cfg, agents, dry_run=args.dry_run)
    scan.print_scan(cfg, report, args.dry_run)


def cmd_backfill(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    if args.sessions_only or args.activity_only:
        counts = scan.run_history_backfill(cfg, agents, dry_run=args.dry_run,
                                           sessions=args.sessions_only, activity=args.activity_only)
        scan.print_history_backfill(counts, dry_run=args.dry_run, sessions=args.sessions_only,
                                    activity=args.activity_only)
        return
    scan.reset_watermarks(agents)
    print(f"watermark reset for: {', '.join(agents)}")
    cmd_scan(cfg, agents, args)


def cmd_export(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        written, skipped = scan.run_export(cfg, agents, out)
    finally:
        if args.out:
            out.close()
    scan.print_export(written, skipped, args.out)


def cmd_status(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    state = load_state()
    print(f"machine       : {cfg.machine}")
    print(f"agents        : {', '.join(agents)}")
    if cfg.has_webhook and not cfg.webhook_secret:
        webhook_state = "url set but SECRET MISSING (scan will refuse)"
    else:
        webhook_state = "configured" if cfg.has_webhook else "NOT set"
    print(f"webhook       : {webhook_state}")
    print(f"pg fallback   : {'configured' if cfg.has_pg else 'not set'}")
    print("watermarks    :")
    for name in agents:
        wm = state.get("collectors", {}).get(name, {})
        print(f"    {name:<12} {REGISTRY[name].describe(cfg, wm)}")


_MAX_MANUAL = timedelta(hours=16)


def _local(value: str) -> datetime:
    """A datetime typed by the user; no zone means this machine's local time."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"time add: not a date and time: {value!r} (use YYYY-MM-DD HH:MM)") \
            from None
    return dt if dt.tzinfo else dt.astimezone()


def build_manual_interval(cfg: Config, start: str | None = None, end: str | None = None,
                          minutes: int | None = None, at: str | None = None, *,
                          not_relevant: bool = False, now: datetime | None = None) -> TimeInterval:
    """One manual agents.time_interval row, or SystemExit with the reason it was refused."""
    now = now or datetime.now(timezone.utc)
    if minutes is not None:
        if start or end:
            raise SystemExit("time add: use --start/--end or --minutes, not both")
        end_dt = _local(at) if at else now
        start_dt = end_dt - timedelta(minutes=minutes)
    elif start and end and not at:
        start_dt, end_dt = _local(start), _local(end)
    else:
        raise SystemExit("time add: give --start and --end, or --minutes [--at]")
    if end_dt <= start_dt:
        raise SystemExit("time add: the end must be after the start")
    if end_dt - start_dt > _MAX_MANUAL:
        raise SystemExit("time add: an interval can span at most 16 hours")
    if end_dt > now:                    # hours not yet worked are not active time
        raise SystemExit("time add: the end is in the future")
    s_iso = start_dt.astimezone(timezone.utc).isoformat()
    e_iso = end_dt.astimezone(timezone.utc).isoformat()
    return TimeInterval(event_id=make_event_id("manual", cfg.machine, s_iso, e_iso), ts=s_iso,
                        end_ts=e_iso, source="manual", app=None, title=None, category=None,
                        work_relevant=not not_relevant, attrs={}, machine=cfg.machine)


def cmd_time_add(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    row = build_manual_interval(cfg, args.start, args.end, args.minutes, args.at,
                                not_relevant=args.not_relevant)
    insert_pg(cfg.require_pg("time add"), [row])
    s, e = (datetime.fromisoformat(x).astimezone() for x in (row.ts, row.end_ts))
    mins = (e - s).total_seconds() / 60
    print(f"added manual interval {s:%Y-%m-%d %H:%M} -> {e:%Y-%m-%d %H:%M} ({mins:.0f} min, "
          f"{'relevant' if row.work_relevant else 'not relevant'})")


def cmd_outcomes(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    from . import outcomes
    report = outcomes.run_outcomes(cfg, refresh=args.fetch or cfg.outcome_fetch,
                                   dry_run=args.dry_run)
    outcomes.print_outcomes(report, args.dry_run)


def cmd_outcome(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    from . import outcomes
    dsn = cfg.require_pg("outcome")
    row = outcomes.build_manual_outcome(cfg, args.outcome, os.getcwd(), args.session)
    outcomes.insert_manual_outcome(dsn, row)
    print(f"marked session {row['session_id'][:8]} {row['outcome']}")


def cmd_inspect(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    from . import inspector
    inspector.print_inspect(inspector.run_inspect(cfg, episode=args.episode, day=args.day,
                                                  label=args.label))


def cmd_episodes(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    from . import review
    review.print_review(review.run_episode_review(cfg, args.month))


def _label_run(cfg: Config, args: argparse.Namespace) -> None:
    """run | backfill: label new units, or one task over the last N days."""
    from .label import run as label_run
    from .label.core import local_midnight
    tasks: list[str] | None
    if args.label_cmd == "backfill":
        since = date.today() - timedelta(days=args.days)
        tasks, dry = [args.task], False
    else:
        since, tasks, dry = args.since, [args.task] if args.task else None, args.dry_run
    report = label_run.run_label(cfg, tasks, local_midnight(since) if since else None,
                                 dry_run=dry)
    label_run.print_report(report, dry)
    failed = [name for name, c in report.items() if c["failed"]]
    if failed:
        raise AutError(f"label: could not run {', '.join(failed)} (reasons above)")


def _label_bakeoff(cfg: Config, args: argparse.Namespace) -> None:
    """eval | choose | train-encoder: the bake-off on the gold labels."""
    from .label import bakeoff
    if args.label_cmd == "eval":
        bakeoff.print_eval(args.task, bakeoff.run_eval(cfg, args.task, args.days))
    elif args.label_cmd == "choose":
        bakeoff.print_choose(args.task, bakeoff.choose(cfg, args.task))
    else:
        bakeoff.print_train_encoder(args.task, bakeoff.train_encoder(cfg, args.task, args.days))


def cmd_label(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    if args.label_cmd in ("run", "backfill"):
        _label_run(cfg, args)
    elif args.label_cmd == "teach":
        from .label import teach
        teach.print_teach(args.task, teach.run_teach(cfg, args.task, args.n, args.budget_usd,
                                                     args.days), args.budget_usd)
    elif args.label_cmd == "review":
        from .label import gold
        gold.print_review(gold.run_gold_review(cfg, args.task, args.days))
    else:
        _label_bakeoff(cfg, args)


def cmd_sync_prices(args: argparse.Namespace) -> None:
    out = args.out or str(config_path().parent / "model_prices.json")
    n = pricing.sync_prices(out, litellm_url=args.litellm_url, models_dev_url=args.models_dev_url)
    print(f"synced {n} model prices -> {out}", file=sys.stderr)


def cmd_report(args: argparse.Namespace) -> None:
    # shortcut: kept one release so old crons only warn; delete the subcommand after that
    print("aut report is retired: metrics.json is replaced by the agents database views "
          "(Grafana on the LAN). Nothing was written.", file=sys.stderr)


_NO_CONFIG_COMMANDS = {"sync-prices": cmd_sync_prices, "report": cmd_report}
_COMMANDS = {"scan": cmd_scan, "backfill": cmd_backfill, "export": cmd_export,
             "status": cmd_status, "time": cmd_time_add, "outcomes": cmd_outcomes,
             "outcome": cmd_outcome, "inspect": cmd_inspect, "episodes": cmd_episodes,
             "label": cmd_label}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aut", description="passive coding-agent usage, activity, active-time and outcome tracker")
    parser.add_argument("--agents", help="comma-separated subset override (default: config)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="push new usage since the watermark")
    p_scan.add_argument("--dry-run", action="store_true", help="parse + report, send nothing")
    p_back = sub.add_parser("backfill",
                            help="reset watermark for selected agents, then scan all history")
    p_back.add_argument("--sessions-only", action="store_true",
                        help="send only session rows for all history; watermark untouched")
    p_back.add_argument("--activity-only", action="store_true",
                        help="send only activity rows for all history; watermark untouched")
    p_back.add_argument("--dry-run", action="store_true",
                        help="with --sessions-only/--activity-only: counts only")
    p_back.add_argument("--agents", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub.add_parser("status", help="show config + watermark state")
    p_export = sub.add_parser("export", help="write all usage as CSV (bulk load / offline bridge)")
    p_export.add_argument("--out", help="output CSV file (default: stdout)")
    p_sync = sub.add_parser("sync-prices", help="refresh the API-equivalent model price map")
    p_sync.add_argument("--out", help="output JSON (default: <config-dir>/model_prices.json)")
    p_sync.add_argument("--litellm-url", default=pricing.LITELLM_URL)
    p_sync.add_argument("--models-dev-url", default=pricing.MODELS_DEV_URL)
    p_report = sub.add_parser("report", help="retired: replaced by the database views")
    for flag in ("--out", "--prices", "--idle-gap"):   # still accepted, so old crons only warn
        p_report.add_argument(flag, help=argparse.SUPPRESS)
    p_time = sub.add_parser("time", help="manual active time")
    time_sub = p_time.add_subparsers(dest="time_cmd", required=True)
    p_tadd = time_sub.add_parser("add", help="record one manual interval (local times)")
    p_tadd.add_argument("--start", help="local start, e.g. '2026-11-03 09:00'")
    p_tadd.add_argument("--end", help="local end")
    p_tadd.add_argument("--minutes", type=int, help="length in minutes, ending at --at")
    p_tadd.add_argument("--at", help="local end for --minutes (default: now)")
    p_tadd.add_argument("--not-relevant", action="store_true",
                        help="not work-relevant (default: relevant)")
    p_outs = sub.add_parser("outcomes", help="score commit rows against the local clones")
    p_outs.add_argument("--fetch", action="store_true",
                        help="git fetch the clones and ask gh first (config: outcome_fetch)")
    p_outs.add_argument("--dry-run", action="store_true", help="count states, write nothing, no fetch or gh")
    p_out = sub.add_parser("outcome", help="mark this project's newest session's outcome")
    p_out.add_argument("outcome", choices=["shipped", "ruled-out", "open"])
    p_out.add_argument("--session", help="the session id (default: newest interactive here)")
    p_eps = sub.add_parser("episodes", help="episodes")
    eps_sub = p_eps.add_subparsers(dest="eps_cmd", required=True)
    p_rev = eps_sub.add_parser("review", help="hand-check one month's episodes (local page)")
    p_rev.add_argument("--month", help="YYYY-MM (default: the last completed month)")
    p_ins = sub.add_parser("inspect", help="why an episode, a day or a label exists (local page)")
    ins = p_ins.add_mutually_exclusive_group(required=True)
    ins.add_argument("--episode", help="episode id (a prefix is enough)")
    ins.add_argument("--day", help="local YYYY-MM-DD: the episodes with a member row that day")
    ins.add_argument("--label", help="a label's unit_id (steer event id, <episode>:<date>, ...)")
    p_lab = sub.add_parser("label", help="local model labels (optional extra aut[label])")
    lab_sub = p_lab.add_subparsers(dest="label_cmd", required=True)
    p_lrun = lab_sub.add_parser("run", help="label new units since the watermark")
    p_lrun.add_argument("--task", choices=TASK_NAMES, help="one task (default: label_tasks)")
    p_lrun.add_argument("--since", type=date.fromisoformat,
                        help="local YYYY-MM-DD to start from instead of the watermark")
    p_lrun.add_argument("--dry-run", action="store_true", help="score and count, write nothing")
    p_lback = lab_sub.add_parser("backfill", help="label one task over the last N days")
    p_lback.add_argument("--task", choices=TASK_NAMES, required=True)
    p_lback.add_argument("--days", type=int, required=True)
    p_teach = lab_sub.add_parser("teach", help="teacher (silver) labels for a stratified sample")
    p_teach.add_argument("--task", choices=DECISION_TASKS, required=True)
    p_teach.add_argument("--n", type=int, required=True, help="units to label this run")
    p_teach.add_argument("--budget-usd", type=float, default=5.0,
                         help="cap on this run's Sonnet spend (default 5)")
    p_lrev = lab_sub.add_parser("review", help="label gold answers on a local page")
    p_lrev.add_argument("--task", choices=DECISION_TASKS)
    for name, text in (("eval", "score every backend on the gold labels"),
                       ("choose", "write the smallest backend that passes into label.json"),
                       ("train-encoder", "fit the small encoder on gold + silver")):
        lab_sub.add_parser(name, help=text).add_argument("--task", choices=DECISION_TASKS,
                                                         required=True)
    for sp in (p_teach, p_lrev, lab_sub.choices["eval"], lab_sub.choices["train-encoder"]):
        sp.add_argument("--days", type=int, default=DAYS,
                        help=f"window of whole past days the units come from (default {DAYS})")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        _dispatch(parser, args)
    except AutError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.cmd in _NO_CONFIG_COMMANDS:
        _NO_CONFIG_COMMANDS[args.cmd](args)
        return

    cfg = load_config()
    agents = cfg.agents
    if args.agents:
        agents = tuple(a.strip() for a in args.agents.split(",") if a.strip())
        bad = [a for a in agents if a not in ALL_AGENTS]
        if bad:
            parser.error(f"unknown agent(s): {bad}; valid = {list(ALL_AGENTS)}")
    if args.cmd == "backfill" and args.dry_run and not (args.sessions_only or args.activity_only):
        parser.error("backfill --dry-run needs --sessions-only or --activity-only (a plain "
                     "backfill resets the watermark; preview it with `aut scan --dry-run`)")

    _COMMANDS[args.cmd](cfg, agents, args)


if __name__ == "__main__":
    sys.exit(main())
