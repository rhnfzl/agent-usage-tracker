"""`aut label run [--task T] [--since D] [--dry-run]` and `aut label backfill --task T --days N`.

Nightly after the scan (its own step in aut-scan.sh, its own heartbeat collector `label`). Per
task: build the units since the watermark, score them inside the time budget, send the label rows
in batches and advance the watermark after each delivered batch, so an interrupted run resumes
where it stopped (a rerun of a unit is a no-op at the sink). The run never logs text.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, cast

from ..config import Config
from ..event import ActivityEvent
from ..push import deliver_scan_rows, scan_row, scrub_error, send_activity_rows
from ..sink import connect
from ..state import atomic_write_text, collector_wm, load_json_dict, load_state, save_state
from .backends import BackendUnavailable, make_backend
from .core import Backend, Choice, NoteBackend, data_dir, decide, label_event, load_choice, local_midnight
from .names import NOTE
from .tasks import OUTCOME_DAYS, TASKS, Source, SourceUnavailable, Task, units

BATCH = 50
# One task's counts, read as 0 when unset, plus `labels`: the rows per label.
TaskReport = defaultdict[str, Any]
NAME = "label"


def _cache_path() -> Path:
    return data_dir() / "label" / "cache.json"


@dataclass(frozen=True, slots=True)
class _Run:
    """What stays fixed across the tasks of one label run. wm is state's label section."""
    cfg: Config
    dsn: str
    src: Source
    dry_run: bool
    deadline: float
    wm: dict
    state: dict
    cache: dict
    labelled_at: str

    def advance(self, name: str, ts: str | None) -> None:
        """Save a task's watermark, never backwards: a backfill over old days leaves the nightly
        watermark where it was."""
        if ts and (not self.wm.get(name)
                   or datetime.fromisoformat(ts) > datetime.fromisoformat(self.wm[name])):
            self.wm[name] = ts
        save_state(self.state)


def _window(name: str, wm: dict, since: datetime | None,
            now: datetime) -> tuple[datetime, datetime | None]:
    """(since, until) of one task. until None: up to now, and the watermark becomes the newest
    unit's ts. A day task covers whole past days, so until is today's local midnight."""
    window = "day" if name == NOTE else TASKS[name].window
    if window == "recent":
        return now - timedelta(days=OUTCOME_DAYS), None
    today = local_midnight(now.astimezone().date())
    start = since or (datetime.fromisoformat(wm[name]) if wm.get(name) else today - timedelta(1))
    if window == "day":
        return local_midnight(start.astimezone().date()), today
    return start, None


def _note_prompt(day: date, labels: list[dict], episodes: list[dict]) -> str:
    """Counts only: the day's accepted labels and episode facts, never a steer text or title."""
    # the row agents.label_latest picks: newest task_ver, then newest labelled_at
    latest = {(a.get("task"), a.get("unit_id")): a for a in sorted(
        labels, key=lambda a: (a.get("task_ver") or 0, a.get("labelled_at") or ""))}
    by = Counter((a["task"], a["label"]) for a in latest.values() if not a.get("below_tau"))
    lines = [f"Write a short private note (3 to 5 sentences) about the work day {day}. Use only "
             "these facts and invent nothing.", "", "Confident labels (task: label count):"]
    lines += [f"- {t}: {lab} {n}" for (t, lab), n in sorted(by.items())] or ["- none"]
    outs = Counter(e["outcome"] for e in episodes)
    lines += ["", f"Episodes active: {len(episodes)} (outcomes: "
              f"{', '.join(f'{k} {v}' for k, v in sorted(outs.items())) or 'none'}), commits "
              f"{sum(e['n_commits'] for e in episodes)}, sessions "
              f"{sum(e['n_sessions'] for e in episodes)}."]
    return "\n".join(lines)


def _insert_note(dsn: str, day: date, text: str, model_id: str) -> None:
    with connect(dsn) as conn:
        conn.execute("INSERT INTO agents.daily_note (day, text, model_id, created_at) "
                     "VALUES (%s, %s, %s, now()) ON CONFLICT (day) DO NOTHING",
                     (day, text, model_id))
        conn.commit()


def _run_notes(run: _Run, backend: NoteBackend, since: datetime, until: datetime) -> TaskReport:
    c: TaskReport = defaultdict(int)
    d = since.astimezone().date()
    while d < until.astimezone().date():
        if time.monotonic() >= run.deadline:
            c["cut"] = 1
            break
        start, end = local_midnight(d), local_midnight(d + timedelta(1))
        eps = [e for e in run.src.episodes(start) if e["first_ts"] < end]
        text = backend.generate(_note_prompt(d, run.src.labels(start, end), eps))
        c["notes"] += 1
        if not run.dry_run:
            _insert_note(run.dsn, d, text, backend.model_id)
            run.advance(NOTE, end.isoformat())
        d += timedelta(1)
    return c


def _flush(run: _Run, task: Task, key: str, pending: list[ActivityEvent],
           next_wm: str | None) -> None:
    """Deliver a batch, then remember it and move the watermark. A dry run only drops it."""
    if not run.dry_run:
        if pending:
            send_activity_rows(run.dsn, pending)      # raises: watermark stays
        if task.cached and pending:
            run.cache.setdefault(key, []).extend(e.attrs["unit_id"] for e in pending)
            atomic_write_text(_cache_path(), json.dumps(run.cache))
        if task.window != "recent":
            run.advance(task.name, next_wm)
    pending.clear()


def _run_task(run: _Run, task: Task, choice: Choice, backend: Backend, since: datetime,
              until: datetime | None) -> TaskReport:
    c: TaskReport = defaultdict(int)
    labels: Counter = Counter()
    c["labels"] = labels
    key = f"{task.name}@{task.ver}|{backend.model_id}|{choice.calib_ver}"
    done = set(run.cache.get(key, ())) if task.cached else set()
    reqs = [r for r in units(task.name, run.src, since, until) if r.unit_id not in done]
    pending: list[ActivityEvent] = []
    cut = None
    for r in reqs:
        if time.monotonic() >= run.deadline:
            cut = r
            break
        raw = backend.distribution(r)       # first: it can load the model, which sets model_rev
        res = decide(r, raw, choice, backend.model_id, backend.model_rev)
        ev = label_event(r, res, run.labelled_at)
        ev.machine = run.cfg.machine
        pending.append(ev)
        c["rows"] += 1
        c["below_tau"] += res.below_tau
        labels[res.chosen] += 1
        if len(pending) >= BATCH:
            _flush(run, task, key, pending, r.ts)
    if cut is not None:
        c["cut"] = 1
        _flush(run, task, key, pending, cut.ts)
    else:
        _flush(run, task, key, pending,
               until.isoformat() if until else (reqs[-1].ts if reqs else None))
    return c


def _label_one(run: _Run, name: str, since: datetime, until: datetime | None, choice: Choice,
               backend_for: Callable[[str, Choice], Backend]) -> tuple[TaskReport, Exception | None]:
    """One task's report, and the error that stopped only it: its watermark stays, the others go
    on, and the heartbeat row records it."""
    try:
        backend = backend_for(name, choice)
        if name != NOTE:
            return _run_task(run, TASKS[name], choice, backend, since, until), None
        # `aut label choose` never picks a note's backend: no encoder
        if until is None:               # a note's window is whole days
            raise ValueError(f"label {name}: a day window has no end")
        return _run_notes(run, cast(NoteBackend, backend), since, until), None
    except SourceUnavailable as exc:
        print(f"aut: label {name}: {exc}; retried on the next run", file=sys.stderr)
        return defaultdict(int, unavailable=1), exc
    except BackendUnavailable as exc:     # the same, and the exit status says so
        print(f"aut: {exc}", file=sys.stderr)
        return defaultdict(int, failed=1), exc


def run_label(cfg: Config, tasks: list[str] | None = None, since: datetime | None = None, *,
              dry_run: bool = False, src: Source | None = None,
              backend_for: Callable[[str, Choice], Backend] | None = None,
              now: datetime | None = None) -> dict[str, TaskReport]:
    """Label each task (default: config label_tasks). backend_for(task, choice) -> backend
    (tests pass a fake). A dry run scores and counts, and writes nothing: no rows, watermark,
    cache or heartbeat. A task whose backend cannot run reports failed = 1 and the others go on."""
    dsn = cfg.require_pg("label (it reads the steer and episode rows)")
    started = datetime.now(timezone.utc)
    labelled_at = started.isoformat(timespec="milliseconds")
    now = now or started
    src = src or Source(cfg)
    loaded: dict = {}

    def default_backend(task: str, choice: Choice) -> Backend:
        k = (choice.backend, choice.model_id or cfg.label_model, choice.model_rev)
        if k not in loaded:                  # one model load per run, shared across tasks
            loaded[k] = make_backend(task, choice, cfg.label_model)
        return loaded[k]

    backend_for = backend_for or default_backend
    state = load_state()
    wm = collector_wm(state, NAME)
    run = _Run(cfg=cfg, dsn=dsn, src=src, dry_run=dry_run,
               deadline=time.monotonic() + cfg.label_budget_s, wm=wm, state=state,
               cache=load_json_dict(_cache_path()), labelled_at=labelled_at)
    report: dict[str, TaskReport] = {}
    status, error, interrupted = "ok", None, False
    try:
        for name in tasks if tasks is not None else cfg.label_tasks:
            s, u = _window(name, wm, since, now)
            report[name], stopped = _label_one(run, name, s, u, load_choice(name), backend_for)
            if stopped:
                status, error = "error", scrub_error(stopped)
        return report
    except BaseException as exc:
        status, error = "error", scrub_error(exc)
        interrupted = isinstance(exc, KeyboardInterrupt)    # leave at once, send nothing
        raise
    finally:
        if not dry_run and not interrupted:
            events = sum(c["rows"] + c["notes"] for c in report.values())
            deliver_scan_rows(cfg, [scan_row(cfg, NAME, started, status=status, error=error,
                                             events=events)], NAME)


def print_report(report: dict[str, TaskReport], dry_run: bool) -> None:
    mode = "DRY-RUN (nothing written)" if dry_run else "written"
    for name, c in report.items():
        if c["failed"]:      # its reason is already on stderr
            continue
        labels = dict(sorted(c.get("labels", {}).items()))
        what = f"{c['notes']} notes" if name == NOTE else \
            f"{c['rows']} rows ({c['below_tau']} below tau), by label {json.dumps(labels)}"
        print(f"label {name} {mode}: {what}")
        if c["cut"]:
            print(f"aut: label {name}: stopped by the time budget (label_budget_s); the rest "
                  "waits for the next run", file=sys.stderr)
