"""Human labels check advisory judges against frozen local evidence. Nothing leaves the store."""

from __future__ import annotations

import math
import re
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import AutError
from ..label import gold
from ..state import _locked
from . import judge, rubric as rubrics
from .case_view import write_case_view
from .groups import _flagged, _journal
from .rubric import LABELS, Rubric
from .store import Store
from .validation import evidence_key, score


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _policy(rubric: Rubric) -> dict[str, Any]:
    value = rubric.qualify
    if set(value) != {"min_fail_labels", "min_failure_recall", "max_false_alarm_rate"} \
            or type(value["min_fail_labels"]) is not int or value["min_fail_labels"] < 1 \
            or any(type(value[name]) not in (int, float) or not math.isfinite(value[name])
                   or not 0 <= value[name] <= 1
                   for name in ("min_failure_recall", "max_false_alarm_rate")):
        raise AutError("improve: invalid rubric qualification policy")
    return dict(value)


def _rubric(rubric_id: str) -> Rubric:
    found = next((r for r in judge._rubrics() if r.id == rubric_id), None)
    if found is None:
        raise AutError("improve: choose an enabled rubric")
    _policy(found)
    return found


def labels(store: Store, rubric: Rubric) -> dict[str, dict[str, Any]]:
    """Latest label per evidence for this version; a null label is an undo tombstone."""
    newest: dict[str, tuple[datetime, str, dict[str, Any]]] = {}
    for key in store.keys("human_labels"):
        row = store.get("human_labels", key)
        try:
            if set(row) != {"evidence_sha256", "case_id", "rubric_id", "rubric_version",
                            "label", "labeller", "labelled_at", "note"} \
                    or not _id(row["case_id"]) or not _id(row["evidence_sha256"]) \
                    or not isinstance(row["rubric_id"], str) \
                    or not re.fullmatch(r"[a-z_]{1,40}", row["rubric_id"]) \
                    or type(row["rubric_version"]) is not int or row["rubric_version"] < 1 \
                    or row["label"] is not None and row["label"] not in LABELS \
                    or row["labeller"] != "human" or row["note"] is not None \
                    and (not isinstance(row["note"], str) or len(row["note"]) > 500):
                raise ValueError
            stamp = datetime.fromisoformat(row["labelled_at"])
            if stamp.tzinfo is None or evidence_key(store.get("cases", row["case_id"])) \
                    != row["evidence_sha256"]:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise AutError("improve: invalid human label record") from exc
        if (row["rubric_id"], row["rubric_version"]) != (rubric.id, rubric.version):
            continue
        slot = row["evidence_sha256"]
        if slot not in newest or (stamp, key) > newest[slot][:2]:
            newest[slot] = stamp, key, row
    return {key: row for key, (_, _, row) in newest.items() if row["label"] is not None}


def judgments(store: Store, rubric: Rubric) -> list[dict[str, Any]]:
    """Version-bound judgments, oldest first by diagnosis journal order (hash breaks ties)."""
    order = {key: index for index, row in enumerate(_journal(store))
             if row.get("kind") == "diagnosis" for key in row.get("judgment_ids", [])}
    found = []
    for key in store.keys("judgments"):
        row = store.get("judgments", key)
        if row.get("rubric") != rubric.id:
            continue
        try:
            if not _id(row["case_id"]) or row["verdict"] not in LABELS:
                raise ValueError
            policy = store.get("diagnoses", row["job_id"])
            if row["case_id"] not in policy["cases"] or row["evaluation_key"] != \
                    judge.evaluation_key(judge.evaluator(policy, rubric.id), row["case_id"],
                                         rubric.id):
                raise ValueError
            if "rubric_snapshot" in row:
                snapshot = store.get("rubric_snapshots", row["rubric_snapshot"])
                if snapshot["id"] != rubric.id or type(snapshot["version"]) is not int \
                        or snapshot["version"] < 1 or not _id(snapshot["sha256"]) \
                        or type(row["rubric_version"]) is not int \
                        or row["rubric_version"] != snapshot["version"] \
                        or row["rubric_sha256"] != snapshot["sha256"] \
                        or policy["rubrics"][rubric.id] != row["rubric_snapshot"]:
                    raise ValueError
                version = snapshot["version"]
            else:
                version = rubrics.legacy_version(policy, rubric.id)
        except (KeyError, TypeError, ValueError) as exc:
            raise AutError("improve: invalid versioned judgment") from exc
        if version == rubric.version:
            found.append({**row, "judgment_id": key})
    return sorted(found, key=lambda row: (order.get(row["judgment_id"], -1), row["judgment_id"]))


def _cases(store: Store, rubric: Rubric) -> dict[str, dict[str, Any]]:
    found = {}
    for key in store.keys("cases"):
        case = store.get("cases", key)
        if rubric.input == "attempt" and judge._eligible(case) \
                and rubrics.applies(rubric, case, None):
            store.get("sessions", case["session_snapshot"])
            found[key] = {**case, "case_id": key}
    return found


def queue(store: Store, rubric: Rubric) -> list[dict[str, Any]]:
    """Fail, split vote, error, then other cases; insert a control after every four."""
    _policy(rubric)
    labelled = labels(store, rubric)
    ranked: dict[str, int] = {}
    for row in judgments(store, rubric):
        rank = 0 if row["verdict"] == "fail" else 1 if _flagged(row) else 3
        ranked[row["case_id"]] = min(ranked.get(row["case_id"], 3), rank)
    cases = sorted(_cases(store, rubric).values(), key=lambda case: (
        min(ranked.get(case["case_id"], 3), 2 if case["trigger"] == "error_result" else 3),
        case["case_id"]))
    unique = []
    seen = set(labelled)
    for case in cases:
        key = evidence_key(case)
        if key not in seen:
            unique.append(case)
            seen.add(key)
    controls = deque(case for case in unique if case["trigger"] == "control"
                     and ranked.get(case["case_id"], 3) == 3)
    primary = deque(case for case in unique if case not in controls)
    out = []
    while primary or controls:
        for _ in range(4):
            if primary:
                out.append(primary.popleft())
        if controls:
            out.append(controls.popleft())
    return out


def _sync_queue(store: Store) -> None:
    with _locked(store.root / "human_labels"):
        _sync_queue_locked(store)


def _sync_queue_locked(store: Store, answer: dict[str, Any] | None = None) -> None:
    """Refresh labelled cases awaiting this version; preserve other producers' queue entries."""
    pending: set[str] = set()
    owned: set[str] = set()
    for rubric in judge._rubrics():
        rows = labels(store, rubric)
        if answer and (answer["rubric_id"], answer["rubric_version"]) == (rubric.id, rubric.version):
            rows.pop(answer["evidence_sha256"], None)
            if answer["label"] is not None:
                rows[answer["evidence_sha256"]] = answer
            owned.add(answer["case_id"])
        graded = {row["case_id"] for row in judgments(store, rubric)}
        owned.update(row["case_id"] for row in rows.values())
        pending.update(row["case_id"] for row in rows.values() if row["case_id"] not in graded)
    # Tombstones can retire an entry only when no other current label still needs that case.
    owned.update(store.get("human_labels", key)["case_id"] for key in store.keys("human_labels"))
    judge.update_queue(store, add=pending, remove=owned - pending)


def save(store: Store, rubric: Rubric, case_id: str, label: str | None) -> str:
    """Append one human answer, or a tombstone when undo restores an unlabelled item."""
    if label is not None and label not in LABELS or case_id not in _cases(store, rubric):
        raise AutError("improve: answer needs an eligible case and a rubric label")
    with _locked(store.root / "human_labels"):
        labels(store, rubric)  # refuse invalid history before extending it
        case = store.get("cases", case_id)
        stamp = datetime.fromisoformat(_now())
        previous = [datetime.fromisoformat(store.get("human_labels", key)["labelled_at"])
                    for key in store.keys("human_labels")]
        if previous:
            stamp = max(stamp, max(previous) + timedelta(microseconds=1))
        row = {"evidence_sha256": evidence_key(case), "case_id": case_id,
               "rubric_id": rubric.id, "rubric_version": rubric.version, "label": label,
               "labeller": "human", "labelled_at": stamp.isoformat(), "note": None}
        # Queue failure must leave the answer unsaved, so the page can retry or undo safely.
        _sync_queue_locked(store, row)
        key = store.put("human_labels", row)
        try:
            store.append("label", label_id=key, case_id=case_id, rubric_id=rubric.id,
                         rubric_version=rubric.version)
        except OSError as exc:
            print(f"aut: label saved, but its journal entry failed ({type(exc).__name__}).",
                  file=sys.stderr)
    return key


def run(store: Store, rubric_id: str) -> dict[str, Any]:
    rubric = _rubric(rubric_id)
    _sync_queue(store)
    human = labels(store, rubric)
    newest = {row["case_id"]: row for row in judgments(store, rubric)}
    pairs = [(row["label"], newest.get(row["case_id"], {}).get("verdict"))
             for row in human.values()]
    result = score(pairs)
    counts = result["counts"]
    recall, alarm = result["failure_recall"], result["false_alarm_rate"]
    fail_labels = sum(row["label"] == "fail" for row in human.values())
    policy = _policy(rubric)
    qualified = bool(fail_labels >= policy["min_fail_labels"] and recall is not None
                     and recall >= policy["min_failure_recall"] and alarm is not None
                     and alarm <= policy["max_false_alarm_rate"])
    observed = len(pairs) - counts.get("missing", 0)
    report = {"version": 1, "rubric_id": rubric.id, "rubric_version": rubric.version,
              **result, "abstention_rate": counts.get("abstentions", 0) / observed
              if observed and result["complete"] else None,
              "fail_labels": fail_labels, "qualified": qualified, "policy": policy,
              "computed_at": _now()}
    key = store.put("qualifications", report)
    store.append("qualification", qualification_id=key, rubric_id=rubric.id,
                 rubric_version=rubric.version, qualified=qualified)
    return {"qualification_id": key, **report}


def review(store: Store, rubric_id: str, *, limit: int = 30, open_browser: bool = True,
           on_ready: Callable[[str], None] | None = None) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise AutError("improve: label limit must be 1..200")
    rubric = _rubric(rubric_id)
    cases = queue(store, rubric)[:limit]
    reqs = {case["case_id"]: judge._request(case, rubric) for case in cases}

    def case_view(case_id: str) -> bytes:
        return Path(write_case_view(store, case_id)["path"]).read_bytes()

    def save_answer(case_id: str, label: str | None) -> None:
        save(store, rubric, case_id, label)

    spec = gold.ReviewSpec(tuple(rubric.labels.items()), rubric.qualify["min_fail_labels"],
                           {row["case_id"]: row["label"]
                            for row in labels(store, rubric).values()},
                           save_answer, case_view)
    counts = gold.serve(rubric.id, reqs, zone="UTC", spec=spec, open_browser=open_browser,
                        on_ready=on_ready)
    _sync_queue(store)
    return {**run(store, rubric_id), **counts}
