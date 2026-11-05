"""The label files that never leave this machine, and the unit order the teacher and the gold page
share.

Under core.data_dir() / "eval": <task>.silver.jsonl (teacher distributions: unit_id, teacher,
dist, date, task_ver), <task>.jsonl (gold: unit_id, gold, labeller, date, task_ver),
<task>.eval.jsonl (one bake-off record per backend, numbers only), <task>.ensemble.jsonl (one record
per combination tried, numbers only), <task>.final.jsonl (results on the final test days) and scores/ (one file per task, model, revision and prompt: unit_id and raw
dist). No row holds text: a unit's text is rebuilt from this machine's logs (tasks.units) whenever
it is needed.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import random
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

from ..config import LABEL_DAYS, AutError, Config
from ..state import atomic_write_text, parse_record
from .core import Request, data_dir, local_midnight, top_label
from .tasks import TASKS, Source, units

T = TypeVar("T")
# Gold per label, (floor, target), as the gold page shows them. AW titles: 150 per class.
FLOORS = {"work_category": (30, 50), "steer_intent": (30, 50), "soft_outcome": (30, 30),
          "aw_title_relevance": (150, 150)}
FOLDS = 5
FINAL_PCT = 20               # share of the days kept for `aut label eval --final`


def eval_path(task: str, kind: str) -> Path:
    """kind: silver | gold | eval | ensemble | final."""
    return data_dir() / "eval" / (f"{task}.jsonl" if kind == "gold" else f"{task}.{kind}.jsonl")


def scores_path(task: str, model_id: str, model_rev: str, prompt_ver: str,
                protocol: str = "") -> Path:
    """The saved raw scores of one task, model, revision, prompt and decision protocol."""
    parts = (task, model_id, model_rev, prompt_ver) + ((protocol,) if protocol else ())
    key = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    name = re.sub(r"[^\w.-]+", "-", model_id)
    return data_dir() / "eval" / "scores" / f"{task}.{name}.{key[:12]}.jsonl"


def read_jsonl(p: Path) -> list[dict[str, Any]]:
    try:
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:    # never "no labels": those labels exist
        raise AutError(f"label: cannot read {p}: {exc}") from None
    # None: a line cut by a crash mid-write
    return [o for line in lines if (o := parse_record(line)) is not None]


def append_jsonl(p: Path, row: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)  # private, as write_jsonl's is
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def write_jsonl(p: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write_text(p, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def gold_latest(task: str) -> dict[str, str]:
    """unit_id -> gold label of the current task version; a later answer for a unit wins."""
    ver, ids = TASKS[task].ver, {c for c, _d in TASKS[task].candidates}
    return {r["unit_id"]: r["gold"] for r in read_jsonl(eval_path(task, "gold"))
            if r.get("task_ver") == ver and r.get("gold") in ids}


def silver_by_unit(task: str) -> dict[str, dict[str, dict[str, float]]]:
    """unit_id -> {teacher: dist} of the current task version."""
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    for r in read_jsonl(eval_path(task, "silver")):
        if r.get("task_ver") == TASKS[task].ver and isinstance(r.get("dist"), dict):
            out[r["unit_id"]][r["teacher"]] = r["dist"]
    return dict(out)


def stratum(teachers: dict[str, Any] | None) -> str:
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


def _hash(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16)


def _group(req: Request) -> str:
    """What must stay together in a split: the unit's local day. A title's ts is its first sight
    inside the window read, which moves, so a title is its own group."""
    return req.unit_id if req.unit == "aw_title" else unit_day(req).isoformat()


def final_test(req: Request) -> bool:
    """The stable part of the days only `aut label eval --final` reads: no fold, tau, temperature,
    encoder fit or choice sees them."""
    return _hash("final|" + _group(req)) % 100 < FINAL_PCT


def fold(req: Request) -> int:
    """The unit's fold, the same for every model and across runs. shortcut: whole days hash to a
    fold, so folds are not stratified by label and a rare label can sit in few folds; upgrade by
    assigning days to folds by their gold label mix once gold is large enough to care."""
    return _hash("fold|" + _group(req)) % FOLDS


def in_encoder_train_split(req: Request) -> bool:
    """The half of the days the encoder trains on; the other half fits its temperature and tau in
    eval, so they are never fitted on the encoder's own training data."""
    return _hash("encoder|" + _group(req)) % 2 == 1


def window_units(cfg: Config, task: str, days: int = LABEL_DAYS, src: Source | None = None,
                 today: date | None = None) -> dict[str, Request]:
    """unit_id -> request for the task's units over the last `days` whole local days."""
    if src is None:
        cfg.require_pg("label (it reads the steer and episode rows)")
    today = today or date.today()
    reqs = units(task, src or Source(cfg), local_midnight(today - timedelta(days=days)),
                 local_midnight(today))
    return {r.unit_id: r for r in reqs}
