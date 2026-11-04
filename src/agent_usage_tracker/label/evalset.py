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
import os
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, TypeVar

from ..config import LABEL_DAYS, AutError, Config
from ..state import atomic_write_text, parse_record
from .core import Request, data_dir, local_midnight, top_label
from .tasks import TASKS, Source, units

T = TypeVar("T")
# Gold per label, (floor, target), as the gold page shows them. AW titles: 150 per class.
FLOORS = {"work_category": (30, 50), "steer_intent": (30, 50), "soft_outcome": (30, 30),
          "aw_title_relevance": (150, 150)}


def eval_path(task: str, kind: str) -> Path:
    """kind: silver | gold | eval."""
    return data_dir() / "eval" / (f"{task}.jsonl" if kind == "gold" else f"{task}.{kind}.jsonl")


def read_jsonl(p: Path) -> list[dict]:
    try:
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:    # never "no labels": those labels exist
        raise AutError(f"label: cannot read {p}: {exc}") from None
    # None: a line cut by a crash mid-write
    return [o for line in lines if (o := parse_record(line)) is not None]


def append_jsonl(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)  # private, as write_jsonl's is
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def write_jsonl(p: Path, rows: list[dict]) -> None:
    atomic_write_text(p, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def gold_latest(task: str) -> dict[str, str]:
    """unit_id -> gold label of the current task version; a later answer for a unit wins."""
    ver, ids = TASKS[task].ver, {c for c, _d in TASKS[task].candidates}
    return {r["unit_id"]: r["gold"] for r in read_jsonl(eval_path(task, "gold"))
            if r.get("task_ver") == ver and r.get("gold") in ids}


def silver_by_unit(task: str) -> dict[str, dict[str, dict[str, float]]]:
    """unit_id -> {teacher: dist} of the current task version."""
    out: dict[str, dict] = defaultdict(dict)
    for r in read_jsonl(eval_path(task, "silver")):
        if r.get("task_ver") == TASKS[task].ver and isinstance(r.get("dist"), dict):
            out[r["unit_id"]][r["teacher"]] = r["dist"]
    return dict(out)


def stratum(teachers: dict | None) -> str:
    """The silver stratum of a unit: the teachers' common top label, `disagree`, or
    `unlabelled`."""
    tops = {top_label(d) for d in (teachers or {}).values() if d}
    return "unlabelled" if not tops else tops.pop() if len(tops) == 1 else "disagree"


def stratified(items: list[T], key: Callable[[T], object], seed: str) -> list[T]:
    """Round-robin over the strata of `key`, each stratum shuffled: rare strata come up as often
    as common ones. Deterministic for a seed, so a resumed sitting sees the same order."""
    rng = random.Random(seed)  # nosec B311 # a repeatable sample order, not security
    groups: dict[object, list[T]] = defaultdict(list)
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


def in_encoder_train_split(req: Request) -> bool:
    """Of the non-held-out gold, the half the encoder trains on; the other half fits its
    temperature and tau in eval, so they are never fitted on the encoder's own training data."""
    return _side("encoder|" + req.unit_id) == 1


def window_units(cfg: Config, task: str, days: int = LABEL_DAYS, src: Source | None = None,
                 today: date | None = None) -> dict[str, Request]:
    """unit_id -> request for the task's units over the last `days` whole local days."""
    if src is None:
        cfg.require_pg("label (it reads the steer and episode rows)")
    today = today or date.today()
    reqs = units(task, src or Source(cfg), local_midnight(today - timedelta(days=days)),
                 local_midnight(today))
    return {r.unit_id: r for r in reqs}
