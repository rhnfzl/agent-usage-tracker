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
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import config_path
from ..event import ActivityEvent, make_event_id

PROMPT_VER = 1               # bump when render() changes; task_ver covers candidates/definitions
LETTERS = "ABCDEFGHIJ"
TAU_FLOOR = 0.5


def local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


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
    state: dict                # the facts the decision reads; text in it is redacted, never stored
    question: str
    decision: str              # choice | boolean
    candidates: list[dict]     # [{"id", "desc"}]: the task's closed list
    none_fits: str | None      # the explicit out-of-list candidate ("other"), or None
    rev: str | None = None     # the unit's state version, when a changed unit is re-asked (episode)

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
    tau: dict = field(default_factory=dict)     # label -> tau; a missing or null label never passes
    calib_ver: str = "none"


def read_choices() -> dict:
    """label.json; {} when there is none yet. Raises OSError or ValueError when it is unusable."""
    try:
        obj = json.loads(choice_path().read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(obj, dict) or not isinstance(obj.get("tasks", {}), dict):
        raise ValueError("unexpected shape")
    return obj


def load_choice(task: str) -> Choice:
    try:
        obj = read_choices()
    except (OSError, ValueError) as exc:
        print(f"aut: warning: {choice_path()} is unusable ({exc}); {task} runs uncalibrated, so "
              "none of its labels pass tau. Fix it or move it aside and re-run `aut label choose`.", file=sys.stderr)
        return Choice()
    c = (obj.get("tasks") or {}).get(task)
    if not isinstance(c, dict):
        return Choice()
    return Choice(**{k: c[k] for k in Choice.__slots__ if k in c})


def render(req: Request, order: list[str]) -> str:
    """The prompt for one candidate order: candidates as letters, the facts, a one-letter answer."""
    desc = {c["id"]: c["desc"] for c in req.candidates}
    lines = [req.question, "", "Facts:"]
    lines += [f"- {k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
              for k, v in req.state.items()]
    lines += ["", "Options:"] + [f"{LETTERS[i]}. {c}: {desc[c]}" for i, c in enumerate(order)]
    lines += ["", f"Answer with one letter ({', '.join(LETTERS[:len(order)])}) and nothing else."]
    return "\n".join(lines)


def softmax(xs: list[float]) -> list[float]:
    m = max(xs)
    e = [math.exp(x - m) for x in xs]
    s = sum(e)
    return [v / s for v in e]


def letter_distribution(letter_logits, req: Request) -> dict[str, float]:
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
    """The activity_event row of one decision: enums, numbers and versions, never the state.
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
