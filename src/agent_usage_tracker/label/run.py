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
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .. import __version__
from ..config import Config
from ..push import scrub_error, send_activity_rows, send_scan_rows
from ..state import atomic_write_text, collector_wm, load_json_dict, load_state, save_state
from .backends import make_backend
from .core import data_dir, decide, label_event, load_choice
from .tasks import NOTE, OUTCOME_DAYS, TASKS, Source, SourceUnavailable, units

BATCH = 50
NAME = "label"


def local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


def _cache_path():
    return data_dir() / "label" / "cache.json"


def _window(name: str, wm: dict, since: datetime | None, now: datetime):
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


def _advance(wm: dict, name: str, ts: str | None) -> None:
    """Never backwards: a backfill over old days leaves the nightly watermark where it was."""
    if ts and (not wm.get(name) or datetime.fromisoformat(ts) > datetime.fromisoformat(wm[name])):
        wm[name] = ts


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
    import psycopg  # optional dependency; install with the [postgres] extra

    with psycopg.connect(dsn, connect_timeout=15) as conn:
        conn.execute("INSERT INTO agents.daily_note (day, text, model_id, created_at) "
                     "VALUES (%s, %s, %s, now()) ON CONFLICT (day) DO NOTHING",
                     (day, text, model_id))
        conn.commit()


def _run_notes(cfg, src, backend, since, until, dry_run, deadline, wm, state) -> Counter:
    c: Counter = Counter()
    d = since.astimezone().date()
    while d < until.astimezone().date():
        if time.monotonic() >= deadline:
            c["cut"] = 1
            break
        start, end = local_midnight(d), local_midnight(d + timedelta(1))
        eps = [e for e in src.episodes(start) if e["first_ts"] < end]
        text = backend.generate(_note_prompt(d, src.labels(start, end), eps))
        c["notes"] += 1
        if not dry_run:
            _insert_note(cfg.pg_dsn, d, text, backend.model_id)
            _advance(wm, NOTE, end.isoformat())
            save_state(state)
        d += timedelta(1)
    return c


def _run_task(cfg, task, src, choice, backend, since, until, dry_run, deadline, wm, state,
              cache, labelled_at) -> Counter:
    c: Counter = Counter()
    key = f"{task.name}@{task.ver}|{backend.model_id}|{choice.calib_ver}"
    done = set(cache.get(key, ())) if task.cached else set()
    reqs = [r for r in units(task.name, src, since, until) if r.unit_id not in done]
    pending: list = []

    def flush(next_wm: str | None) -> None:
        if not dry_run:
            if pending:
                send_activity_rows(cfg.pg_dsn, pending)      # raises: watermark stays
            if task.cached and pending:
                cache.setdefault(key, []).extend(e.attrs["unit_id"] for e in pending)
                atomic_write_text(_cache_path(), json.dumps(cache))
            if task.window != "recent":
                _advance(wm, task.name, next_wm)
                save_state(state)
        pending.clear()

    cut = None
    for r in reqs:
        if time.monotonic() >= deadline:
            cut = r
            break
        res = decide(r, backend.distribution(r), choice, backend.model_id, backend.model_rev)
        ev = label_event(r, res, labelled_at)
        ev.machine = cfg.machine
        pending.append(ev)
        c["rows"] += 1
        c["below_tau"] += res.below_tau
        c[f"label_{res.chosen}"] += 1
        if len(pending) >= BATCH:
            flush(r.ts)
    if cut is not None:
        c["cut"] = 1
        flush(cut.ts)
    else:
        flush(until.isoformat() if until else (reqs[-1].ts if reqs else None))
    return c


def run_label(cfg: Config, tasks: list[str] | None = None, since: datetime | None = None,
              dry_run: bool = False, src: Source | None = None, backend_for=None,
              now: datetime | None = None) -> dict[str, Counter]:
    """Label each task (default: config label_tasks). backend_for(task, choice) -> backend
    (tests pass a fake). A dry run scores and counts, and writes nothing: no rows, watermark,
    cache or heartbeat."""
    if not cfg.has_pg:
        raise SystemExit("label needs transport.pg_dsn (it reads the steer and episode rows)")
    started = datetime.now(timezone.utc)
    labelled_at = started.isoformat(timespec="milliseconds")
    now = now or started
    src = src or Source(cfg)
    loaded: dict = {}

    def default_backend(task: str, choice):
        k = (choice.backend, choice.model_id or cfg.label_model, choice.model_rev)
        if k not in loaded:                  # one model load per run, shared across tasks
            loaded[k] = make_backend(task, choice, cfg.label_model)
        return loaded[k]

    backend_for = backend_for or default_backend
    state = load_state()
    wm = collector_wm(state, NAME)
    cache = load_json_dict(_cache_path())
    deadline = time.monotonic() + cfg.label_budget_s
    report: dict[str, Counter] = {}
    status, error = "ok", None
    try:
        for name in tasks if tasks is not None else cfg.label_tasks:
            s, u = _window(name, wm, since, now)
            choice = load_choice(name)
            backend = backend_for(name, choice)
            if name == NOTE:
                report[name] = _run_notes(cfg, src, backend, s, u, dry_run, deadline, wm, state)
            else:
                try:
                    report[name] = _run_task(cfg, TASKS[name], src, choice, backend, s, u,
                                             dry_run, deadline, wm, state, cache, labelled_at)
                except SourceUnavailable as exc:      # its watermark stays; the others go on
                    print(f"aut: label {name}: {exc}; retried on the next run", file=sys.stderr)
                    report[name] = Counter(unavailable=1)
                    status, error = "error", scrub_error(exc)
        return report
    except BaseException as exc:
        status, error = "error", scrub_error(exc)
        raise
    finally:
        if not dry_run:
            try:            # best effort, like the scan heartbeat
                send_scan_rows(cfg, [{
                    "machine": cfg.machine, "collector": NAME, "started_at": started,
                    "finished_at": datetime.now(timezone.utc), "status": status,
                    "events": sum(c["rows"] + c["notes"] for c in report.values()), "human": 0,
                    "newest_ts": None, "error": error, "aut_version": __version__}])
            except Exception as exc:
                print(f"aut: warning: label heartbeat not delivered: {scrub_error(exc)}",
                      file=sys.stderr)


def print_report(report: dict[str, Counter], dry_run: bool) -> None:
    mode = "DRY-RUN (nothing written)" if dry_run else "written"
    for name, c in report.items():
        labels = {k[6:]: v for k, v in sorted(c.items()) if k.startswith("label_")}
        what = f"{c['notes']} notes" if name == NOTE else \
            f"{c['rows']} rows ({c['below_tau']} below tau), by label {json.dumps(labels)}"
        print(f"label {name} {mode}: {what}")
        if c["cut"]:
            print(f"aut: label {name}: stopped by the time budget (label_budget_s); the rest "
                  "waits for the next run", file=sys.stderr)
