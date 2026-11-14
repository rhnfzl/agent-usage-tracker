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
from .schedule import run_once
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
        else:
            action.add_argument("--case-limit", type=int, default=12)
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
    desktop = actions.add_parser("desktop", help="import private native-app command evidence")
    desktop.add_argument("--trace", type=Path, required=True, help="raw trace file or segment directory")
    desktop.add_argument("--responses", type=Path, help="local response companion JSONL")
    desktop.add_argument("--mapping", type=Path, required=True, help="explicit task/session/parent JSON")
    study_cli.register(actions)
    actions.add_parser("status", help="show local progress counts without trace text")


def run(args: argparse.Namespace) -> None:
    root = args.root or Path(os.environ.get("AUT_IMPROVE_ROOT", str(data_dir() / "improve")))
    store = Store(root)
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
    elif args.improve_cmd == "audit":
        result = audit(store, Path.home(), days=args.days, limit=args.limit, project=args.project)
    elif args.improve_cmd == "diagnose":
        report = run_once(store, audit_id=args.audit, models=tuple(args.models.split(",")),
                          case_limit=args.limit, budget_s=args.budget_s, max_calls=args.max_calls,
                          explain_model=args.explain_model)
        result = report.get("diagnosis", report)
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
                          models=tuple(args.models.split(",")), explain_model=args.explain_model)
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
    if args.improve_cmd in {"run", "diagnose"} and result.get("status") in {"timeout", "error"}:
        raise AutError(f"improve: cycle {result['status']}")
