"""A locked local cycle with a hard subprocess deadline and durable progress."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import threading
from contextlib import ExitStack, contextmanager
from collections.abc import Iterator
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from types import FrameType

from ..config import AutError
from ..state import LockBusy, _locked, atomic_write_text
from . import groups
from .inbox import write as inbox
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


def _pending(store: Store) -> dict[str, Any]:
    path = store.root / "pending_cycle.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _pending_write(store: Store) -> tuple[int, int] | None:
    """Identity of the last pending_cycle.json write; every atomic write makes a new file."""
    try:
        info = os.stat(store.root / "pending_cycle.json")
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_mtime_ns


def _reusable_audit(store: Store, pending: dict[str, Any], days: int,
                    limit: int) -> dict[str, Any] | None:
    """The pending frozen audit when it was built for these days and limit.
    Budget and call limits do not change the evidence, so they no longer discard it."""
    frozen = pending.get("audit")
    if not frozen:
        return None
    try:
        source = store.get("audits", frozen["audit_id"]).get("source_policy")
    except (AutError, KeyError, TypeError):    # missing or changed record: rebuild, never fail nightly
        return None
    if source is None:      # pre-v4 manifests: the days and limit of the policy that froze it
        try:
            source = store.get("cycle_policies", pending["policy_id"])
        except (AutError, KeyError, TypeError):
            return None
    return frozen if (source.get("days"), source.get("limit")) == (days, limit) else None


def freeze_cycle_audit(store: Store, *, days: int = 7, limit: int = 30,
                       budget_s: int = 1500) -> dict[str, Any]:
    """Freeze the next scheduled cycle's audit, so the reviewer spends its budget on judging."""
    if not 1 <= budget_s <= 3600:
        raise AutError("improve: audit budget must be 1..3600 seconds")
    try:
        with _locked(store.root / "cycle_audit", blocking=False):
            result = _freeze(store, days, limit, budget_s)
    except LockBusy:
        result = {"status": "busy"}
    store.append("cycle_audit", status=result["status"], audit_id=result.get("audit_id"))
    return result


def _freeze(store: Store, days: int, limit: int, budget_s: int) -> dict[str, Any]:
    # Content alone cannot tell a cycle that finished during the build from last night's.
    before = _pending_write(store)
    if (kept := _reusable_audit(store, _pending(store), days, limit)):
        return {"status": "kept", **kept}
    # Build without the cycle lock: after sleep launchd fires this and the reviewer together,
    # and the reviewer must not lose its night to a busy lock. The worst case is a duplicate build.
    try:
        with _cooperative_deadline(time.monotonic() + budget_s):
            fresh = audit(store, Path.home(), days=days, limit=limit)
    except _BudgetExpired:
        return {"status": "budget"}
    try:
        with _locked(store.root / "cycle", blocking=False):
            if (kept := _reusable_audit(store, _pending(store), days, limit)):
                return {"status": "kept", **kept}
            if _pending_write(store) != before:     # a cycle finished and judged this evidence
                return {"status": "superseded", "audit_id": fresh["audit_id"]}
            atomic_write_text(store.root / "pending_cycle.json", json.dumps({"audit": fresh}) + "\n")
            return {"status": "frozen", **fresh}
    except LockBusy:        # a running cycle owns pending; the built audit record is harmless
        return {"status": "busy", "audit_id": fresh["audit_id"]}


def run_once(store: Store, *, days: int = 7, limit: int = 30, case_limit: int = 12,
             budget_s: int = 300, max_calls: int = 40,
             models: tuple[str, ...] = ("pplx-decider-flash", "clef-flash"),
             explain_model: str | None = None, audit_id: str | None = None,
             audit_budget_s: int = 0) -> dict[str, Any]:
    if not 1 <= budget_s <= 3600:
        raise AutError("improve: cycle budget must be 1..3600 seconds")
    if not 0 <= audit_budget_s <= 3600:
        raise AutError("improve: audit budget must be 0..3600 seconds")
    reserve = min(5., budget_s / 10)
    policy = {"version": 1, "days": days, "limit": limit, "case_limit": case_limit,
              "budget_s": budget_s, "max_calls": max_calls, "models": list(models),
              "explain_model": explain_model, "audit_budget_s": audit_budget_s}
    if audit_id is not None:
        store.get("audits", audit_id)
        policy["audit_id"] = audit_id
    key = store.put("cycle_policies", policy)
    run_id = uuid.uuid4().hex
    try:
        with _locked(store.root / "cycle", blocking=False):
            # A run that must build its own audit gets that time on top of the judge's budget.
            needs_audit = audit_id is None and audit_budget_s and not _reusable_audit(
                store, _pending(store), days, limit)
            deadline = time.monotonic() + budget_s + (audit_budget_s if needs_audit else 0)
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
            diagnosis = report.get("diagnosis", report.get("progress", {}).get("diagnosis", {}))
            if audit_id is None and diagnosis.get("complete"):
                # A worker killed after its early release never wrote pending again; a freezer
                # that started after that release must still see a new write identity.
                atomic_write_text(store.root / "pending_cycle.json",
                                  json.dumps({"policy_id": key, "audit": None}) + "\n")
            store.append("cycle_finished", **report)
            atomic_write_text(store.root / "last_cycle.json", json.dumps(report, indent=2) + "\n")
            try:    # after the worker, outside its budget; old judgments are worth grouping too
                groups.refresh(store)
            except Exception as exc:    # grouping must never fail the cycle or skip the inbox
                store.append("groups_error", error=type(exc).__name__)
            inbox(store, diagnosis.get("judgment_ids", []))
            return report
    except LockBusy:
        return {"status": "busy"}


def _worker(store: Store, policy_id: str, run_id: str, *,
            deadline: float | None = None) -> dict[str, Any]:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    policy = store.get("cycle_policies", policy_id)
    if deadline is not None and not math.isfinite(deadline):
        raise AutError("improve: invalid worker deadline")
    budget_s = policy["budget_s"]
    pending_path = store.root / "pending_cycle.json"
    provided = policy.get("audit_id")
    reuse = None if provided else _reusable_audit(store, _pending(store), policy["days"],
                                                  policy["limit"])
    allowance = 0 if provided or reuse else policy.get("audit_budget_s", 0)
    outer = min(deadline if deadline is not None else started + budget_s + allowance,
                started + budget_s + allowance)
    judged: dict[str, Any] = {"complete": False, "reason": "budget", "calls": 0,
                              "finished": 0, "judgment_ids": []}
    report: dict[str, Any] = {"status": "running", "policy_id": policy_id, "audit": None,
                              "diagnosis": judged, "worker_started_at": started_at, "audit_s": 0}
    if provided:
        store.get("audits", provided)
        frozen = {"audit_id": provided}
    elif reuse:
        frozen = reuse
    else:
        try:
            # With an audit allowance the audit has its own cap; without one it keeps today's
            # shared deadline, which only the outer subprocess timeout enforces.
            with _cooperative_deadline(min(outer, started + allowance)) if allowance else ExitStack():
                frozen = audit(store, Path.home(), days=policy["days"], limit=policy["limit"])
        except _BudgetExpired:
            report.update(status="deferred", audit_s=round(time.monotonic() - started, 3))
            atomic_write_text(store.root / "worker_result.json",
                              json.dumps({"run_id": run_id, "report": report}) + "\n")
            return report
        atomic_write_text(pending_path, json.dumps({"policy_id": policy_id, "audit": frozen}) + "\n")
        report["audit_s"] = round(time.monotonic() - started, 3)
    report["audit"] = frozen
    # The judge's own budget starts after the audit; the outer deadline still bounds both.
    deadline = min(outer, time.monotonic() + budget_s) - min(5., budget_s / 10)

    cleared = False

    def checkpoint(progress: dict[str, Any]) -> None:
        nonlocal judged, cleared
        judged = progress
        report["diagnosis"] = judged
        # Blocking work done: release the audit now, so an alarm or a kill during a non-blocking
        # rubric's call cannot hold it open for the next night.
        if judged.get("complete") and not provided and not cleared:
            atomic_write_text(pending_path, json.dumps({"policy_id": policy_id, "audit": None}) + "\n")
            cleared = True
        atomic_write_text(store.root / "worker_result.json",
                          json.dumps({"run_id": run_id, "report": report}) + "\n")

    checkpoint(judged)
    try:
        with _cooperative_deadline(deadline):
            judged = diagnose(store, frozen["audit_id"], models=tuple(policy["models"]),
                              limit=policy["case_limit"], budget_s=policy["budget_s"],
                              max_calls=policy["max_calls"], explain_model=policy["explain_model"],
                              deadline=deadline, progress=checkpoint)
    except _BudgetExpired:     # keeps the last checkpoint's complete: blocking work may be done
        judged = {**judged, "reason": "budget"}
    report.update(status="ok" if judged["complete"] else "deferred", diagnosis=judged)
    # Write again even after an early release: a freezer that started after that release must
    # see a new write identity, or it would freeze an audit while this cycle was still judging.
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
