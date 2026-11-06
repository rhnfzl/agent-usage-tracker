"""Import provisional historical labels separately from calibrated nightly labels."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import AutError, Config
from ..event import ActivityEvent, make_event_id
from ..push import send_activity_rows

POLICY = "working-sonnet-v1"
_MODEL = "claude-sonnet-5-5-medium"
_LABELS = {
    "steer_intent": {
        "framing",
        "instruction",
        "clarification",
        "correction",
        "redirection",
        "review",
        "acceptance",
        "stop",
        "other",
    },
    "soft_outcome": {"resolved", "still_open", "blocked", "ruled_out"},
}
_SIGNALS = {
    "steer_intent": {"explicit_halt", "disputes_agent_work"},
    "soft_outcome": {"required_next_step", "external_blocker"},
}


def _check(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid provisional baseline")


def _timestamp(value: Any) -> str:
    _check(isinstance(value, str))
    _check(datetime.fromisoformat(value).utcoffset() is not None)
    return str(value)


def _events(bundle: Any, machine: str, labelled_at: str) -> list[ActivityEvent]:
    _check(bundle["version"] == "working-sonnet-baseline-v1")
    _check(isinstance(bundle["rows"], list) and bool(bundle["rows"]))
    baseline_id = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    stamped = _timestamp(labelled_at)
    result, seen = [], set()
    for row in bundle["rows"]:
        task, unit_id = row["task"], row["unit_id"]
        _check(
            task in _LABELS
            and isinstance(unit_id, str)
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", unit_id) is not None
        )
        _check(row["unit"] == ("steer" if task == "steer_intent" else "episode"))
        _check((task, unit_id) not in seen)
        seen.add((task, unit_id))
        _check(
            row["quality"] == "provisional"
            and row["estimate"] is True
            and row["human_verified"] is False
        )
        _check(
            row["label"] in _LABELS[task] | {"unknown"}
            and row["baseline_label"] in _LABELS[task] | {"unknown"}
        )
        _check(row["label_source"] in {"sonnet", "unknown_review", "unknown_input"})
        _check(row["label_source"] == "sonnet" or row["label"] == "unknown")
        _check(
            row["model_id"] == _MODEL
            or (
                row["model_id"] is None
                and row["label"] == "unknown"
                and row["baseline_label"] == "unknown"
            )
        )
        weight = row["weight"]
        _check(type(weight) in (int, float) and math.isfinite(weight) and weight > 0)
        _check(
            isinstance(row["input_hash"], str)
            and re.fullmatch(r"[a-f0-9]{64}", row["input_hash"]) is not None
        )
        signals = {}
        _check(isinstance(row["reviewed_signals"], dict))
        for question, answer in row["reviewed_signals"].items():
            _check(question in _SIGNALS[task] and answer["answer"] in {"yes", "no", "unknown"})
            _check(answer["source"] == "agent_review" and answer["human_verified"] is False)
            _check(
                isinstance(answer["question_sha256"], str)
                and re.fullmatch(r"[a-f0-9]{64}", answer["question_sha256"]) is not None
            )
            signals[question] = {
                "answer": answer["answer"],
                "source": "agent_review",
                "question_sha256": answer["question_sha256"],
            }
        _check(
            not any(value["answer"] == "unknown" for value in signals.values())
            or row["label"] == "unknown"
        )
        # Only ids, enums and numbers cross the sink boundary, never review reasons or paths.
        attrs = {
            "task": "working." + task,
            "task_ver": 1,
            "unit": row["unit"],
            "unit_id": unit_id,
            "label": row["label"],
            "baseline_label": row["baseline_label"],
            "p": None,
            "below_tau": row["label"] == "unknown",
            "model_id": row["model_id"],
            "label_policy": POLICY,
            "baseline_id": baseline_id,
            "labelled_at": stamped,
            "label_source": row["label_source"],
            "population_weight": weight,
            "input_hash": row["input_hash"],
            "provisional": True,
            "human_verified": False,
            "signals": signals,
        }
        result.append(
            ActivityEvent(
                event_id=make_event_id("baseline", POLICY, baseline_id, task, unit_id),
                ts=_timestamp(row["ts"]),
                agent="label",
                kind="label",
                session_id=None,
                turn_id=None,
                attrs=attrs,
                machine=machine,
            )
        )
    return result


def events(bundle: Any, machine: str, labelled_at: str) -> list[ActivityEvent]:
    """Validate the entire bundle before writing; repeated imports have stable event ids."""
    try:
        return _events(bundle, machine, labelled_at)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise AutError("label import-baseline: invalid provisional baseline bundle") from exc


def run(cfg: Config, path: Path, *, apply: bool = False) -> dict[str, Any]:
    try:
        bundle = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise AutError("label import-baseline: could not read the baseline JSON") from exc
    rows = events(bundle, cfg.machine, datetime.now(timezone.utc).isoformat())
    if apply:
        if not cfg.pg_dsn:
            raise AutError("label import-baseline: transport.pg_dsn is required for --apply")
        send_activity_rows(cfg.pg_dsn, rows)
    return {
        "mode": "applied" if apply else "preview",
        "policy": POLICY,
        "rows": len(rows),
        "unknown": sum(e.attrs["label"] == "unknown" for e in rows),
        "reviewed_signals": sum(len(e.attrs["signals"]) for e in rows),
        "tasks": dict(Counter(e.attrs["task"] for e in rows)),
    }
