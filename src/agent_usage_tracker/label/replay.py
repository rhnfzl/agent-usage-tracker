"""Private, bounded historical jobs. No teacher calls or nightly watermark changes."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import AutError, Config
from ..push import send_activity_rows
from ..repo_keys import RepoKeys
from ..state import LockBusy, _locked, atomic_write_text
from .backends import BackendUnavailable
from .core import Choice, Request, data_dir, decide, label_event, local_midnight
from .local import TASK_NAMES, LocalBackend, decorate, load_policy, reserve_label_time
from .tasks import Source, units

BATCH = 10


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _write(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, allow_nan=False))


def _identity(cfg: Config) -> str:
    value = RepoKeys(cfg.repo_key_salt, create=False).of("aut-label-replay")
    if not value:
        raise AutError("label replay: restore the existing repository salt before preparing a job")
    return value


def _directory(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise AutError("label replay: invalid job ID")
    return data_dir() / "label" / "replay" / job_id


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, ValueError) as exc:
        raise AutError("label replay: job state is missing or invalid; it was not reset") from exc


def _report(manifest: dict[str, Any], progress: dict[str, Any], delivered: int = 0,
            reason: str | None = None) -> dict[str, Any]:
    total = len(manifest["requests"])
    return {"job_id": manifest["job_id"], "task": manifest["task"],
            "since": manifest["since"], "until": manifest["until"], "total": total,
            "missing_local_text": manifest["missing_local_text"],
            "partial_local_text": manifest["partial_local_text"],
            "incomplete_outcome_context": manifest.get("incomplete_outcome_context", 0),
            "delivered": delivered, "remaining": total - progress["next"],
            "complete": progress["next"] == total, "reason": reason}


def prepare(cfg: Config, task: str, since: date, until: date, *, src: Source | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    """Freeze redacted local inputs and preview counts, without scoring or sink writes."""
    now = now or datetime.now(timezone.utc)
    if task not in TASK_NAMES or since >= until or until > now.astimezone().date():
        raise AutError("label replay: choose a local task and a nonempty range of completed dates")
    cfg.require_pg("label replay preview")
    identity = _identity(cfg)
    policy, policy_id = load_policy()
    if task not in policy["tasks"]:
        raise AutError("label replay: this task has no saved local recipe")
    start, end = local_midnight(since), local_midnight(until)
    src = src or Source(cfg, local_context=True)
    missing = len(getattr(src, "missing_text_units", []))
    partial = len(getattr(src, "partial_text_units", []))
    incomplete = len(getattr(src, "incomplete_outcome_units", []))
    # Outcome facts describe the current snapshot; daily work stops at the selected date.
    reqs = [r for r in units(task, src, start, now if task == "soft_outcome" else end)
            if start <= datetime.fromisoformat(r.ts) < end]
    reqs.sort(key=lambda r: (r.ts, r.unit, r.unit_id))
    if len({(r.task, r.unit, r.unit_id) for r in reqs}) != len(reqs):
        raise AutError("label replay: duplicate unit identities in the preview")
    job_id = uuid.uuid4().hex
    directory = _directory(job_id)
    labelled_at = reserve_label_time(directory.parent, now)
    manifest = {"version": 1, "job_id": job_id, "task": task, "since": since.isoformat(),
                "until": until.isoformat(), "asof": now.astimezone(timezone.utc).isoformat(),
                "labelled_at": labelled_at, "machine": cfg.machine, "identity": identity,
                "policy_id": policy_id, "task_policy": policy["tasks"][task],
                "missing_local_text": sum(start <= ts < end for ts in
                                          getattr(src, "missing_text_units", [])[missing:]),
                "partial_local_text": sum(start <= ts < end for ts in
                                          getattr(src, "partial_text_units", [])[partial:]),
                "incomplete_outcome_context": sum(start <= ts < end for ts in
                    getattr(src, "incomplete_outcome_units", [])[incomplete:]),
                "requests": [asdict(r) for r in reqs]}
    progress = {"manifest_sha256": _digest(manifest), "next": 0}
    directory.mkdir(mode=0o700)
    _write(directory / "manifest.json", manifest)
    _write(directory / "progress.json", progress)
    return _report(manifest, progress)


def resume(cfg: Config, job_id: str, *, apply: bool = False, budget_s: int = 600,
           backend_for: Callable[[str, dict[str, Any], str, Config], LocalBackend] | None = None,
           ) -> dict[str, Any]:
    """Resume one private generation. Only acknowledged delivery advances its cursor."""
    if type(budget_s) is not int or not 1 <= budget_s <= 3600:
        raise AutError("label replay: budget must be between 1 and 3600 seconds")
    directory = _directory(job_id)
    if not directory.is_dir():
        raise AutError("label replay: job does not exist")
    progress_path = directory / "progress.json"
    try:
        with _locked(progress_path, blocking=False):
            return _resume(cfg, job_id, directory, apply, budget_s, backend_for)
    except LockBusy as exc:
        raise AutError("label replay: this job is already running") from exc


def _resume(cfg: Config, job_id: str, directory: Path, apply: bool, budget_s: int,
            backend_for: Callable[[str, dict[str, Any], str, Config], LocalBackend] | None,
            ) -> dict[str, Any]:
    manifest = _read(directory / "manifest.json")
    progress = _read(directory / "progress.json")
    if (manifest.get("version") != 1 or manifest.get("job_id") != job_id
            or progress.get("manifest_sha256") != _digest(manifest)
            or type(progress.get("next")) is not int
            or not 0 <= progress["next"] <= len(manifest.get("requests", []))):
        raise AutError("label replay: job snapshot or cursor changed; refusing to continue")
    if manifest["machine"] != cfg.machine or manifest["identity"] != _identity(cfg):
        raise AutError("label replay: machine identity or repository salt changed")
    policy, _current_id = load_policy()
    task = manifest["task"]
    if _digest(policy["tasks"].get(task)) != _digest(manifest["task_policy"]):
        raise AutError("label replay: the task recipe changed; prepare a new job")
    if not apply or progress["next"] == len(manifest["requests"]):
        return _report(manifest, progress)
    dsn = cfg.require_pg("label replay")
    backend = (backend_for or LocalBackend)(task, manifest["task_policy"],
                                             manifest["policy_id"], cfg)
    deadline = time.monotonic() + budget_s
    delivered = 0
    prepared: list[Request] = []
    prepared_start = progress["next"]
    try:
        while progress["next"] < len(manifest["requests"]):
            if time.monotonic() >= deadline:
                return _report(manifest, progress, delivered, "budget")
            if not prepared or progress["next"] >= prepared_start + len(prepared):
                prepared_start = progress["next"]
                group_size = getattr(getattr(backend, "runtime", None), "group_size", BATCH)
                prefetch = max(BATCH, group_size // BATCH * BATCH)
                prepared = [
                    Request(**r)
                    for r in manifest["requests"][prepared_start : prepared_start + prefetch]
                ]
                for req in prepared:
                    req.rev = f"replay:{job_id}:{req.rev or ''}"
                backend.prepare_requests(prepared, deadline)
            offset = progress["next"] - prepared_start
            chunk = prepared[offset : offset + BATCH]
            events = []
            for req in chunk:
                if time.monotonic() >= deadline:
                    return _report(manifest, progress, delivered, "budget")
                raw = backend.distribution(req)
                response = decide(req, raw, Choice(), backend.model_id, backend.model_rev)
                event = decorate(label_event(req, response, manifest["labelled_at"]), req,
                                 backend, response.prob)
                event.machine = cfg.machine
                event.attrs.update(replay_job_id=job_id, input_revision=req.rev)
                events.append(event)
            send_activity_rows(dsn, events)
            progress["next"] += len(chunk)
            _write(directory / "progress.json", progress)
            delivered += len(chunk)
    except BackendUnavailable:
        if time.monotonic() >= deadline:
            return _report(manifest, progress, delivered, "budget")
        raise
    finally:
        backend.close()
    return _report(manifest, progress, delivered)
