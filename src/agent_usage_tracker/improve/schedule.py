"""A locked local cycle with a hard subprocess deadline and durable progress."""

from __future__ import annotations

import html
import argparse
import json
import math
import signal
import threading
from contextlib import contextmanager
from collections.abc import Iterator
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from types import FrameType

from ..config import AutError
from ..state import LockBusy, _locked, atomic_write_text
from .judge import diagnose
from .store import Store, audit
from .trial import command


class _BudgetExpired(BaseException):
    # Model loading wraps ordinary exceptions as failures; cancellation must pass through.
    pass


@contextmanager
def _cooperative_deadline(deadline: float) -> Iterator[None]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _BudgetExpired
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expired(signum: int, frame: FrameType | None) -> None:
        raise _BudgetExpired

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                            max(.001, previous_timer[0] - (time.monotonic() - started)),
                            previous_timer[1])


def inbox(store: Store, judgment_ids: list[str]) -> None:
    rows = [store.get("judgments", key) for key in judgment_ids]
    entries = []
    for row in rows:
        review = row.get("review") or {}
        entries.append("<tr><td>" + html.escape(row["rubric"]) + "</td><td>"
                       + html.escape(row["verdict"]) + "</td><td>"
                       + html.escape(review.get("rationale", "Local decision-model estimate"))
                       + "</td><td><code>" + html.escape(row["case_id"][:12]) + "</code></td></tr>")
    page = ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            "<title>Private improvement inbox</title><style>"
            "body{font:16px system-ui;margin:2rem;max-width:75rem;line-height:1.5}"
            "td,th{text-align:left;padding:.7rem;border-bottom:1px solid #ccc}"
            "table{border-collapse:collapse;width:100%} .scroll{overflow:auto}"
            "</style><h1>Private improvement inbox</h1>"
            "<p>Local model estimates. Unknowns remain unresolved. These are not human labels "
            "or proof of task quality. Routine review does not require a human.</p>"
            "<div class=scroll role=region aria-label='Model judgments' tabindex=0><table>"
            "<tr><th>Criterion</th><th>Estimate</th><th>Evidence review</th><th>Case</th></tr>"
            + "".join(entries) + "</table></div></html>")
    atomic_write_text(store.root / "inbox.html", page)


def run_once(store: Store, *, days: int = 7, limit: int = 30, case_limit: int = 12,
             budget_s: int = 300, max_calls: int = 40,
             models: tuple[str, ...] = ("pplx-decider-flash", "clef-flash"),
             explain_model: str | None = None, audit_id: str | None = None) -> dict[str, Any]:
    if not 1 <= budget_s <= 3600:
        raise AutError("improve: cycle budget must be 1..3600 seconds")
    deadline = time.monotonic() + budget_s
    reserve = min(5., budget_s / 10)
    policy = {"version": 1, "days": days, "limit": limit, "case_limit": case_limit,
              "budget_s": budget_s, "max_calls": max_calls, "models": list(models),
              "explain_model": explain_model}
    if audit_id is not None:
        store.get("audits", audit_id)
        policy["audit_id"] = audit_id
    key = store.put("cycle_policies", policy)
    run_id = uuid.uuid4().hex
    try:
        with _locked(store.root / "cycle", blocking=False):
            argv = [sys.executable, "-I", "-m", "agent_usage_tracker.improve.schedule", "--root",
                    str(store.root), "--policy", key, "--run-id", run_id,
                    "--deadline", str(deadline)]
            result = command(argv, store.root, max(.001, deadline - time.monotonic() - reserve / 2))
            report: dict[str, Any] = {"status": "timeout" if result["timeout"] else "error",
                                      "policy_id": key, "exit_code": result["exit_code"]}
            if not result["accepted"]:
                report["error_id"] = store.put("cycle_errors", {
                    "run_id": run_id, "policy_id": key, **result})
            try:
                saved = json.loads((store.root / "worker_result.json").read_text())
                if saved["run_id"] != run_id or saved["report"]["policy_id"] != key:
                    raise ValueError("stale worker result")
                if result["accepted"]:
                    if saved["report"]["status"] not in {"ok", "deferred"}:
                        raise ValueError("unfinished worker result")
                    report = saved["report"]
                else:
                    report["progress"] = saved["report"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if result["accepted"]:
                    raise AutError("improve: worker did not preserve its matching result") from exc
            store.append("cycle_finished", **report)
            atomic_write_text(store.root / "last_cycle.json", json.dumps(report, indent=2) + "\n")
            inbox(store, report.get("diagnosis", report.get("progress", {}).get("diagnosis", {}))
                  .get("judgment_ids", []))
            return report
    except LockBusy:
        return {"status": "busy"}


def _worker(store: Store, policy_id: str, run_id: str, *,
            deadline: float | None = None) -> dict[str, Any]:
    started = time.monotonic()
    policy = store.get("cycle_policies", policy_id)
    if deadline is not None and not math.isfinite(deadline):
        raise AutError("improve: invalid worker deadline")
    deadline = min(deadline if deadline is not None else started + policy["budget_s"],
                   started + policy["budget_s"]) - min(5., policy["budget_s"] / 10)
    pending_path = store.root / "pending_cycle.json"
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
    provided = policy.get("audit_id")
    if provided:
        store.get("audits", provided)
        frozen = {"audit_id": provided}
    elif pending.get("policy_id") == policy_id and pending.get("audit"):
        frozen = pending["audit"]
        store.get("audits", frozen["audit_id"])
    else:
        frozen = audit(store, Path.home(), days=policy["days"], limit=policy["limit"])
        atomic_write_text(pending_path, json.dumps({"policy_id": policy_id, "audit": frozen}) + "\n")
    judged: dict[str, Any] = {"complete": False, "reason": "budget", "calls": 0,
                              "finished": 0, "judgment_ids": []}
    report = {"status": "running", "policy_id": policy_id, "audit": frozen,
              "diagnosis": judged}

    def checkpoint(progress: dict[str, Any]) -> None:
        nonlocal judged
        judged = progress
        report["diagnosis"] = judged
        atomic_write_text(store.root / "worker_result.json",
                          json.dumps({"run_id": run_id, "report": report}) + "\n")

    checkpoint(judged)
    try:
        with _cooperative_deadline(deadline):
            judged = diagnose(store, frozen["audit_id"], models=tuple(policy["models"]),
                              limit=policy["case_limit"], budget_s=policy["budget_s"],
                              max_calls=policy["max_calls"], explain_model=policy["explain_model"],
                              deadline=deadline, progress=checkpoint)
    except _BudgetExpired:
        judged = {**judged, "complete": False, "reason": "budget"}
    report.update(status="ok" if judged["complete"] else "deferred", diagnosis=judged)
    if judged["complete"] and not provided:
        atomic_write_text(pending_path, json.dumps({"policy_id": policy_id, "audit": None}) + "\n")
    atomic_write_text(store.root / "worker_result.json",
                      json.dumps({"run_id": run_id, "report": report}) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal isolated local improvement worker")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--deadline", type=float)
    args = parser.parse_args()
    _worker(Store(args.root), args.policy, args.run_id, deadline=args.deadline)
