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
from typing import Any, Callable

from ..config import LABEL_NOTE, Config
from ..episode_store import Episode
from ..event import ActivityEvent
from ..push import job_heartbeat, send_activity_rows
from ..repo_keys import refuse_new_salt
from ..sink import connect
from ..state import atomic_write_text, collector_wm, load_json_dict, load_state, save_state
from .backends import BackendUnavailable, make_backend
from .core import (Backend, Choice, NoteBackend, current_labels, data_dir, decide, label_event,
                   load_choice, local_days, local_midnight)
from .tasks import OUTCOME_DAYS, TASKS, Source, SourceUnavailable, Task, units
from .local import LocalBackend, decorate, load_policy, reserve_label_time

BATCH = 50
# The day tasks built from agents.episode, which the sink rebuilds once a night: they wait for it.
_EPISODE_TASKS = frozenset({"work_category", LABEL_NOTE})
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
    wm: dict[str, Any]
    state: dict[str, Any]
    cache: dict[str, Any]
    labelled_at: str
    ready: datetime | None = None       # Source.episodes_ready, read once per run
    state_name: str = NAME
    local_estimates: bool = False

    def advance(self, name: str, ts: str | None) -> None:
        """Save a task's watermark, never backwards: a backfill over old days leaves the nightly
        watermark where it was."""
        if ts and (not self.wm.get(name)
                   or datetime.fromisoformat(ts) > datetime.fromisoformat(self.wm[name])):
            self.wm[name] = ts
        save_state(self.state, [self.state_name])


def _window(name: str, wm: dict[str, Any], since: datetime | None, now: datetime,
            ready: datetime | None = None) -> tuple[datetime, datetime | None]:
    """(since, until) of one task. until None: up to now, and the watermark becomes the newest
    unit's ts. A day task covers whole past days (see _day_window)."""
    window = TASKS[name].window
    if window == "recent":
        if since:
            print(f"aut: label {name}: --since and --days do not apply; it always covers the last "
                  f"{OUTCOME_DAYS} days", file=sys.stderr)
        return now - timedelta(days=OUTCOME_DAYS), None
    if window == "day":
        return _day_window(name, wm, since, now, ready)
    return _start(name, wm, since, now), None


def _start(name: str, wm: dict[str, Any], since: datetime | None, now: datetime) -> datetime:
    """Where a task picks up: `since`, else its watermark, else a day ago."""
    if since:
        return since
    if wm.get(name):
        return datetime.fromisoformat(wm[name])
    return local_midnight(now.astimezone().date()) - timedelta(1)


def _day_window(name: str, wm: dict[str, Any], since: datetime | None, now: datetime,
                ready: datetime | None = None) -> tuple[datetime, datetime]:
    """(since, until) over whole past days: until is today's local midnight, or for a task built
    from episodes the midnight that starts the day of `ready`, so a day whose episodes are not
    rebuilt yet (a month delivered after a gap) waits for the next rebuild instead of being passed
    for good. Never before since: then the window is empty."""
    start = local_midnight(_start(name, wm, since, now).astimezone().date())
    until = local_midnight(now.astimezone().date())
    if name in _EPISODE_TASKS:
        until = start if ready is None else max(
            start, min(until, local_midnight(ready.astimezone().date())))
    return start, until


def _note_prompt(day: date, labels: list[tuple[Any, ...]], episodes: list[Episode]) -> str:
    """Counts only: the day's accepted labels and episode facts, never a steer text or title."""
    by = Counter((a["task"], a["label"]) for a in current_labels(labels) if not a["below_tau"])
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
    for d in local_days(since, until):
        if time.monotonic() >= run.deadline:
            c["cut"] = 1
            break
        start, end = local_midnight(d), local_midnight(d + timedelta(1))
        eps = [e for e in run.src.episodes(start) if e["first_ts"] < end]
        text = backend.generate(_note_prompt(d, run.src.labels(start, end), eps))
        c["notes"] += 1
        if not run.dry_run:
            _insert_note(run.dsn, d, text, backend.model_id)
            run.advance(LABEL_NOTE, end.isoformat())
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
    labels: Counter[str] = Counter()
    c["labels"] = labels
    key = f"{task.name}@{task.ver}|{backend.model_id}|{choice.calib_ver}"
    done = set(run.cache.get(key, ())) if task.cached else set()
    skipped = run.src.skipped["no_text"]
    reqs = [r for r in units(task.name, run.src, since, until) if r.unit_id not in done]
    if run.local_estimates:
        if not isinstance(backend, LocalBackend):
            raise BackendUnavailable("label: local estimates require a saved local decision recipe")
        backend.prepare_requests(reqs, run.deadline)
        c["provisional"] = 1
    c["skipped_no_text"] = run.src.skipped["no_text"] - skipped
    pending: list[ActivityEvent] = []
    cut = None
    for r in reqs:
        if time.monotonic() >= run.deadline:
            cut = r
            break
        raw = backend.distribution(r)       # first: it can load the model, which sets model_rev
        res = decide(r, raw, choice, backend.model_id, backend.model_rev)
        ev = label_event(r, res, run.labelled_at)
        if isinstance(backend, LocalBackend):
            ev = decorate(ev, r, backend, res.prob)
        ev.machine = run.cfg.machine
        pending.append(ev)
        c["rows"] += 1
        c["below_tau"] += ev.attrs["below_tau"]
        labels[ev.attrs["label"]] += 1
        if len(pending) >= BATCH:
            _flush(run, task, key, pending, r.ts)
    if cut is not None:
        c["cut"] = 1
        _flush(run, task, key, pending, cut.ts)
    else:
        _flush(run, task, key, pending,
               until.isoformat() if until else (reqs[-1].ts if reqs else None))
    return c


def _label_one(run: _Run, name: str, since: datetime | None, now: datetime, choice: Choice,
               backend_for: Callable[[str, Choice], Backend]) -> tuple[TaskReport, Exception | None]:
    """One task's report, and the error that stopped only it: its watermark stays, the others go
    on, and the heartbeat row records it."""
    try:
        if name != LABEL_NOTE:
            start, until = _window(name, run.wm, since, now, run.ready)
            return _run_task(run, TASKS[name], choice, backend_for(name, choice), start, until), None
        note_start, note_end = _day_window(name, run.wm, since, now, run.ready)
        backend = backend_for(name, choice)
        if not isinstance(backend, NoteBackend):    # label.json can name the encoder for it
            raise BackendUnavailable(f"label: {name} needs a backend that can write text")
        return _run_notes(run, backend, note_start, note_end), None
    except SourceUnavailable as exc:
        print(f"aut: label {name}: {exc}; retried on the next run", file=sys.stderr)
        return defaultdict(int, unavailable=1), exc
    except BackendUnavailable as exc:     # the same, and the exit status says so
        print(f"aut: {exc}", file=sys.stderr)
        return defaultdict(int, failed=1), exc


def run_label(cfg: Config, tasks: list[str] | None = None, since: datetime | None = None, *,
              dry_run: bool = False, src: Source | None = None,
              backend_for: Callable[[str, Choice], Backend] | None = None,
              now: datetime | None = None, local_estimates: bool = False) -> dict[str, TaskReport]:
    """Label each task (default: config label_tasks). backend_for(task, choice) -> backend
    (tests pass a fake). A dry run scores and counts, and writes nothing: no rows, watermark,
    cache or heartbeat. A task whose backend cannot run reports failed = 1 and the others go on."""
    dsn = cfg.require_pg("label (it reads the steer and episode rows)")
    refuse_new_salt(cfg.repo_key_salt, dsn, cfg.machine)
    started = datetime.now(timezone.utc)
    labelled_at = started.isoformat(timespec="milliseconds")
    now = now or started
    src = src or Source(cfg)
    loaded: dict[tuple[str | None, str | None, str | None, str | None], Backend] = {}
    policy, policy_id = load_policy() if local_estimates else ({}, "")
    if local_estimates and not dry_run:
        labelled_at = reserve_label_time(data_dir() / "label" / "replay", started)

    def default_backend(task: str, choice: Choice) -> Backend:
        if local_estimates:
            key = (task, "local", policy_id, None)
            if key not in loaded:
                if task not in policy["tasks"]:
                    raise BackendUnavailable("label: task has no saved local decision recipe")
                loaded[key] = LocalBackend(task, policy["tasks"][task], policy_id, cfg)
            return loaded[key]
        # one model load per run, shared across tasks; an encoder is trained for one task alone
        k = (task if choice.backend == "encoder" else None, choice.backend,
             choice.model_id or cfg.label_model, choice.model_rev)
        if k not in loaded:
            loaded[k] = make_backend(task, choice, cfg.label_model, cfg.label_llama_server)
        return loaded[k]

    backend_for = backend_for or default_backend
    state = load_state()
    state_name = "label_local" if local_estimates else NAME
    wm = collector_wm(state, state_name)
    names = tasks if tasks is not None else cfg.label_tasks
    run = _Run(cfg=cfg, dsn=dsn, src=src, dry_run=dry_run,
               deadline=time.monotonic() + cfg.label_budget_s, wm=wm, state=state,
               cache=load_json_dict(_cache_path()), labelled_at=labelled_at,
               ready=src.episodes_ready() if _EPISODE_TASKS & set(names) else None,
               state_name=state_name, local_estimates=local_estimates)
    report: dict[str, TaskReport] = {}
    with job_heartbeat(cfg, state_name, started, dry_run=dry_run) as beat:
        try:
            for name in names:
                choice = Choice() if local_estimates else load_choice(
                    name, TASKS[name].ver if name in TASKS else None)
                report[name], stopped = _label_one(run, name, since, now, choice,
                                                   backend_for)
                beat.events += report[name]["rows"] + report[name]["notes"]
                if stopped:
                    beat.fail(stopped)
        finally:
            for b in loaded.values():        # a llama-server stops with the run
                getattr(b, "close", lambda: None)()
    return report


def print_report(report: dict[str, TaskReport], *, dry_run: bool) -> None:
    mode = "DRY-RUN (nothing written)" if dry_run else "written"
    for name, c in report.items():
        if c["failed"]:      # its reason is already on stderr
            continue
        labels = dict(sorted(c.get("labels", {}).items()))
        what = f"{c['notes']} notes" if name == LABEL_NOTE else \
            f"{c['rows']} rows ({c['below_tau']} below tau), by label {json.dumps(labels)}"
        if c.get("provisional"):
            what = f"{c['rows']} provisional rows ({c['below_tau']} unknown), by label {json.dumps(labels)}"
        print(f"label {name} {mode}: {what}")
        if c["skipped_no_text"]:      # a new laptop, or transcripts deleted: say so, once a run
            print(f"aut: label {name}: {c['skipped_no_text']} steers skipped: their text is not on "
                  "this machine", file=sys.stderr)
        if c["cut"]:
            print(f"aut: label {name}: stopped by the time budget (label_budget_s); the rest "
                  "waits for the next run", file=sys.stderr)
