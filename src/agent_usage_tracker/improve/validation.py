"""Compare frozen judge decisions with explicit, independently executable references."""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from ..config import AutError
from .judge import LABELS, RUBRICS
from .proposals import file_digest
from .store import Store, digest
from .trial import command


def evidence_key(case: dict[str, Any]) -> str:
    return digest({name: case[name] for name in (
        "events", "coverage", "omitted_task_events")})


def _oracle_inputs(spec: dict[str, Any]) -> None:
    for index, arg in enumerate(spec["oracle_argv"]):
        path = (Path(spec["oracle_cwd"]) / arg).resolve()
        if path.is_file() and not (
            index == 0 and path.resolve() == Path(sys.executable).resolve()
        ) and path not in {Path(p).resolve() for p in spec["oracle_files"]}:
            raise AutError("improve: reference oracle command input is not frozen")
    for name, expected in spec["oracle_files"].items():
        path = Path(name)
        if not path.is_absolute() or not path.is_file() or file_digest(path) != expected:
            raise AutError("improve: reference oracle input changed")


def validate(store: Store, spec_id: str, diagnosis_id: str, *,
             execute: bool = False) -> dict[str, Any]:
    spec = store.get("reference_specs", spec_id)
    policy = store.get("diagnoses", diagnosis_id)
    try:
        references = spec["references"]
        argv = spec["oracle_argv"]
        timeout = spec["timeout_s"]
        if spec["version"] != 1 or spec["split"] not in {"development", "holdout"} \
                or not isinstance(references, list) or not 1 <= len(references) <= 200 \
                or not isinstance(argv, list) or not argv \
                or any(not isinstance(a, str) or "\0" in a for a in argv) \
                or type(timeout) not in (int, float) or not math.isfinite(timeout) \
                or not 0 < timeout <= 300 or not spec["oracle_files"] \
                or "{reference_input}" not in argv:
            raise ValueError
        identities = set()
        for ref in references:
            if ref["rubric"] not in RUBRICS or ref["expected"] not in LABELS \
                    or not re.fullmatch(r"[a-f0-9]{64}", ref["evidence_sha256"]):
                raise ValueError
            identity = ref["evidence_sha256"], ref["rubric"]
            if identity in identities:
                raise ValueError
            identities.add(identity)
        cwd = Path(spec["oracle_cwd"])
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError
        _oracle_inputs(spec)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AutError("improve: invalid executable reference specification") from exc
    cases: dict[str, str] = {}
    for case_id in policy["cases"]:
        key = evidence_key(store.get("cases", case_id))
        if key in cases:
            raise AutError("improve: ambiguous reference evidence in diagnosis")
        cases[key] = case_id
    if any(ref["evidence_sha256"] not in cases for ref in references):
        raise AutError("improve: reference evidence is absent from this diagnosis")
    if not execute:
        return {"spec_id": spec_id, "diagnosis_id": diagnosis_id,
                "execution_required": True, "references": len(references), "split": spec["split"]}
    payload = {"references": [{"evidence_sha256": ref["evidence_sha256"],
        "rubric": ref["rubric"], "case": store.get("cases", cases[ref["evidence_sha256"]])}
        for ref in references]}
    input_id = store.put("reference_inputs", payload)
    input_path = store.path("reference_inputs", input_id)
    argv = [str(input_path) if arg == "{reference_input}" else arg for arg in argv]
    oracle = command(argv, cwd, timeout, source_pythonpath=False)
    oracle_id = store.put("reference_oracles", {"spec_id": spec_id, **oracle})
    _oracle_inputs(spec)
    store.get("reference_inputs", input_id)
    try:
        if not oracle["accepted"] or oracle["output_truncated"]:
            raise ValueError
        observed = json.loads(oracle["output"])
        # Short digest parts survive text redaction without exempting arbitrary long tokens.
        bound = [{"evidence_sha256": "".join(row["evidence_sha256_parts"]),
                  "rubric": row["rubric"], "expected": row["expected"]}
                 for row in observed["references"]]
        if bound != references:
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise AutError(f"improve: reference oracle failed or disagreed; record {oracle_id}") from exc
    evaluator = digest({k: v for k, v in policy.items() if k not in {"audit_id", "cases"}})
    judgments = {value["evaluation_key"]: (key, value) for key in store.keys("judgments")
                 if (value := store.get("judgments", key))}
    counts: Counter[str] = Counter()
    rows = []
    for ref in references:
        key = digest({"evaluator": evaluator, "case": cases[ref["evidence_sha256"]],
                      "rubric": ref["rubric"]})
        if key not in judgments:
            counts["missing"] += 1
            rows.append({**ref, "observed": None, "judgment_id": None})
            continue
        judgment_id, judgment = judgments[key]
        verdict = judgment["verdict"]
        if verdict not in LABELS:
            raise AutError("improve: reference judgment has an invalid verdict")
        expected = ref["expected"]
        counts[f"reference_{expected}"] += 1
        counts["abstentions"] += verdict == "unknown"
        counts["detected_failures"] += expected == "fail" and verdict == "fail"
        counts["missed_failures"] += expected == "fail" and verdict != "fail"
        counts["false_alarms"] += expected == "pass" and verdict == "fail"
        counts["supported_passes"] += expected == "pass" and verdict == "pass"
        counts["decisions_on_unknown"] += expected == "unknown" and verdict != "unknown"
        rows.append({**ref, "observed": verdict, "judgment_id": judgment_id})

    def rate(numerator: str, denominator: str) -> float | None:
        return counts[numerator] / counts[denominator] if counts[denominator] \
            and not counts["missing"] else None

    report = {"spec_id": spec_id, "diagnosis_id": diagnosis_id, "oracle_id": oracle_id,
        "input_id": input_id,
        "complete": not counts["missing"], "split": spec["split"], "counts": dict(counts),
        "failure_recall": rate("detected_failures", "reference_fail"),
        "false_alarm_rate": rate("false_alarms", "reference_pass"),
        "pass_recall": rate("supported_passes", "reference_pass"), "cases": rows,
        "scope": "executable references only; not real-work accuracy",
        "human_verified": False, "automation_qualified": False,
        "holdout_independence_verified": False}
    key = store.put("reference_results", report)
    store.append("reference_validation", report_id=key, diagnosis_id=diagnosis_id,
                 spec_id=spec_id, complete=report["complete"])
    return {"report_id": key, **report}
