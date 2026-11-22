"""Capture and share evidence without exposing write operations to MCP clients."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from ..config import AutError
from ..label.core import data_dir
from .access import MODES, Evidence
from .store import Store, audit


def register(commands: Any) -> None:
    parser = commands.add_parser("evidence", help="search, compare and share captured agent sessions")
    parser.add_argument("--root", type=Path, help="private AUT improvement store")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--audit", help="freeze access to the selected audit ID")
    scope.add_argument("--bundle", type=Path, help="open a previously exported evidence bundle")
    parser.add_argument("--content", choices=MODES, default="full",
                        help="content available through this command or MCP connection")
    actions = parser.add_subparsers(dest="evidence_cmd", required=True)

    capture = actions.add_parser("capture", help="save a private audit with full text by default")
    capture.add_argument("--days", type=int, default=7)
    capture.add_argument("--limit", type=int, default=20)
    capture.add_argument("--idle-s", type=int, default=120)
    capture.add_argument("--project", type=Path)

    actions.add_parser("describe", help="report the evidence scope and coverage")
    search = actions.add_parser("search", help="filter captured events and return matching references")
    for arg in ("query", "agent", "kind", "status", "since", "until", "cursor", "tool", "workspace"):
        search.add_argument(f"--{arg}", default="")
    search.add_argument("--limit", type=int, default=20)

    session = actions.add_parser("session", help="read a page of events from a session")
    event = actions.add_parser("event", help="read text from one captured event")
    event.add_argument("session_id")
    event.add_argument("event_id")
    session.add_argument("session_id")
    for action, limit in ((event, 8_000), (session, 30)):
        action.add_argument("--offset", type=int, default=0)
        action.add_argument("--limit", type=int, default=limit)

    compare = actions.add_parser("compare", help="compare observed failures and activity in two sessions")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.add_argument("--cases", action="store_true", help="compare saved attempts by case ID")
    select = actions.add_parser("freeze-case", help="save the attempt around an event for repair review")
    select.add_argument("session_id")
    select.add_argument("event_id")
    case = actions.add_parser("case", help="resolve a saved improvement case to evidence references")
    case.add_argument("case_id")
    freeze = actions.add_parser("freeze-cases", help="save observed failures and controls as improvement cases")
    freeze.add_argument("--limit", type=int, default=30)
    export = actions.add_parser("export", help="write a private bundle of explicitly selected sessions")
    export.add_argument("path", type=Path)
    export.add_argument("ids", nargs="+")
    mcp = actions.add_parser("mcp", help="serve the selected evidence through MCP")
    mcp.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    mcp.add_argument("--port", type=int, default=8765, help="loopback port for HTTP")


def run(args: argparse.Namespace) -> None:
    root = args.root or Path(os.environ.get("AUT_IMPROVE_ROOT", str(data_dir() / "improve")))
    action = args.evidence_cmd
    if action == "capture":
        if args.audit or args.bundle or args.content == "metadata":
            raise AutError("evidence: capture takes no scope and requires full or redacted content")
        value = audit(Store(root), Path.home(), days=args.days, limit=args.limit,
                      idle_s=args.idle_s, project=args.project, capture_full=args.content == "full")
    else:
        if not (args.audit or args.bundle):
            raise AutError("evidence: select --audit ID or --bundle PATH")
        store = Store(root, read_only=True) if args.audit else None
        evidence = Evidence(args.content, store, args.audit, args.bundle)
        if action == "mcp":
            try:
                from .access_mcp import serve
            except ModuleNotFoundError as exc:
                if exc.name not in {"mcp", "anyio", "uvicorn", "starlette"}:
                    raise
                raise AutError("evidence: install agent-usage-tracker[mcp] for MCP access") from exc
            serve(evidence, args.transport, args.port)
            return
        if action == "freeze-case":
            if store is None:
                raise AutError("evidence: freezing cases requires an audit in the private store")
            from .judge import freeze_case
            key = freeze_case(Store(root), args.audit, args.session_id, args.event_id)
            value = evidence.case(key)
        elif action == "freeze-cases":
            if store is None:
                raise AutError("evidence: freezing cases requires an audit in the private store")
            from .judge import cases
            rows = cases(Store(root), args.audit, limit=args.limit)
            value = {"audit_id": args.audit,
                     "cases": [evidence.case(row["case_id"]) for row in rows]}
        elif action == "search":
            value = evidence.search(args.query, args.agent, args.kind, args.status, args.since,
                                    args.until, args.cursor, args.limit, args.tool, args.workspace)
        elif action == "event":
            value = evidence.event(args.session_id, args.event_id, args.offset, args.limit)
        elif action == "session":
            value = evidence.session(args.session_id, args.offset, args.limit)
        elif action == "compare":
            value = evidence.compare(args.baseline, args.candidate, args.cases)
        elif action == "case":
            value = evidence.case(args.case_id)
        elif action == "export":
            value = evidence.export(args.path, args.ids)
        else:
            value = evidence.describe()
    print(json.dumps(value, sort_keys=True))
