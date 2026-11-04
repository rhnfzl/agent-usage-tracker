"""The label files that never leave this machine, and the unit order the teacher and the gold page
share.

Under core.data_dir() / "eval": <task>.silver.jsonl (teacher distributions: unit_id, teacher,
dist, date, task_ver), <task>.jsonl (gold: unit_id, gold, labeller, date, task_ver) and
<task>.eval.jsonl (one bake-off record per backend, numbers only). No row holds text: a unit's
text is rebuilt from this machine's logs (tasks.units) whenever it is needed.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import Config
from ..state import atomic_write_text
from .core import Request, data_dir, local_midnight
from .names import DAYS
from .tasks import TASKS, Source, units

# map.md section 6: gold per label, (floor, target). AW titles: 150 per class.
FLOORS = {"work_category": (30, 50), "steer_intent": (30, 50), "soft_outcome": (30, 30),
          "aw_title_relevance": (150, 150)}


def path(task: str, kind: str) -> Path:
    """kind: silver | gold | eval."""
    return data_dir() / "eval" / (f"{task}.jsonl" if kind == "gold" else f"{task}.{kind}.jsonl")


def read_jsonl(p: Path) -> list[dict]:
    try:
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:    # never "no labels": those labels exist
        raise SystemExit(f"label: cannot read {p}: {exc}") from None
    out = []
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue                  # a line cut by a crash mid-write
        if isinstance(o, dict):
            out.append(o)
    return out


def append_jsonl(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def write_jsonl(p: Path, rows: list[dict]) -> None:
    atomic_write_text(p, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def gold_latest(task: str) -> dict[str, str]:
    """unit_id -> gold label of the current task version; a later answer for a unit wins."""
    ver, ids = TASKS[task].ver, {c for c, _d in TASKS[task].candidates}
    return {r["unit_id"]: r["gold"] for r in read_jsonl(path(task, "gold"))
            if r.get("task_ver") == ver and r.get("gold") in ids}


def silver_by_unit(task: str) -> dict[str, dict[str, dict[str, float]]]:
    """unit_id -> {teacher: dist} of the current task version."""
    out: dict[str, dict] = defaultdict(dict)
    for r in read_jsonl(path(task, "silver")):
        if r.get("task_ver") == TASKS[task].ver and isinstance(r.get("dist"), dict):
            out[r["unit_id"]][r["teacher"]] = r["dist"]
    return dict(out)


def stratum(teachers: dict | None) -> str:
    """The silver stratum of a unit: the teachers' common top label, `disagree`, or
    `unlabelled`."""
    tops = {max(d, key=d.get) for d in (teachers or {}).values() if d}
    return "unlabelled" if not tops else tops.pop() if len(tops) == 1 else "disagree"


def stratified(items: list, key, seed: str) -> list:
    """Round-robin over the strata of `key`, each stratum shuffled: rare strata come up as often
    as common ones. Deterministic for a seed, so a resumed session sees the same order."""
    rng = random.Random(seed)
    groups: dict = defaultdict(list)
    for x in items:
        groups[key(x)].append(x)
    keys = sorted(groups, key=str)
    for k in keys:
        rng.shuffle(groups[k])
    rng.shuffle(keys)
    return [x for row in itertools.zip_longest(*(groups[k] for k in keys)) for x in row
            if x is not None]


def unit_day(req: Request) -> date:
    return datetime.fromisoformat(req.ts).astimezone().date()


def _side(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % 2


def held_out(req: Request) -> bool:
    """Gold is split once, by the unit's local day: about half the days calibrate and choose, the
    other half report. A day never changes sides, so a held-out answer never tunes anything. A
    title's ts is its first sight inside the window read, which moves, so it splits by unit_id."""
    return _side(req.unit_id if req.unit == "aw_title" else unit_day(req).isoformat()) == 1


def encoder_fit(req: Request) -> bool:
    """Of the non-held-out gold, the half the encoder trains on; the other half fits its
    temperature and tau in eval, so they are never fitted on the encoder's own training data."""
    return _side("encoder|" + req.unit_id) == 1


def window_units(cfg: Config, task: str, days: int = DAYS, src: Source | None = None,
                 today: date | None = None) -> dict[str, Request]:
    """unit_id -> request for the task's units over the last `days` whole local days."""
    if src is None and not cfg.has_pg:
        raise SystemExit("label needs transport.pg_dsn (it reads the steer and episode rows)")
    today = today or date.today()
    reqs = units(task, src or Source(cfg), local_midnight(today - timedelta(days=days)),
                 local_midnight(today))
    return {r.unit_id: r for r in reqs}
