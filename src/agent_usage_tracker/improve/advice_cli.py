"""Expose the same advice rule to evidence replay and Claude Code."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ..config import AutError
from ..label.core import data_dir
from .advice_setup import CLIENTS


def advice_root() -> Path:
    return Path(os.environ.get("AUT_ADVICE_ROOT", str(data_dir() / "session-advice"))).expanduser()


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("advise", help="replay or record private session advice")
    actions = parser.add_subparsers(dest="advice_cmd", required=True)
    replay = actions.add_parser("replay", help="evaluate one event in a frozen audit")
    for name in ("audit", "session", "event", "revision"):
        replay.add_argument(f"--{name}", required=True)
    replay.add_argument("--project", type=Path, required=True)
    replay.add_argument("--root", type=Path, help="private evidence store for replay")
    setup = actions.add_parser("setup", help="preview or add user-level hooks for installed clients")
    setup.add_argument("--apply", action="store_true")
    setup.add_argument("--mode", choices=("record", "show"), default="record")
    setup.add_argument("--client", choices=CLIENTS, action="append")
    hook = actions.add_parser("hook", aliases=["claude-hook"], help="read a coding client hook from stdin")
    hook.add_argument("--project", type=Path, help="restrict this hook to one absolute project root")
    hook.add_argument("--client", choices=CLIENTS, default="claude")
    hook.add_argument("--mode", choices=("record", "show"), default="record")
    hook.add_argument("--root", type=Path, help="private advice receipt directory")
    review = actions.add_parser("acceptance", help="record an independently checked task outcome")
    review.add_argument("observation", help="observation ID or the prefix shown in AUT overview")
    review.add_argument("--result", required=True, choices=("accepted", "rejected"))
    review.add_argument("--root", type=Path, help="private advice receipt directory")


def _replay(args: argparse.Namespace) -> dict[str, Any]:
    from .access import Evidence
    from .advice import REVISION, RULE, WORKFLOW, committed_ci, pytest_command, recommend, tool_output
    from .store import Store

    if not REVISION.fullmatch(args.revision):
        raise AutError("advise: replay requires a full Git commit ID")
    root = args.root or Path(os.environ.get("AUT_IMPROVE_ROOT", str(data_dir() / "improve")))
    evidence = Evidence("redacted", Store(root, read_only=True), args.audit)
    pair = evidence.tool_result_at(args.session, args.event)
    quiet = {"status": "quiet"}
    if pair is None:
        return quiet
    call, result = pair
    if (call.get("tool") not in {"Bash", "functions.exec_command", "exec_command"}
            or result.get("status") != "error" or call.get("truncated") or result.get("truncated")):
        return quiet
    try:
        tool_input = json.loads(call["text"])
    except ValueError:
        return quiet
    value = tool_input.get("command") if isinstance(tool_input, dict) else None
    command = pytest_command(value) if isinstance(value, str) else None
    output = result["text"]
    if output.startswith("[{"):
        try:
            output = tool_output(json.loads(output)) or ""
        except ValueError:
            return quiet
    if command and (ci := committed_ci(args.project, args.revision)) and recommend(
        command, output, ci
    ):
        return {
            "status": "suggestion", "rule": RULE, "command": ci.command,
            "evidence": {"audit_id": args.audit, "session_id": args.session, "event_id": args.event,
                         "call_id": call["id"], "workflow": WORKFLOW, "line": ci.line,
                         "revision": ci.revision},
            "check": "Run from the project root and check whether pytest reaches the tests.",
        }
    return quiet


def run(args: argparse.Namespace) -> None:
    if args.advice_cmd == "replay":
        print(json.dumps(_replay(args), sort_keys=True))
        return
    if args.advice_cmd == "setup":
        from .advice_setup import configure

        executable = Path(sys.argv[0]).absolute()
        if executable.name != "aut" or not executable.is_file():
            raise AutError("advise: run setup from a persistently installed aut executable")
        receipts = configure(Path.home(), executable, apply=args.apply, mode=args.mode,
                             clients=args.client)
        print(json.dumps({"mode": args.mode, "clients": receipts}, indent=2))
        if any(row["status"] == "unavailable" for row in receipts):
            raise AutError("advise: one or more client configs could not be updated")
        return
    from .advice_hook import HOOK_LIMIT, handle_batch, record_acceptance

    root = args.root or advice_root()
    if args.advice_cmd == "acceptance":
        try:
            ref = record_acceptance(root, args.observation, args.result)
        except (OSError, sqlite3.Error) as exc:
            raise AutError("advise: cannot read or update the local advice receipts") from exc
        print(json.dumps({"observation": ref, "acceptance": args.result}, sort_keys=True))
        return

    result = handle_batch(sys.stdin.buffer.read(HOOK_LIMIT + 1), root, args.project, args.mode, args.client)
    print(json.dumps(result, sort_keys=True))
