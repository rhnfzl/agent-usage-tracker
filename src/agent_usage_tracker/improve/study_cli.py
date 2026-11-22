"""Expose study records through the existing private improvement command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..config import AutError
from . import studies
from .store import Store
from .studies import assess, create, shortlist
from .study_followup import followup, transfer


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
    action = actions.add_parser("run", help="verify an isolated proposal using its frozen study")
    action.add_argument("--study", required=True)
    action.add_argument("--execute", action="store_true")
    for name in ("followup", "transfer", "observe"):
        action = actions.add_parser(name)
        action.add_argument("--study", required=True)
        action.add_argument("--spec-file", type=Path, required=True)
        if name == "followup":
            action.add_argument("--execute", action="store_true")


def run(args: argparse.Namespace, store: Store) -> dict[str, Any]:
    if args.study_cmd == "shortlist":
        return shortlist(store, args.audit)
    if args.study_cmd == "assess":
        return assess(store, args.study)
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
        return observe(store, args.study, spec)
    return transfer(store, args.study, spec)
