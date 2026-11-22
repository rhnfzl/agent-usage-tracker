"""Private improvement commands, independent from the configured sink."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from ..config import AutError
from ..label.core import data_dir
from .proposals import prepare, stage
from .schedule import freeze_cycle_audit, run_once
from .store import Store, audit
from .trial import run_trial
from .validation import validate
from . import study_cli


def register(sub: Any) -> None:
    parser = sub.add_parser("improve", help="private local diagnosis and controlled setup trials")
    parser.add_argument("--root", type=Path, help="private local store (never sent to the sink)")
    actions = parser.add_subparsers(dest="improve_cmd", required=True)
    for name in ("audit", "run"):
        action = actions.add_parser(name, help="freeze local evidence" if name == "audit"
                                    else "run one locked, bounded local cycle")
        action.add_argument("--days", type=int, default=7)
        action.add_argument("--limit", type=int, default=30)
        if name == "audit":
            action.add_argument("--project", type=Path)
            action.add_argument("--cycle", action="store_true",
                                help="freeze the next scheduled cycle's audit unless one is pending")
            action.add_argument("--budget-s", type=int, default=1500,
                                help="deadline for the --cycle audit step")
        else:
            action.add_argument("--case-limit", type=int, default=12)
            action.add_argument("--audit-budget-s", type=int, default=0,
                                help="extra seconds to build an audit when none is frozen")
    decision = actions.add_parser("diagnose", help="review frozen evidence with cached local models")
    decision.add_argument("--audit", required=True)
    decision.add_argument("--limit", type=int, default=12)
    for action in (decision, actions.choices["run"]):
        action.add_argument("--models", default="pplx-decider-flash,clef-flash")
        action.add_argument("--budget-s", type=int, default=300)
        action.add_argument("--max-calls", type=int, default=40)
        action.add_argument("--explain-model", help="cached local instruct model for evidence review")
    inspection = actions.add_parser("inspect", help="write a private view of captured case evidence")
    inspection.add_argument("--case", required=True)
    proposal = actions.add_parser("propose", help="draft one explicit scoped text replacement")
    proposal.add_argument("--case", required=True)
    proposal.add_argument("--scope", type=Path, required=True)
    proposal.add_argument("--target", type=Path, required=True)
    proposal.add_argument("--before-file", type=Path, required=True)
    proposal.add_argument("--after-file", type=Path, required=True)
    proposal.add_argument("--candidate", type=Path, help="stage only in an independent directory")
    drafting = actions.add_parser("draft", help="draft changes for human review in private candidates")
    drafting.add_argument("--max-groups", type=int, default=1)
    drafting.add_argument("--budget-usd", type=float, default=1.,
                          help="known dollar spend cap; unknown OAuth cost stays unknown")
    drafting.add_argument("--budget-s", type=float, default=360.)
    drafting.add_argument("--mode", choices=("auto", "failover", "round_robin"), default="auto")
    drafting.add_argument("--order", default="codex,claude,cursor,pi",
                          help="comma-separated fixed or round-robin order; local models stay last")
    drafting.add_argument("--excerpts", choices=("none", "all"), default=None,
                          help="send redacted conversation events too (default none)")
    drafting.add_argument("--target", type=Path, help="explicitly offer exactly this file, including code")
    drafting.add_argument("--reconsider", help="full rejected group key to reconsider")
    drafting.add_argument("--explain-model", help="cached MLX model for local fallback")
    trial = actions.add_parser("trial", help="preview or execute an operator-supplied trial")
    source = trial.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec")
    source.add_argument("--spec-file", type=Path)
    trial.add_argument("--execute", action="store_true", help="execute the trusted argv cases")
    trial.add_argument("--budget-s", type=int, default=600)
    reference = actions.add_parser("validate", help="check judgments against executable references")
    reference.add_argument("--spec-file", type=Path, required=True)
    reference.add_argument("--diagnosis", required=True)
    reference.add_argument("--execute", action="store_true", help="execute the trusted reference oracle")
    labelling = actions.add_parser("label", help="check advisory judges with private human labels")
    labelling.add_argument("--rubric", required=True)
    labelling.add_argument("--limit", type=int, default=30)
    card = actions.add_parser("report-card", help="write a private improvement report")
    card.add_argument("--days", type=int, default=28)
    card.add_argument("--format", choices=("md", "html", "json"), default="md")
    desktop = actions.add_parser("desktop", help="import private native-app command evidence")
    desktop.add_argument("--trace", type=Path, required=True, help="raw trace file or segment directory")
    desktop.add_argument("--responses", type=Path, help="local response companion JSONL")
    desktop.add_argument("--mapping", type=Path, required=True, help="explicit task/session/parent JSON")
    grouping = actions.add_parser("groups", help="group failed judgments by deterministic signals")
    grouping.add_argument("--days", type=int, default=14)
    grouping.add_argument("--threshold", type=int, default=3,
                          help="distinct tasks that make a group recurring")
    inbox = actions.add_parser("inbox", help="list, accept or reject proposals")
    choices = inbox.add_subparsers(dest="inbox_cmd", required=True)
    choices.add_parser("list", help="open proposals and failure groups: ids and counts, no text")
    accept = choices.add_parser("accept", help="accept a proposal; commits only in its candidate")
    accept.add_argument("id", help="proposal id or a unique prefix of 6 or more hex characters")
    accept.add_argument("--note")
    reject = choices.add_parser("reject", help="reject a proposal and block its group")
    reject.add_argument("id", help="proposal id or a unique prefix of 6 or more hex characters")
    reject.add_argument("--reason", required=True, help="why, 1 to 500 characters")
    adopt = actions.add_parser("adopt", help="record a merged change (the title and date are "
                               "published later; targets and commit stay private)")
    adopt.add_argument("proposal", nargs="?", help="an accepted proposal id")
    adopt.add_argument("--title", help="human-written, at most 120 characters")
    adopt.add_argument("--commit", required=True)
    adopt.add_argument("--pr", type=int)
    adopt.add_argument("--date", help="YYYY-MM-DD, default today")
    adopt.add_argument("--target", action="append", default=[])
    adopt.add_argument("--rubric", action="append", default=[])
    adopt.add_argument("--kind", choices=("instruction", "skill", "config", "code"))
    adopt.add_argument("--note")
    study_cli.register(actions)
    actions.add_parser("status", help="show local progress counts without trace text")


def _decide(store: Store, args: argparse.Namespace) -> dict[str, Any]:
    from . import decide, inbox
    if args.improve_cmd == "inbox" and args.inbox_cmd == "list":
        return inbox.summary(store)
    if args.improve_cmd == "adopt":
        if not args.proposal and not args.title:
            raise AutError("improve: adopt needs a proposal id or --title")
        result = decide.adopt(store, decide.resolve(store, args.proposal) if args.proposal
                              else None, commit=args.commit, title=args.title, pr=args.pr,
                              adopted_at=args.date, targets=args.target, rubric_ids=args.rubric,
                              kind=args.kind, note=args.note)
    elif args.inbox_cmd == "accept":
        result = decide.accept(store, decide.resolve(store, args.id), args.note)
    else:
        result = decide.reject(store, decide.resolve(store, args.id), args.reason)
    try:    # the decision is saved; a page that cannot be rebuilt now is rebuilt next cycle
        inbox.rewrite(store)
    except Exception as exc:
        result["inbox_error"] = type(exc).__name__
    return result


def run(args: argparse.Namespace) -> None:
    root = args.root or Path(os.environ.get("AUT_IMPROVE_ROOT", str(data_dir() / "improve")))
    if args.improve_cmd == "report-card":
        from .report_card import write
        root = root.expanduser()
        store = Store(root, read_only=True) if (root / ".aut-improve").is_file() else Store(root)
        store.read_only = False  # Refresh qualifications without rewriting existing support files.
        print(write(store, days=args.days, format=args.format), end="\n")
        return
    preview = args.improve_cmd == "study" and (
        args.study_cmd == "preflight" or args.study_cmd == "observe" and getattr(args, "preview", False))
    store = Store(root, read_only=preview)
    if args.improve_cmd == "study":
        result = study_cli.run(args, store)
    elif args.improve_cmd == "desktop":
        from .desktop import import_desktop
        try:
            mapping = json.loads(args.mapping.read_text())
        except (OSError, ValueError) as exc:
            raise AutError("improve: desktop mapping is not readable JSON") from exc
        if not isinstance(mapping, dict):
            raise AutError("improve: desktop mapping must be a JSON object")
        result = import_desktop(store, args.trace, args.responses, mapping)
    elif args.improve_cmd == "audit" and args.cycle:
        if args.project:
            raise AutError("improve: --cycle freezes the scheduled audit and takes no --project")
        result = freeze_cycle_audit(store, days=args.days, limit=args.limit, budget_s=args.budget_s)
    elif args.improve_cmd == "audit":
        result = audit(store, Path.home(), days=args.days, limit=args.limit, project=args.project)
    elif args.improve_cmd == "diagnose":
        report = run_once(store, audit_id=args.audit, models=tuple(args.models.split(",")),
                          case_limit=args.limit, budget_s=args.budget_s, max_calls=args.max_calls,
                          explain_model=args.explain_model)
        result = report.get("diagnosis", report)
    elif args.improve_cmd == "groups":
        from .groups import refresh
        result = refresh(store, window_days=args.days, threshold=args.threshold)
    elif args.improve_cmd == "label":
        from .qualify import review
        result = review(store, args.rubric, limit=args.limit)
    elif args.improve_cmd in {"inbox", "adopt"}:
        result = _decide(store, args)
    elif args.improve_cmd == "inspect":
        from .case_view import write_case_view
        result = write_case_view(store, args.case)
    elif args.improve_cmd == "propose":
        key = prepare(store, args.case, args.scope, args.target,
                      args.before_file.read_bytes().decode("utf-8"),
                      args.after_file.read_bytes().decode("utf-8"))
        if args.candidate:
            stage(store, key, args.candidate)
        result = {"proposal_id": key, "staged": bool(args.candidate)}
    elif args.improve_cmd == "draft":
        from . import draft
        options = draft.settings()
        if args.excerpts is not None:
            options["excerpts"] = args.excerpts
        result = draft.run(store, max_groups=args.max_groups, reconsider=args.reconsider,
                           target=args.target, mode=args.mode, order=args.order,
                           budget_usd=args.budget_usd, budget_s=args.budget_s,
                           explain_model=args.explain_model, **options)
    elif args.improve_cmd == "trial":
        key = args.spec
        if args.spec_file:
            try:
                value = json.loads(args.spec_file.read_text())
            except (OSError, ValueError) as exc:
                raise AutError("improve: trial specification is not readable JSON") from exc
            key = store.put("trial_specs", value)
        result = run_trial(store, key, execute=args.execute, budget_s=args.budget_s)
    elif args.improve_cmd == "run":
        result = run_once(store, days=args.days, limit=args.limit, case_limit=args.case_limit,
                          budget_s=args.budget_s, max_calls=args.max_calls,
                          models=tuple(args.models.split(",")), explain_model=args.explain_model,
                          audit_budget_s=args.audit_budget_s)
    elif args.improve_cmd == "validate":
        try:
            value = json.loads(args.spec_file.read_text())
        except (OSError, ValueError) as exc:
            raise AutError("improve: reference specification is not readable JSON") from exc
        key = store.put("reference_specs", value)
        result = validate(store, key, args.diagnosis, execute=args.execute)
    else:
        kinds = ("audits", "sessions", "cases", "judgments", "proposals", "trial_results")
        result = {"records": {kind: len(store.keys(kind)) for kind in kinds},
                  "private": True, "human_verified": False}
    print(json.dumps(result, sort_keys=True))
    if result.get("status") == "stale":
        raise AutError(result["message"])
    if args.improve_cmd in {"run", "diagnose", "audit"} \
            and result.get("status") in {"timeout", "error", "budget"}:
        raise AutError(f"improve: {args.improve_cmd} {result['status']}")
