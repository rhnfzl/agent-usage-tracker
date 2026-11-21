"""Guide first-time configuration with local evidence and explicit application."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import setup_schedule
from .config import AutError, config_path, load_config
from .coverage import build_report
from .push import make_sender
from .state import _locked, atomic_write_text, state_path

# Keep the guided starter small. config.example.toml documents the optional features.
_STARTER = """# Local reporting works with this file as-is.
machine = "local"
agents = ["claude_code", "codex", "cursor", "cursor_cli", "pi"]

# Set these only if you want to send usage to your own sink.
[transport]
# webhook_url = "https://your-sink.example/webhook/llm-usage"
# webhook_secret = "env:AUT_WEBHOOK_SECRET"
# pg_dsn = "env:AUT_PG_DSN"
"""


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("setup", help="preview local setup or explicitly configure it")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="create config if missing")
    mode.add_argument("--uninstall", action="store_true", help="remove only the setup-managed timer")
    p.add_argument("--schedule", action="store_true", help="include the sink delivery timer")
    p.add_argument("--json", action="store_true", help="print the local setup receipt as JSON")


def _existing_config(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _create_config(path: Path) -> str:
    if _existing_config(path):
        return "kept"
    with _locked(path.with_name("setup-config")):
        if _existing_config(path):
            return "kept"
        atomic_write_text(path, _STARTER)
        return "created"


def _installed_aut() -> Path | None:
    entry = Path(sys.argv[0])
    if (entry.name.casefold() in {"aut", "aut.exe", "agent-usage-tracker", "agent-usage-tracker.exe"}
            and entry.is_file()):
        return entry.absolute()
    path = shutil.which("aut")
    return Path(path).absolute() if path else None


def _standard_paths() -> bool:
    home = Path.home()
    return (config_path() == home / ".config" / "agent-usage-tracker" / "config.toml"
            and state_path() == home / ".local" / "state" / "agent-usage-tracker" / "state.json"
            and not os.environ.get("XDG_DATA_HOME"))


def receipt(args: argparse.Namespace) -> dict[str, Any]:
    path = config_path()
    config_action = "kept" if _existing_config(path) else "would_create"
    if args.apply:
        config_action = _create_config(path)
    cfg = load_config()
    transport = bool(cfg.has_webhook or cfg.has_pg)
    schedule_mode = "uninstall" if args.uninstall else "install" if args.apply else "preview"
    schedule_status = "not_requested"
    if args.uninstall:
        schedule_status = setup_schedule.change("uninstall", None)
    elif args.schedule:
        if not setup_schedule.manager():
            schedule_status = "unavailable"
        elif not transport:
            schedule_status = "needs_sink"
        elif not _standard_paths():
            schedule_status = "needs_standard_paths"
        else:
            try:
                make_sender(cfg)
            except AutError:
                if args.apply:
                    raise
                schedule_status = "needs_valid_sink"
            else:
                schedule_status = (setup_schedule.change("install", _installed_aut())
                                   if args.apply else "would_install")
    health = build_report(cfg)
    collectors = health["collectors"]
    queue = health["delivery"]
    enabled = set(cfg.agents)
    observed = {item["collector"]: item["observation"]["receipt_status"]
                for item in collectors if item["configured"]}
    return {
        "version": 1,
        "configured": {"status": "present" if _existing_config(path) else "missing",
                       "action": config_action, "path": str(path),
                       "collectors": list(cfg.agents), "sink_configured": transport},
        "observed": {"scan_status": "observed" if any(
            state in {"fresh", "stale"} for state in observed.values()) else "not_observed",
            "scan_receipts": observed,
            "sources": [{"id": item["id"], "availability": item["availability"]}
                        for item in health["sources"] if item["engine"] in enabled]},
        "delivered": {"status": "pending" if queue["pending"] else
                      "unverified" if transport else "not_configured",
                      "queue_status": queue["status"], "pending": queue["pending"],
                      "scope": "local_queue_only"},
        "schedule": {"mode": schedule_mode, "manager": setup_schedule.manager(),
                     "status": schedule_status, "guidance": setup_schedule.MANUAL
                     if schedule_status in {"unavailable", "needs_standard_paths"} else None},
    }


def run(args: argparse.Namespace) -> None:
    report = receipt(args)
    if args.json:
        print(json.dumps(report, sort_keys=True))
        return
    configured, observed = report["configured"], report["observed"]
    delivered, schedule = report["delivered"], report["schedule"]
    print(f"Configured  {configured['status']} ({configured['action']}) at {configured['path']}")
    print(f"Observed    {observed['scan_status'].replace('_', ' ')}")
    for source in observed["sources"]:
        print(f"  {source['id']}  {source['availability']}")
    print(f"Delivered   {delivered['status'].replace('_', ' ')} "
          f"(local queue {delivered['queue_status']})")
    if delivered["pending"]:
        print(f"  {sum(delivered['pending'].values()):,} rows pending")
    print(f"Schedule    {schedule['status'].replace('_', ' ')}")
    if schedule["status"] == "needs_sink":
        print("Configure a sink before installing a scheduled scan.")
    if schedule["status"] == "needs_valid_sink":
        print("Run aut status to check the configured sink before scheduling.")
    if schedule["guidance"]:
        print(schedule["guidance"])
    if not args.apply and not args.uninstall:
        print("Run aut setup --apply to create the starter config.")
    print("Run aut demo to try sample usage or aut doctor for source health.")
