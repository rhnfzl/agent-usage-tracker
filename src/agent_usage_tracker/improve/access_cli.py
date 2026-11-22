"""Capture and share evidence without exposing write operations to MCP clients."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..config import AutError
from ..label.core import data_dir
from .access import MODES, Evidence
from .evidence import BYTE_LIMIT
from .ranked_search import DEFAULT_SEARCH_MODE, SEARCH_MODES
from .search_eval import evaluate
from .store import Store, audit

_MIB = 1024 * 1024
_MAX_CAPTURE_MIB = 256


def register(commands: Any) -> None:
    parser = commands.add_parser("evidence", help="search, compare and share captured agent sessions")
    parser.add_argument("--root", type=Path, help="private AUT improvement store")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--audit", help="freeze access to the selected audit ID")
    scope.add_argument("--bundle", type=Path, help="open a previously exported evidence bundle")
    parser.add_argument("--content", choices=MODES,
                        help="content available through this command or MCP connection")
    parser.add_argument("--embedding-model", type=Path, help="local sentence model directory")
    actions = parser.add_subparsers(dest="evidence_cmd", required=True)

    discover = actions.add_parser("discover", help="investigate open questions locally across audits")
    discover.add_argument("--include-audit", action="append", default=[])
    discover.add_argument("--question", help="optional direction for the investigation")
    discover.add_argument("--model", help="cached local instruct model")
    discover.add_argument("--rounds", type=int, choices=(1, 2, 3), default=2)

    capture = actions.add_parser("capture", help="save a private audit with full text by default")
    capture.add_argument("--days", type=int, default=7)
    capture.add_argument("--limit", type=int, default=20)
    capture.add_argument("--idle-s", type=int, default=120)
    capture.add_argument("--project", type=Path)
    capture.add_argument("--no-observe", action="store_true", help="keep this capture local")
    actions.add_parser("observe", help="send the selected audit or bundle to session observability")
    capture.add_argument("--max-mib", type=int, default=BYTE_LIMIT // _MIB,
                         help="maximum MiB read per source (1..256, default: 16)")
    record = actions.add_parser("record-codex-turn", help="record Git context from a Codex prompt hook")
    record.add_argument("--project", type=Path, required=True)
    record.add_argument("--setup-file", action="append", default=[], metavar="PROJECT_PATH",
                        help="record the file digest or absence for this project-relative setup path")

    describe = actions.add_parser("describe", help="report the evidence scope and coverage")
    describe.add_argument("--text-coverage", action="store_true")
    search = actions.add_parser("search", help="filter captured events and return matching references")
    for arg in ("query", "agent", "kind", "status", "since", "until", "cursor", "tool", "workspace"):
        search.add_argument(f"--{arg}", default="")
    search.add_argument("--session-id", default="")
    search.add_argument("--exclude-session", dest="exclude_sessions", action="append", default=[])
    search.add_argument("--group-by-session", action="store_true")
    search.add_argument("--argument-path", help="JSON pointer into a recorded tool call")
    search.add_argument("--mode", choices=SEARCH_MODES, default=DEFAULT_SEARCH_MODE)
    search.add_argument("--limit", type=int, default=20)

    evaluation = actions.add_parser("evaluate", help="measure search against a local labeled set")
    evaluation.add_argument("path", type=Path)
    evaluation.add_argument("--mode", choices=SEARCH_MODES, action="append")
    evaluation.add_argument("--limit", type=int, default=5)

    listing = actions.add_parser("list-sessions", help="list captured sessions and their coverage")
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=30)
    listing.add_argument("--agent", default="")
    listing.add_argument("--workspace", default="")

    session = actions.add_parser("session", help="read a page of events from a session")
    event = actions.add_parser("event", help="read text from one captured event")
    event.add_argument("session_id")
    event.add_argument("event_id")
    session.add_argument("session_id")
    for action, limit in ((event, 8_000), (session, 30)):
        action.add_argument("--offset", type=int, default=0)
        action.add_argument("--limit", type=int, default=limit)

    context = actions.add_parser("context", help="read an event with its recorded counterpart")
    context.add_argument("session_id")
    context.add_argument("event_id")
    context.add_argument("--radius", type=int, default=2)
    context.add_argument("--limit", type=int, default=2_000)
    context.add_argument("--focus", default="", help="center the anchor excerpt near these words")
    context.add_argument("--view", choices=("nearby", "conversation"), default="nearby")

    commits = actions.add_parser("commits", help="find runs by a recorded Git result or checkpoint commit")
    commits.add_argument("commit")
    commits.add_argument("--source", choices=("command", "checkpoint"), default="command")
    commits.add_argument("--offset", type=int, default=0)
    commits.add_argument("--limit", type=int, default=30)

    retries = actions.add_parser("retries", help="find repeated and changed requests after tool errors")
    retries.add_argument("--minimum-sessions", type=int, default=2)
    retries.add_argument("--limit", type=int, default=20)
    retries.add_argument("--tool", default="")

    compare = actions.add_parser("compare", help="compare observed failures and activity in two sessions")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.add_argument("--cases", action="store_true", help="compare saved attempts by case ID")
    select = actions.add_parser("freeze-case", help="save the attempt around an event for repair review")
    select.add_argument("session_id")
    select.add_argument("event_id")
    skill = actions.add_parser("propose-skill", help="draft a project skill from corroborated evidence")
    skill.add_argument("--scope", type=Path, required=True)
    skill.add_argument("--target", required=True)
    skill.add_argument("--draft-file", type=Path, required=True)
    skill.add_argument("--proof", action="append", required=True, metavar="CASE_ID:EVENT_ID")
    skill.add_argument("--candidate", type=Path)
    case = actions.add_parser("case", help="resolve a saved improvement case to evidence references")
    case.add_argument("case_id")
    ordinary = actions.add_parser("ordinary-work", help="find attempts that can supply study measurements")
    from .study_observe import COVERAGE_BASES, METRICS
    ordinary.add_argument("--metric", choices=METRICS, default="tool_calls")
    ordinary.add_argument("--coverage-basis", choices=COVERAGE_BASES)
    ordinary.add_argument("--require-turn-context", action="store_true")
    ordinary.add_argument("--include-excluded", action="store_true")
    for option in ("agent", "before", "cursor"):
        ordinary.add_argument(f"--{option}", default="")
    ordinary.add_argument("--limit", type=int, default=30)
    freeze = actions.add_parser("freeze-cases", help="save observed failures and controls as improvement cases")
    freeze.add_argument("--limit", type=int, default=30)
    export = actions.add_parser("export", help="write a private bundle of explicitly selected sessions")
    export.add_argument("path", type=Path)
    export.add_argument("ids", nargs="+")
    mcp = actions.add_parser("mcp", help="serve the selected evidence through MCP")
    mcp.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    code = actions.add_parser("run-code", help="run a local evidence recipe")
    code.add_argument("source", nargs="?", help="Python recipe path or - for stdin")
    code.add_argument("--recipe", help="reuse the source from a saved run")
    code.add_argument("--params", help="JSON object passed to the program")
    code.add_argument("--save", action="store_true", help="save private recipe and replay record")
    replay = actions.add_parser("replay-code", help="replay a saved investigation")
    replay.add_argument("run_id")
    actions.add_parser("code-catalog", help="list operations available to recipes")
    mcp.add_argument("--code", action="store_true", help="enable programmable investigations")
    for action in (code, replay, mcp):
        action.add_argument("--include-audit", action="append", default=[])
        action.add_argument("--include-bundle", action="append", type=Path, default=[])
    mcp.add_argument("--port", type=int, default=8765, help="loopback port for HTTP")


def run(args: argparse.Namespace) -> None:
    root = args.root or Path(os.environ.get("AUT_IMPROVE_ROOT", str(data_dir() / "improve")))
    action = args.evidence_cmd
    if action == "discover":
        if args.bundle or args.content not in (None, "redacted"):
            raise AutError("discovery: use redacted audits in a private local store")
        from .discovery import run as discover

        value = discover(Store(root), args.audit, args.include_audit, question=args.question,
                         model_name=args.model, rounds=args.rounds)
        print(json.dumps(value, sort_keys=True))
        return
    configured_model = args.embedding_model or os.environ.get("AUT_EVIDENCE_EMBEDDING_MODEL")
    model_path = Path(configured_model).expanduser().resolve() if configured_model else None
    with_code = action in {"run-code", "replay-code", "code-catalog"} or (
        action == "mcp" and args.code)
    args.content = args.content or ("redacted" if with_code else "full")
    if action == "code-catalog" or action in {"run-code", "replay-code"}:
        value = _code(args, root, model_path)
        print(json.dumps(value, sort_keys=True))
        return
    if action == "record-codex-turn":
        if args.audit or args.bundle:
            raise AutError("evidence: the Codex turn hook uses a store, not an audit or bundle")
        from .turn_revisions import HOOK_INPUT_LIMIT, record_hook

        record_hook(root, args.project, sys.stdin.buffer.read(HOOK_INPUT_LIMIT + 1), tuple(args.setup_file))
        return
    if action == "capture":
        if args.audit or args.bundle or args.content == "metadata":
            raise AutError("evidence: capture takes no scope and requires full or redacted content")
        if not 1 <= args.max_mib <= _MAX_CAPTURE_MIB:
            raise AutError("evidence: use a per-source capture budget of 1..256 MiB")
        writable = Store(root)
        value = audit(writable, Path.home(), days=args.days, limit=args.limit,
                      idle_s=args.idle_s, project=args.project, capture_full=args.content == "full",
                      max_bytes=args.max_mib * _MIB)
        if not args.no_observe and os.environ.get("AUT_SESSION_OBSERVABILITY_URL"):
            from .session_observability import publish
            try:
                value["observability"] = publish(args.content, writable, value["audit_id"], None)
            except AutError as exc:
                value["observability"] = {"status": "failed", "error": str(exc)}
    else:
        if not (args.audit or args.bundle):
            raise AutError("evidence: select --audit ID or --bundle PATH")
        store = Store(root, read_only=True) if args.audit else None
        if action == "observe":
            from .session_observability import publish
            print(json.dumps(publish(args.content, store, args.audit, args.bundle), sort_keys=True))
            return
        from .semantic_search import LocalEmbedder

        embedder = LocalEmbedder(model_path) if model_path else None
        evidence = Evidence(args.content, store, args.audit, args.bundle, embedder=embedder)
        if action == "mcp":
            try:
                from .access_mcp import serve
            except ModuleNotFoundError as exc:
                if exc.name not in {"mcp", "anyio", "uvicorn", "starlette"}:
                    raise
                raise AutError("evidence: install agent-usage-tracker[mcp] for MCP access") from exc
            scopes = None
            if args.code:
                from .investigation import scope_specs
                scopes = scope_specs(root, args.content, args.audit, args.bundle,
                                     args.include_audit, args.include_bundle, model_path)
            elif args.include_audit or args.include_bundle:
                raise AutError("investigation: additional scopes require mcp --code")
            serve(evidence, args.transport, args.port, scopes)
            return
        if action == "propose-skill":
            if store is None:
                raise AutError("evidence: skill proposals require an audit in the private store")
            from .proposals import stage
            from .skill_proposals import propose_skill
            try:
                with args.draft_file.open("rb") as file:
                    draft = file.read(32_769).decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise AutError("evidence: skill draft must be a readable UTF-8 file") from exc
            writable = Store(root)
            key = propose_skill(writable, args.audit, args.scope, args.target, draft, args.proof)
            if args.candidate:
                stage(writable, key, args.candidate)
            value = {"proposal_id": key, "staged": bool(args.candidate)}
        elif action == "freeze-case":
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
            value = evidence.search(
                query=args.query, agent=args.agent, kind=args.kind, status=args.status,
                since=args.since, until=args.until, cursor=args.cursor, limit=args.limit,
                tool=args.tool, workspace=args.workspace, mode=args.mode, session_id=args.session_id,
                exclude_sessions=args.exclude_sessions, group_by_session=args.group_by_session,
                argument_path=args.argument_path,
            )
        elif action == "evaluate":
            value = evaluate(evidence, args.path, args.mode or ["auto", "exact", "ranked", "sessions"], args.limit)
        elif action == "event":
            value = evidence.event(args.session_id, args.event_id, args.offset, args.limit)
        elif action == "context":
            value = evidence.context(args.session_id, args.event_id, args.radius, args.limit, args.focus, args.view)
        elif action == "list-sessions":
            value = evidence.list_sessions(args.offset, args.limit, args.agent, args.workspace)
        elif action == "session":
            value = evidence.session(args.session_id, args.offset, args.limit)
        elif action == "commits":
            value = evidence.commits(args.commit, args.offset, args.limit, args.source)
        elif action == "retries":
            value = evidence.retries(args.minimum_sessions, args.limit, args.tool)
        elif action == "compare":
            value = evidence.compare(args.baseline, args.candidate, args.cases)
        elif action == "case":
            value = evidence.case(args.case_id)
        elif action == "ordinary-work":
            value = evidence.ordinary_work(args.metric, args.coverage_basis, args.require_turn_context,
                                           args.agent, args.before, args.include_excluded, args.cursor, args.limit)
        elif action == "export":
            value = evidence.export(args.path, args.ids)
        else:
            value = evidence.describe(args.text_coverage)
    print(json.dumps(value, sort_keys=True))


def _code(args: argparse.Namespace, root: Path, model_path: Path | None) -> dict[str, Any]:
    from .investigation import CONTRACT, MAX_SOURCE, catalog, run, save, scope_specs
    from .store import digest

    if args.evidence_cmd == "code-catalog":
        return catalog(with_model=model_path is not None)
    if not (args.audit or args.bundle):
        raise AutError("investigation: select --audit ID or --bundle PATH")
    specs = scope_specs(root, args.content, args.audit, args.bundle,
                        args.include_audit, args.include_bundle, model_path)
    if args.evidence_cmd == "replay-code":
        record = Store(root, read_only=True).get("investigation_runs", args.run_id)
        if record["contract"] != CONTRACT:
            raise AutError("investigation: saved recipe uses an unsupported operation contract")
        value = run(record["source"], record["params"], specs, record["scopes"])
        return {**value, "replayed": args.run_id, "matches": digest(value) == record["result_sha256"]}
    if bool(args.source) == bool(args.recipe):
        raise AutError("investigation: select a source path, stdin, or --recipe ID")
    try:
        params = json.loads(args.params) if args.params is not None else {}
        if args.recipe:
            recipe = Store(root, read_only=True).get("investigation_runs", args.recipe)
            if recipe["contract"] != CONTRACT:
                raise AutError("investigation: saved recipe uses an unsupported operation contract")
            source = recipe["source"]
            if args.params is None:
                params = recipe["params"]
        elif args.source == "-":
            source = sys.stdin.buffer.read(MAX_SOURCE + 1).decode("utf-8")
        else:
            with Path(args.source).open("rb") as stream:
                source = stream.read(MAX_SOURCE + 1).decode("utf-8")
    except (ValueError, OSError, RecursionError) as exc:
        raise AutError("investigation: provide UTF-8 source and valid JSON parameters") from exc
    value = run(source, params, specs)
    if args.save:
        value["run_id"] = save(Store(root), source, params, value)
    return value
