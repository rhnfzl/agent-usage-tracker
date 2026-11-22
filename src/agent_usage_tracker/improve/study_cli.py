"""Expose study records through the existing private improvement command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..config import AutError
from ..event import parse_ts
from . import studies
from .access import MODES, Evidence, _page
from .store import Store, digest
from .studies import assess, create, shortlist
from .study_followup import followup, transfer
from .study_observe import COVERAGE_BASES, METRICS


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("study", help="freeze and assess a prospective proof policy")
    actions = parser.add_subparsers(dest="study_cmd", required=True)
    action = actions.add_parser("shortlist", help="deduplicate frozen incident evidence")
    action.add_argument("--audit", required=True)
    action = actions.add_parser("create", help="freeze an operator-authored prospective policy")
    action.add_argument("--spec-file", type=Path, required=True)
    action.add_argument("--trial-spec-file", type=Path,
                        help="freeze a trusted trial file with a policy that omits trial_spec_id")
    action = actions.add_parser("assess", help="report each stored proof stage")
    action.add_argument("--study", required=True)
    action = actions.add_parser("preflight", help="find unused baseline candidates for a prospective study")
    source = action.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-study", help="reuse a study's proposal, discovery and control evidence")
    source.add_argument("--scope-file", type=Path, help="JSON with proposal_id, discovery_cases and control_cases")
    action.add_argument("--audit", required=True)
    action.add_argument("--content", choices=MODES, default="metadata")
    action.add_argument("--metric", choices=METRICS, default="tool_calls")
    action.add_argument("--coverage-basis", choices=COVERAGE_BASES)
    action.add_argument("--require-turn-context", action="store_true")
    action.add_argument("--require-setup-revision", action="store_true")
    action.add_argument("--require-worktree-setup", action="store_true",
                        help="require recorded contents or absence of the setup file in the worktree")
    action.add_argument("--include-excluded", action="store_true")
    for name in ("before", "agent", "cursor"):
        action.add_argument(f"--{name}", default="")
    action.add_argument("--limit", type=int, default=30)
    action = actions.add_parser("run", help="verify an isolated proposal using its frozen study")
    action.add_argument("--study", required=True)
    action.add_argument("--execute", action="store_true")
    for name in ("followup", "transfer", "observe"):
        action = actions.add_parser(name)
        action.add_argument("--study", required=True)
        action.add_argument("--spec-file", type=Path, required=True)
        if name == "followup":
            action.add_argument("--execute", action="store_true")
        elif name == "observe":
            action.add_argument("--preview", action="store_true", help="check pairs without recording an observation")


def _preflight(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    from .study_candidates import ordinary_work_page
    from .study_independence import reserved_evidence
    from .study_observe import baseline_gate

    _page(0, args.limit)
    fields = ("proposal_id", "discovery_cases", "control_cases")
    if args.from_study:
        study = store.get("studies", args.from_study)
        draft = {name: study[name] for name in fields}
    else:
        try:
            draft = json.loads(args.scope_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AutError("improve: preflight scope is not readable JSON") from exc
    studies.fields(draft, " ".join(fields))
    studies.strings(draft["discovery_cases"])
    studies.strings(draft["control_cases"])
    proposal = store.get("proposals", draft["proposal_id"])
    if proposal["case_id"] not in draft["discovery_cases"]:
        raise AutError("improve: proposal must link discovery evidence")
    reserved = reserved_evidence(store, proposal, draft["discovery_cases"], draft["control_cases"])
    policy = {"metric": args.metric, "require_turn_context": args.require_turn_context
              or args.require_setup_revision or args.require_worktree_setup,
              "require_setup_revision": args.require_setup_revision}
    if args.require_worktree_setup:
        policy["require_worktree_setup"] = True
    if args.coverage_basis:
        policy["coverage_basis"] = args.coverage_basis
    before = parse_ts(args.before) if args.before else None
    evidence = Evidence(args.content, store=store, audit_id=args.audit)
    report = ordinary_work_page(store, evidence.describe(), evidence.keys, evidence.ambiguous,
        policy, args.agent, args.before, args.include_excluded, args.cursor, args.limit, evidence._label,
        gate=baseline_gate(policy, proposal, reserved, before), gate_key=digest(draft))
    return {"proposal_id": draft["proposal_id"], "prospective_baseline_preview": True, **report}


def run(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    if args.study_cmd == "shortlist":
        return shortlist(store, args.audit)
    if args.study_cmd == "assess":
        return assess(store, args.study)
    if args.study_cmd == "preflight":
        return _preflight(args, store)
    if args.study_cmd == "run":
        return studies.run(store, args.study, execute=args.execute)
    try:
        spec = json.loads(args.spec_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AutError("improve: study specification is not readable JSON") from exc
    if args.study_cmd == "create":
        if args.trial_spec_file is not None:
            if not isinstance(spec, dict) or "trial_spec_id" in spec:
                raise AutError("improve: use either a trial spec file or a saved trial_spec_id")
            try:
                trial = json.loads(args.trial_spec_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise AutError("improve: trial specification is not readable JSON") from exc
            spec["trial_spec_id"] = store.put("trial_specs", trial)
        return create(store, spec)
    if args.study_cmd == "followup":
        return followup(store, args.study, spec, execute=args.execute)
    if args.study_cmd == "observe":
        from .study_observe import observe
        return observe(store, args.study, spec, preview=args.preview)
    return transfer(store, args.study, spec)
