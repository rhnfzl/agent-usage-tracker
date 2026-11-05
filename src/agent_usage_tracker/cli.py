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
  aut time add --start <local> --end <local> | --minutes N [--at <local end>] [--not-relevant]
                        record a manual active-time interval (needs transport.pg_dsn)
  aut outcomes [--fetch] [--dry-run]
                        score every commit row against the local clones -> agents.commit_outcome
  aut outcome <shipped|ruled-out|open> [--session ID]
                        the manual outcome of this project's newest interactive session
  aut history-steers [--dry-run]
                        daily steer estimates for lost 2026 logs, from the CLI history files
  aut episodes review [--month YYYY-MM]
                        hand-check a month's episodes on a local page -> agents.episode_correction
  aut inspect --episode ID | --day YYYY-MM-DD | --label UNIT_ID
                        why an episode, a day's episodes or a label exist, on a local evidence page
                        (its linked correction form writes agents.episode_correction)
  aut label run [--task T] [--since YYYY-MM-DD] [--dry-run]
                        a local model labels new units (optional extra aut[label]) -> label rows
  aut label backfill --task T --days N
                        the same over the last N days; the nightly watermark is not moved back
  aut label teach --task T --n N [--budget-usd X]
                        teacher (silver) labels for a stratified sample -> eval/<task>.silver.jsonl
  aut label review [--task T]
                        label gold answers on a local page -> eval/<task>.jsonl (never text)
  aut label eval --task T [--final --model M]
                        bake-off on gold -> saved scores, eval record, one table per task
  aut label choose --task T [--model M] | train-encoder --task T
                        the table, or M's saved result -> label.json; the encoder
  aut label ensemble --task T [--days D] [--models A,B,C]
                        odd combinations of models from saved scores -> their records, one table

The dry-run report doubles as the accuracy check: it prints per-agent, per-model token
sums straight from your real logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from typing import Sequence

from . import inspector, manual_time, outcomes, pricing, review, scan
from .collectors import REGISTRY
from .config import (ALL_AGENTS, LABEL_DAYS, LABEL_DECISION_TASKS, LABEL_TASK_NAMES, AutError,
                     Config, load_config, config_path)
from .push import is_operational_failure, redacted_error, webhook_url_allowed
from .state import collector_wm, load_state


def _fail_if_partial(what: str, failed: set[str]) -> None:
    """A one-shot command has no heartbeat, so its exit code is what stops `aut export && load`."""
    if failed:
        raise AutError(f"{what}: partial, could not fully read {', '.join(sorted(failed))} "
                       "(reasons above)")


def cmd_scan(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    report = scan.run_scan(cfg, agents, dry_run=args.dry_run)
    scan.print_scan(cfg, report, dry_run=args.dry_run)


def cmd_backfill(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    if args.sessions_only or args.activity_only:
        counts = scan.run_history_backfill(cfg, agents, dry_run=args.dry_run,
                                           sessions=args.sessions_only, activity=args.activity_only)
        scan.print_history_backfill(counts, dry_run=args.dry_run, sessions=args.sessions_only,
                                    activity=args.activity_only)
        _fail_if_partial("backfill", counts["failed"])
        return
    scan.reset_watermarks(agents)
    print(f"watermark reset for: {', '.join(agents)}")
    cmd_scan(cfg, agents, args)


def cmd_export(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        written, skipped, failed = scan.run_export(cfg, agents, out)
    finally:
        if args.out:
            out.close()
    scan.print_export(written, skipped, args.out)
    _fail_if_partial("export", failed)


def cmd_status(cfg: Config, agents: Sequence[str], args: argparse.Namespace) -> None:
    state = load_state()
    print(f"machine       : {cfg.machine}")
    print(f"agents        : {', '.join(agents)}")
    if cfg.has_webhook and not cfg.webhook_secret:
        webhook_state = "url set but SECRET MISSING (scan will refuse)"
    elif cfg.has_webhook and not webhook_url_allowed(cfg.webhook_url or ""):
        webhook_state = "url NOT ALLOWED: https://, or http:// to localhost (scan will refuse)"
    else:
        webhook_state = "configured" if cfg.has_webhook else "NOT set"
    print(f"webhook       : {webhook_state}")
    print(f"pg fallback   : {'configured' if cfg.has_pg else 'not set'}")
    print("watermarks    :")
    for name in agents:
        print(f"    {name:<12} {REGISTRY[name].describe(cfg, collector_wm(state, name))}")


def cmd_time_add(cfg: Config, args: argparse.Namespace) -> None:
    span = manual_time.ManualSpan(args.start, args.end, args.minutes, args.at)
    manual_time.print_time_add(manual_time.run_time_add(cfg, span, not_relevant=args.not_relevant))


def cmd_outcomes(cfg: Config, args: argparse.Namespace) -> None:
    report = outcomes.run_outcomes(cfg, refresh=args.fetch or cfg.outcome_fetch,
                                   dry_run=args.dry_run)
    outcomes.print_outcomes(report, dry_run=args.dry_run)


def cmd_outcome(cfg: Config, args: argparse.Namespace) -> None:
    dsn = cfg.require_pg("outcome")
    row = outcomes.build_manual_outcome(cfg, args.outcome, os.getcwd(), args.session)
    outcomes.insert_manual_outcome(dsn, row)
    print(f"marked session {row['session_id'][:8]} {row['outcome']}")


def cmd_inspect(cfg: Config, args: argparse.Namespace) -> None:
    selected: tuple[inspector.Selector, str] = (
        ("episode", args.episode) if args.episode is not None else
        ("day", args.day) if args.day is not None else ("label", args.label))
    inspector.print_inspect(inspector.run_inspect(cfg, *selected))


def cmd_episodes(cfg: Config, args: argparse.Namespace) -> None:
    review.print_review(review.run_episode_review(cfg, args.month))


# The label modules are imported where used, so every other command skips loading their code (a
# test holds the CLI to that). The collectors load regardless, for scan and REGISTRY above.
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
    label_run.print_report(report, dry_run=dry)
    failed = [name for name, c in report.items() if c["failed"]]
    if failed:
        raise AutError(f"label: could not run {', '.join(failed)} (reasons above)")


def _label_bakeoff(cfg: Config, args: argparse.Namespace) -> None:
    """eval | choose | train-encoder: the bake-off on the gold labels."""
    from .label import bakeoff
    if args.label_cmd == "eval":
        if args.final or args.model:
            if not (args.final and args.model):
                raise AutError("label eval: --final and --model go together")
            bakeoff.print_final(bakeoff.run_final(cfg, args.task, args.model, args.days))
        else:
            bakeoff.print_eval(cfg, args.task, bakeoff.run_eval(cfg, args.task, args.days))
    elif args.label_cmd == "choose":
        chosen = bakeoff.choose(cfg, args.task, args.model)
        if chosen:
            bakeoff.print_choose(args.task, chosen)
    else:
        bakeoff.print_train_encoder(args.task, bakeoff.train_encoder(cfg, args.task, args.days))


def cmd_label(cfg: Config, args: argparse.Namespace) -> None:
    if args.label_cmd in ("run", "backfill"):
        _label_run(cfg, args)
    elif args.label_cmd == "ensemble":
        from .label import ensemble
        ensemble.print_ensemble(cfg, args.task, *ensemble.run_ensemble(
            cfg, args.task, args.days, models=args.models.split(",") if args.models else None))
    elif args.label_cmd == "teach":
        from .label import teach
        teach.print_teach(args.task, teach.run_teach(cfg, args.task, args.n,
                                                     budget_usd=args.budget_usd, days=args.days,
                                                     sonnet_only=args.sonnet_only),
                          args.budget_usd)
    elif args.label_cmd == "review":
        from .label import gold
        gold.print_review(gold.run_gold_review(cfg, args.task, args.days))
    else:
        _label_bakeoff(cfg, args)


def cmd_history_steers(cfg: Config, args: argparse.Namespace) -> None:
    from . import history_steers
    c = history_steers.run(cfg, args.dry_run)
    print(f"history-steers {'DRY-RUN (nothing written)' if args.dry_run else 'written'}: "
          f"{json.dumps(dict(c))}")


def cmd_sync_prices(args: argparse.Namespace) -> None:
    out = args.out or str(config_path().parent / "model_prices.json")
    n = pricing.sync_prices(out, litellm_url=args.litellm_url, models_dev_url=args.models_dev_url,
                            genai_prices_url=args.genai_prices_url)
    print(f"synced {n} model prices -> {out}", file=sys.stderr)


_NO_CONFIG_COMMANDS = {"sync-prices": cmd_sync_prices}
_AGENT_COMMANDS = {"scan": cmd_scan, "backfill": cmd_backfill, "export": cmd_export,
                   "status": cmd_status}
_COMMANDS = {"time": cmd_time_add, "outcomes": cmd_outcomes, "outcome": cmd_outcome,
             "inspect": cmd_inspect, "episodes": cmd_episodes, "label": cmd_label,
             "history-steers": cmd_history_steers}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aut", description="passive coding-agent usage, activity, active-time and outcome tracker")
    agents_help = "comma-separated subset override (default: config)"
    parser.add_argument("--agents", help=f"{agents_help}; for scan, backfill, export and status")
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
    p_status = sub.add_parser("status", help="show config + watermark state")
    p_export = sub.add_parser("export", help="write all usage as CSV (bulk load / offline bridge)")
    p_export.add_argument("--out", help="output CSV file (default: stdout)")
    for sp in (p_scan, p_back, p_status, p_export):     # accepted after the subcommand too
        sp.add_argument("--agents", default=argparse.SUPPRESS, help=agents_help)
    p_sync = sub.add_parser("sync-prices", help="refresh the API-equivalent model price map")
    p_sync.add_argument("--out", help="output JSON (default: <config-dir>/model_prices.json)")
    p_sync.add_argument("--litellm-url", default=pricing.LITELLM_URL)
    p_sync.add_argument("--models-dev-url", default=pricing.MODELS_DEV_URL)
    p_sync.add_argument("--genai-prices-url", default=pricing.GENAI_PRICES_URL)
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
    p_hist = sub.add_parser("history-steers",
                            help="daily steer estimates for lost 2026 logs, from CLI history")
    p_hist.add_argument("--dry-run", action="store_true", help="count, write nothing")
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
    p_lrun.add_argument("--task", choices=LABEL_TASK_NAMES, help="one task (default: label_tasks)")
    p_lrun.add_argument("--since", type=date.fromisoformat,
                        help="local YYYY-MM-DD to start from instead of the watermark")
    p_lrun.add_argument("--dry-run", action="store_true", help="score and count, write nothing")
    p_lback = lab_sub.add_parser("backfill", help="label one task over the last N days")
    p_lback.add_argument("--task", choices=LABEL_TASK_NAMES, required=True)
    p_lback.add_argument("--days", type=int, required=True)
    p_teach = lab_sub.add_parser("teach", help="teacher (silver) labels for a stratified sample")
    p_teach.add_argument("--task", choices=LABEL_DECISION_TASKS, required=True)
    p_teach.add_argument("--n", type=int, required=True, help="units to label this run")
    p_teach.add_argument("--budget-usd", type=float, default=5.0,
                         help="cap on this run's Sonnet spend (default 5)")
    p_teach.add_argument("--sonnet-only", action="store_true",
                         help="skip the local teacher, and every unit Sonnet may not read")
    p_lrev = lab_sub.add_parser("review", help="label gold answers on a local page")
    p_lrev.add_argument("--task", choices=LABEL_DECISION_TASKS)
    for name, text in (("eval", "score every backend on the gold labels; print the table"),
                       ("choose", "print the table, or write --model's result to label.json"),
                       ("ensemble", "odd combinations of models, from saved scores"),
                       ("train-encoder", "fit the small encoder on gold + silver")):
        lab_sub.add_parser(name, help=text).add_argument("--task", choices=LABEL_DECISION_TASKS,
                                                         required=True)
    for sp in (p_teach, p_lrev, lab_sub.choices["eval"], lab_sub.choices["ensemble"],
               lab_sub.choices["train-encoder"]):
        sp.add_argument("--days", type=int, default=LABEL_DAYS,
                        help=f"window of whole past days the units come from (default {LABEL_DAYS})")
    lab_sub.choices["eval"].add_argument("--final", action="store_true",
                                         help="score --model on the final test days")
    lab_sub.choices["ensemble"].add_argument(
        "--models", help="comma-separated: only these models (default: all with an eval record)")
    for name in ("eval", "choose"):
        lab_sub.choices[name].add_argument("--model", help="a name from the table, or ensemble:<id>")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        _dispatch(parser, args)
    except AutError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        if not is_operational_failure(exc):     # a bug keeps its traceback
            raise
        print(f"aut: {redacted_error(exc)}", file=sys.stderr)
        return 1
    return 0


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.agents and args.cmd not in _AGENT_COMMANDS:     # it would run over everything
        parser.error(f"--agents does not apply to {args.cmd}")
    if args.cmd in _NO_CONFIG_COMMANDS:
        _NO_CONFIG_COMMANDS[args.cmd](args)
        return

    cfg = load_config()
    if args.cmd in _COMMANDS:
        _COMMANDS[args.cmd](cfg, args)
        return
    agents = cfg.agents
    if args.agents:
        agents = tuple(a.strip() for a in args.agents.split(",") if a.strip())
        bad = [a for a in agents if a not in ALL_AGENTS]
        if bad:
            parser.error(f"unknown agent(s): {bad}; valid = {list(ALL_AGENTS)}")
    if args.cmd == "backfill" and args.dry_run and not (args.sessions_only or args.activity_only):
        parser.error("backfill --dry-run needs --sessions-only or --activity-only (a plain "
                     "backfill resets the watermark; preview it with `aut scan --dry-run`)")

    _AGENT_COMMANDS[args.cmd](cfg, agents, args)


if __name__ == "__main__":
    sys.exit(main())
