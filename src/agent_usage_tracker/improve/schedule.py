"""A locked local cycle with a hard subprocess deadline and durable progress."""

from __future__ import annotations

import html
import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import AutError
from ..state import LockBusy, _locked, atomic_write_text
from .judge import diagnose
from .store import Store, audit
from .trial import command


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
             explain_model: str | None = None) -> dict[str, Any]:
    if not 1 <= budget_s <= 3600:
        raise AutError("improve: cycle budget must be 1..3600 seconds")
    policy = {"version": 1, "days": days, "limit": limit, "case_limit": case_limit,
              "budget_s": budget_s, "max_calls": max_calls, "models": list(models),
              "explain_model": explain_model}
    key = store.put("cycle_policies", policy)
    run_id = uuid.uuid4().hex
    try:
        with _locked(store.root / "cycle", blocking=False):
            argv = [sys.executable, "-m", "agent_usage_tracker.improve.schedule", "--root",
                    str(store.root), "--policy", key, "--run-id", run_id]
            result = command(argv, Path(__file__).resolve().parents[3], budget_s)
            report: dict[str, Any] = {"status": "timeout" if result["timeout"] else "error",
                                      "policy_id": key, "exit_code": result["exit_code"]}
            if result["accepted"]:
                try:
                    saved = json.loads((store.root / "worker_result.json").read_text())
                    if saved["run_id"] != run_id:
                        raise ValueError("stale worker result")
                    report = saved["report"]
                except (OSError, ValueError, KeyError) as exc:
                    raise AutError("improve: worker did not preserve its matching result") from exc
            store.append("cycle_finished", **report)
            atomic_write_text(store.root / "last_cycle.json", json.dumps(report, indent=2) + "\n")
            inbox(store, report.get("diagnosis", {}).get("judgment_ids", []))
            return report
    except LockBusy:
        return {"status": "busy"}


def _worker(store: Store, policy_id: str, run_id: str) -> dict[str, Any]:
    policy = store.get("cycle_policies", policy_id)
    started = time.monotonic()
    pending_path = store.root / "pending_cycle.json"
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
    if pending.get("policy_id") == policy_id and pending.get("audit"):
        frozen = pending["audit"]
        store.get("audits", frozen["audit_id"])
    else:
        frozen = audit(store, Path.home(), days=policy["days"], limit=policy["limit"])
        atomic_write_text(pending_path, json.dumps({"policy_id": policy_id, "audit": frozen}) + "\n")
    remaining = int(policy["budget_s"] - (time.monotonic() - started))
    judged = diagnose(store, frozen["audit_id"], models=tuple(policy["models"]),
                      limit=policy["case_limit"], budget_s=max(1, remaining),
                      max_calls=policy["max_calls"], explain_model=policy["explain_model"])
    report = {"status": "ok" if judged["complete"] else "deferred",
              "policy_id": policy_id, "audit": frozen, "diagnosis": judged}
    if judged["complete"]:
        atomic_write_text(pending_path, json.dumps({"policy_id": policy_id, "audit": None}) + "\n")
    atomic_write_text(store.root / "worker_result.json",
                      json.dumps({"run_id": run_id, "report": report}) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal isolated local improvement worker")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    _worker(Store(args.root), args.policy, args.run_id)
