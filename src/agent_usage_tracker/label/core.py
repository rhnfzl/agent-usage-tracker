"""The decision interface every label task uses, its calibration, and the stored label row.

A backend returns a raw distribution over the request's candidates. Temperature scaling (one
scalar per task and model, fitted on gold labels) turns it into the calibrated distribution, and a
per-label threshold tau decides whether the top answer counts: below tau the row is still written,
with below_tau = true, and readers treat it as unknown. "Unknown" is never a candidate.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, runtime_checkable

from ..config import config_path
from ..event import ActivityEvent, make_event_id

PROMPT_VER = 1               # bump when render() changes; task_ver covers candidates/definitions
LETTERS = "ABCDEFGHIJ"
TAU_FLOOR = 0.5


def local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


def local_days(since: datetime, until: datetime) -> Iterator[date]:
    """The local dates from `since`'s up to, not including, `until`'s."""
    d, end = since.astimezone().date(), until.astimezone().date()
    while d < end:
        yield d
        d += timedelta(days=1)


def data_dir() -> Path:
    """Where label data that never leaves this machine lives (gold and silver under eval/)."""
    base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(base) / "aut"


def choice_path() -> Path:
    """The chosen backend, model, temperature and tau per task (written by `aut label choose`)."""
    return config_path().parent / "label.json"


@dataclass(slots=True)
class Request:
    task: str
    task_ver: int
    unit: str                  # steer | episode_day | aw_block | episode | aw_title
    unit_id: str
    ts: str                    # the unit's own time (ISO-8601): the label row's ts
    facts: dict                # what the decision reads; text in it is redacted, never stored
    question: str
    decision: str              # choice | boolean
    candidates: list[dict]     # [{"id", "desc"}]: the task's closed list
    none_fits: str | None      # the explicit out-of-list candidate ("other"), or None
    rev: str | None = None     # the unit's version, when a changed unit is re-asked (episode)

    @property
    def prompt_ver(self) -> str:
        return f"{self.task}@{self.task_ver}/p{PROMPT_VER}"


@dataclass(slots=True)
class Response:
    dist: dict[str, float]     # calibrated, over every candidate
    chosen: str
    prob: float
    below_tau: bool
    model_id: str
    model_rev: str
    prompt_ver: str
    calib_ver: str


@dataclass(slots=True)
class Choice:
    """One task's winner. The default is uncalibrated: T = 1 and no tau, so nothing passes."""
    backend: str = "mlx_letter"
    model_id: str | None = None
    model_rev: str | None = None
    temperature: float = 1.0
    # label -> tau; a missing or null label never passes
    tau: dict[str, float | None] = field(default_factory=dict)
    calib_ver: str = "none"


class Backend(Protocol):
    """What a decision reads. model_rev and size_mb may only be final after the first distribution
    call, and model_rev goes into the label row's event_id: read them after scoring."""
    model_id: str
    model_rev: str
    size_mb: float | None

    def distribution(self, req: Request) -> dict[str, float]: ...


@runtime_checkable
class NoteBackend(Backend, Protocol):
    """A backend that can also write free text, for the private nightly note."""

    def generate(self, prompt: str, max_tokens: int = 300) -> str: ...


def read_choices() -> dict[str, Any]:
    """label.json; {} when there is none yet. Raises OSError or ValueError when it is unusable."""
    try:
        obj = json.loads(choice_path().read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(obj, dict) or not isinstance(obj.get("tasks", {}), dict):
        raise ValueError("unexpected shape")
    return obj


def _is_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_entry(task: str, c: object) -> None:
    """Raises ValueError for a label.json entry whose values decide() could not use."""
    if not isinstance(c, dict):
        raise ValueError(f"its entry for {task} is not a table")
    t, tau = c.get("temperature", 1.0), c.get("tau", {})
    if not _is_number(t) or t <= 0:
        raise ValueError(f"the temperature of {task} is not a positive number")
    if not isinstance(tau, dict) or not all(v is None or _is_number(v) for v in tau.values()):
        raise ValueError(f"the tau of {task} is not a table of numbers")


def load_choice(task: str) -> Choice:
    try:
        c = read_choices().get("tasks", {}).get(task)
        if c is not None:
            _check_entry(task, c)
    except (OSError, ValueError) as exc:
        print(f"aut: warning: {choice_path()} is unusable ({exc}); {task} runs uncalibrated, so "
              "none of its labels pass tau. Fix it or move it aside and re-run `aut label choose`.", file=sys.stderr)
        return Choice()
    if c is None:
        return Choice()
    return Choice(**{k: c[k] for k in Choice.__slots__ if k in c})


def render(req: Request, order: list[str]) -> str:
    """The prompt for one candidate order: candidates as letters, the facts, a one-letter answer."""
    desc = {c["id"]: c["desc"] for c in req.candidates}
    lines = [req.question, "", "Facts:"]
    lines += [f"- {k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
              for k, v in req.facts.items()]
    lines += ["", "Options:"] + [f"{LETTERS[i]}. {c}: {desc[c]}" for i, c in enumerate(order)]
    lines += ["", f"Answer with one letter ({', '.join(LETTERS[:len(order)])}) and nothing else."]
    return "\n".join(lines)


def softmax(xs: list[float]) -> list[float]:
    m = max(xs)
    e = [math.exp(x - m) for x in xs]
    s = sum(e)
    return [v / s for v in e]


def top_label(dist: dict[str, float]) -> str:
    return max(dist, key=lambda label: dist[label])


def letter_distribution(letter_logits: Callable[[str, list[str]], list[float]],
                        req: Request) -> dict[str, float]:
    """letter_logits(prompt, letters) -> one logit per letter at the answer position. Scores the
    original and the reversed candidate order and averages the two (letter position bias)."""
    ids = [c["id"] for c in req.candidates]
    if not 1 <= len(ids) <= len(LETTERS):
        raise ValueError(f"{req.task}: {len(ids)} candidates, at most {len(LETTERS)}")
    out = dict.fromkeys(ids, 0.0)
    orders = (ids, ids[::-1])
    for order in orders:
        for c, p in zip(order, softmax(letter_logits(render(req, order),
                                                     list(LETTERS[:len(order)])))):
            out[c] += p / len(orders)
    return out


def temper(dist: dict[str, float], t: float) -> dict[str, float]:
    """Temperature scaling of a distribution: softmax(log p / t)."""
    keys = list(dist)
    return dict(zip(keys, softmax([math.log(max(dist[k], 1e-12)) / t for k in keys])))


def fit_temperature(dists: list[dict], golds: list[str], lo: float = 0.05,
                    hi: float = 20.0) -> float:
    """The T that minimises the gold labels' negative log likelihood (golden section on log T;
    the NLL of temperature scaling is unimodal in it). 1.0 without data."""
    if not dists:
        return 1.0

    def nll(lt: float) -> float:
        t = math.exp(lt)
        return -sum(math.log(max(temper(d, t)[g], 1e-12)) for d, g in zip(dists, golds))

    a, b, r = math.log(lo), math.log(hi), (math.sqrt(5) - 1) / 2
    for _ in range(80):
        c, d = b - r * (b - a), a + r * (b - a)
        if nll(c) < nll(d):
            b = d
        else:
            a = c
    return round(math.exp((a + b) / 2), 4)


def fit_tau(items: list[tuple[str, float, str]], precision: float = 0.90,
            floor: float = TAU_FLOOR) -> dict[str, float | None]:
    """items: (chosen, p_cal, gold). Per chosen label: the smallest p at which the items chosen as
    that label with p >= it reach `precision` (cuts only between distinct p), raised to `floor`;
    None when no cut reaches it (the label never passes)."""
    by: dict[str, list[tuple[float, bool]]] = {}
    for chosen, p, gold in items:
        by.setdefault(chosen, []).append((p, chosen == gold))
    out: dict[str, float | None] = {}
    for label, rows in by.items():
        rows.sort(key=lambda r: -r[0])
        best, ok = None, 0
        for i, (p, hit) in enumerate(rows):
            ok += hit
            if (i + 1 == len(rows) or rows[i + 1][0] < p) and ok / (i + 1) >= precision:
                best = p
        out[label] = None if best is None else max(best, floor)
    return out


def decide(req: Request, raw: dict[str, float], choice: Choice, model_id: str,
           model_rev: str) -> Response:
    dist = temper(raw, choice.temperature)
    chosen = max(dist, key=dist.__getitem__)
    tau = choice.tau.get(chosen)
    return Response(dist=dist, chosen=chosen, prob=dist[chosen],
                    below_tau=tau is None or dist[chosen] < tau, model_id=model_id,
                    model_rev=model_rev, prompt_ver=req.prompt_ver, calib_ver=choice.calib_ver)


def label_event(req: Request, res: Response, labelled_at: str | None = None) -> ActivityEvent:
    """The activity_event row of one decision: enums, numbers and versions, never the facts.
    Same unit, task version, model and calibration -> same id, so a rerun is a no-op at the sink;
    a new model, prompt or calibration (or a changed episode, req.rev) adds a row and never
    overwrites one. labelled_at (UTC, fixed width) is the run's start: agents.label_latest takes
    the newest one, so a later calibrated row wins over an earlier uncalibrated one."""
    labelled_at = labelled_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return ActivityEvent(
        event_id=make_event_id("label", req.task, req.task_ver, req.unit_id, res.model_id,
                               res.model_rev, res.prompt_ver, res.calib_ver,
                               *((req.rev,) if req.rev else ())),
        ts=req.ts, agent="label", kind="label", session_id=None, turn_id=None,
        attrs={"task": req.task, "task_ver": req.task_ver, "unit": req.unit,
               "unit_id": req.unit_id, "label": res.chosen, "p": round(res.prob, 4),
               "dist": {k: round(v, 4) for k, v in res.dist.items()},
               "below_tau": res.below_tau, "model_id": res.model_id, "model_rev": res.model_rev,
               "prompt_ver": res.prompt_ver, "calib_ver": res.calib_ver,
               "labelled_at": labelled_at})


def current_labels(rows: Iterable[tuple]) -> list[dict]:
    """The one current label per (task, unit, unit_id) of (event_id, ts, attrs) rows, picked as
    agents.label_latest picks it: newest task_ver, then newest labelled_at (a row without one
    loses), then event_id. A row with no below_tau counts as below: readers treat it as unknown."""
    best: dict[tuple, tuple] = {}
    for eid, ts, a in rows:
        a = a or {}
        key = (a.get("task"), a.get("unit"), a.get("unit_id"))
        rank = (int(a.get("task_ver") or 0), a.get("labelled_at") or "", eid)
        if key not in best or rank > best[key][0]:
            below = a.get("below_tau")
            best[key] = (rank, {"task": key[0], "unit": key[1], "unit_id": key[2], "ts": ts,
                                "label": a.get("label"), "p": a.get("p"),
                                "below_tau": True if below is None else bool(below),
                                "model_id": a.get("model_id"), "task_ver": rank[0]})
    return [v for _r, v in best.values()]
