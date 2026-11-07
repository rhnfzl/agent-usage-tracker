"""Opt-in local outcome catch-up with acknowledged, resumable private coverage progress."""

from __future__ import annotations

import json
import re
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..config import AutError
from ..event import ActivityEvent
from ..state import LockBusy, _locked, atomic_write_text
from .backends import BackendUnavailable
from .core import Choice, Request, data_dir, decide, label_event
from .local import LocalBackend, decorate
from .outcome_coverage import coverage_key, pending_outcomes

if TYPE_CHECKING:
    from .run import _Run


def _load(path: Path, machine: str) -> tuple[set[str], bool]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return set(), False
    except (OSError, ValueError) as exc:
        raise AutError("label: outcome coverage progress is unreadable; it was not reset") from exc
    if (not isinstance(value, dict) or value.get("version") != 1
            or value.get("machine") != machine or type(value.get("next_mature")) is not bool
            or not isinstance(value.get("acknowledged"), list)
            or any(not isinstance(k, str) or not re.fullmatch(r"[a-f0-9]{64}", k)
                   for k in value["acknowledged"])):
        raise AutError("label: invalid outcome coverage progress; it was not reset")
    return set(value["acknowledged"]), value["next_mature"]


def covered_outcomes(run: _Run, requests: list[Request], backend: LocalBackend, choice: Choice,
                     report: dict[str, Any], batch_size: int,
                     deliver: Callable[[str, list[ActivityEvent]], Any]) -> None:
    path = data_dir() / "label" / "outcome-coverage.json"
    try:
        with nullcontext() if run.dry_run else _locked(path, blocking=False):
            done, next_mature = _load(path, run.cfg.machine)
            pending = pending_outcomes(requests, done, backend.policy_id, start_mature=next_mature)
            current = run.src.outcome_revisions([r for key, r in pending])
            eligible = [(key, r) for key, r in pending if current.get(r.unit_id) == r.rev]
            report["skipped_stale_outcome"] += len(pending) - len(eligible)
            backend.prepare_requests([r for key, r in eligible], run.deadline)
            report["provisional"] = 1
            batch: list[tuple[str, Request, ActivityEvent]] = []

            def flush() -> None:
                nonlocal next_mature
                revisions = run.src.outcome_revisions([r for key, r, ev in batch])
                valid = [(key, r, ev) for key, r, ev in batch
                         if revisions.get(r.unit_id) == r.rev]
                report["skipped_stale_outcome"] += len(batch) - len(valid)
                if valid and not run.dry_run:
                    deliver(run.dsn, [ev for key, r, ev in valid])
                    # Unknown is an acknowledged estimate too. Never retry it as success hunting.
                    done.update(key for key, r, ev in valid)
                    next_mature = not coverage_key(valid[-1][1], backend.policy_id)[1]
                    atomic_write_text(path, json.dumps({"version": 1, "machine": run.cfg.machine,
                        "acknowledged": sorted(done), "next_mature": next_mature}, sort_keys=True))
                for key, r, ev in valid:
                    report["rows"] += 1
                    report["below_tau"] += ev.attrs["below_tau"]
                    report["labels"][ev.attrs["label"]] += 1
                batch.clear()

            try:
                for key, request in eligible:
                    if time.monotonic() >= run.deadline:
                        report["cut"] = 1
                        break
                    raw = backend.distribution(request)
                    result = decide(request, raw, choice, backend.model_id, backend.model_rev)
                    event = decorate(label_event(request, result, run.labelled_at), request,
                                     backend, result.prob)
                    event.machine = run.cfg.machine
                    batch.append((key, request, event))
                    if len(batch) >= batch_size:
                        flush()
            except BackendUnavailable:
                flush()  # Preserve completed estimates when a later model batch exhausts its budget.
                raise
            flush()
    except LockBusy as exc:
        raise BackendUnavailable("label: outcome coverage is already running") from exc
