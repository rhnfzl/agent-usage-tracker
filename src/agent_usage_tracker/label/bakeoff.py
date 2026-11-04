"""`aut label eval`, `aut label choose` and `aut label train-encoder`: the bake-off on gold.

Gold is split once by day (evalset.held_out): the calibration half fits the temperature and the
per-label tau, the held-out half reports. Per backend, one eval record of numbers (and model
ids): per-label precision / recall / F1 / tau / coverage at tau / share error, macro-F1, Cohen's
kappa, ECE before and after temperature scaling, and seconds per decision. It is appended to
<task>.eval.jsonl and sent as an activity_event of kind `eval`. `choose` writes the smallest
backend that passes the bar into label.json, which the nightly run reads.
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone

from ..config import AutError, Config
from ..event import ActivityEvent, make_event_id
from ..push import send_activity_rows
from ..state import atomic_write_text
from .backends import MODELS, Encoder, encoder_path, features, make_backend
from .core import (Backend, Choice, choice_path, fit_tau, fit_temperature, read_choices, softmax,
                   temper, top_label)
from .evalset import (DAYS, append_jsonl, eval_path, gold_latest, held_out,
                      in_encoder_train_split, read_jsonl, silver_by_unit, stratum, window_units)
from .tasks import TASKS, Source

SHARE_ERROR_PP = 5.0         # a label's share on known items may be off this much (CONTRACT.md)
GOLD_WEIGHT = 3.0            # train-encoder: one gold answer counts as three silver ones


def _round4(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def wilson_lb(k: int, n: int, z: float = 1.96) -> float | None:
    """Lower bound of the 95% Wilson interval of k right out of n."""
    if not n:
        return None
    p = k / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) \
        / (1 + z * z / n)


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa of (predicted, gold)."""
    if not pairs:
        return None
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    pa, pb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(pa[x] * pb[x] for x in pa) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def ece(tops: list[tuple[float, bool]], bins: int = 10) -> float | None:
    """Expected calibration error of (top probability, was right), equal-width bins."""
    if not tops:
        return None
    by: dict[int, list] = {}
    for p, hit in tops:
        by.setdefault(min(int(p * bins), bins - 1), []).append((p, hit))
    return sum(abs(sum(p for p, _ in b) / len(b) - sum(h for _, h in b) / len(b)) * len(b)
               for b in by.values()) / len(tops)


def weights(task: str, golds: list[str]) -> dict[str, float]:
    """unit_id -> weight that re-weights the stratified gold sample to natural frequency: the
    share of its silver stratum among every silver unit over its share among the gold units.
    shortcut: the silver sample stands in for the population (teach samples by day and unit
    type, not by label); upgrade by counting nightly rows once a task runs nightly."""
    silver = silver_by_unit(task)
    pop = Counter(stratum(t) for t in silver.values())
    sample = Counter(stratum(silver.get(u)) for u in golds)
    tp, ts = sum(pop.values()), len(golds)
    return {u: (pop[s] / tp) / (sample[s] / ts) if pop[s] else 1.0
            for u in golds for s in [stratum(silver.get(u))]}


def evaluate(task: str, backend: Backend, items: list[tuple], precision: float,
             today: date) -> dict:
    """items: (request, gold, held_out, weight). The eval record of one backend (numbers)."""
    ver, labels = TASKS[task].ver, [c for c, _d in TASKS[task].candidates]
    t0 = time.monotonic()
    raws = [backend.distribution(r) for r, *_ in items]
    secs = (time.monotonic() - t0) / max(len(items), 1)
    enc = isinstance(backend, Encoder)      # its training half never calibrates it
    cal = [i for i, it in enumerate(items)
           if not it[2] and not (enc and in_encoder_train_split(it[0]))]
    held = [i for i, it in enumerate(items) if it[2]]
    t = fit_temperature([raws[i] for i in cal], [items[i][1] for i in cal])
    dists = [temper(d, t) for d in raws]
    top = [top_label(d) for d in dists]
    tau = fit_tau([(top[i], dists[i][top[i]], items[i][1]) for i in cal], precision)
    rep = held or cal        # with too few days for a split, report on what there is (n_held 0)
    gold = {i: items[i][1] for i in rep}
    w = {i: items[i][3] for i in rep}
    passed = [i for i in rep
              if (cut := tau.get(top[i])) is not None and dists[i][top[i]] >= cut]
    wk, wa = sum(w[i] for i in passed), sum(w.values())
    per: dict[str, dict] = {}
    for lab in labels:
        tp = sum(top[i] == lab == gold[i] for i in rep)
        pred = sum(top[i] == lab for i in rep)
        true = sum(gold[i] == lab for i in rep)
        p = tp / pred if pred else None
        r = tp / true if true else None
        ok = [i for i in passed if top[i] == lab]
        hits = sum(gold[i] == lab for i in ok)
        per[lab] = {"n": true, "precision": _round4(p), "recall": _round4(r),
                    "f1": _round4(2 * p * r / (p + r)) if p and r else (0.0 if pred or true else None),
                    "tau": _round4(tau.get(lab)), "coverage": _round4(len(ok) / pred) if pred else None,
                    "precision_at_tau": _round4(hits / len(ok)) if ok else None,
                    "precision_lb": _round4(wilson_lb(hits, len(ok))),
                    "share_error_pp": _round4(abs(sum(w[i] for i in passed if top[i] == lab) / wk
                                             - sum(w[i] for i in rep if gold[i] == lab) / wa)
                                         * 100) if wk else None}
    f1s = [v["f1"] for v in per.values() if v["f1"] is not None]
    return {"task": task, "task_ver": ver, "backend": "encoder" if isinstance(backend, Encoder)
            else "mlx_letter", "model_id": backend.model_id, "model_rev": backend.model_rev,
            "size_mb": backend.size_mb, "prompt_ver": items[0][0].prompt_ver,
            "calib_ver": f"{task}@{ver}/{today.isoformat()}", "temperature": t, "n": len(items),
            "n_cal": len(cal), "n_held": len(held),
            "macro_f1": _round4(sum(f1s) / len(f1s)) if f1s else None,
            "kappa": _round4(kappa([(top[i], gold[i]) for i in rep])),
            "ece_raw": _round4(ece([(max(raws[i].values()), top_label(raws[i]) == gold[i])
                               for i in rep])),
            "ece_cal": _round4(ece([(dists[i][top[i]], top[i] == gold[i]) for i in rep])),
            "coverage": _round4(len(passed) / len(rep)) if rep else None,
            "s_per_decision": _round4(secs), "per_label": per}


def gold_items(cfg: Config, task: str, days: int, src: Source | None,
               today: date | None = None) -> tuple[list[tuple], int]:
    """(request, gold, held_out, weight) for every gold unit still in the window; and how many
    gold units fell out of it."""
    gold = gold_latest(task)
    reqs = window_units(cfg, task, days, src, today)
    found = [u for u in gold if u in reqs]
    w = weights(task, found)
    return [(reqs[u], gold[u], held_out(reqs[u]), w[u]) for u in found], len(gold) - len(found)


def default_backends(cfg: Config, task: str) -> list[Backend]:
    """Every local model with an MLX build, plus the encoder once trained."""
    repos = list(dict.fromkeys([r for r in MODELS.values() if r] + [cfg.label_model]))
    out = [make_backend(task, Choice(model_id=r), cfg.label_model) for r in repos]
    if encoder_path(task).exists():
        out.append(Encoder(task))
    for name, repo in MODELS.items():
        if repo is None:
            print(f"aut: label eval: {name} has no official MLX build yet; left out",
                  file=sys.stderr)
    return out


def run_eval(cfg: Config, task: str, days: int = DAYS, *, src: Source | None = None,
             backends: list[Backend] | None = None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    items, gone = gold_items(cfg, task, days, src, today)
    if not items:
        raise AutError(f"label eval: no gold labels for {task} in the last {days} days "
                       "(label some with `aut label review`)")
    if gone:
        print(f"aut: label eval: {gone} gold units are outside the {days}-day window; left out",
              file=sys.stderr)
    recs = []
    for b in backends if backends is not None else default_backends(cfg, task):
        rec = evaluate(task, b, items, cfg.label_precision, today)
        append_jsonl(eval_path(task, "eval"), {"ts": now.isoformat(), **rec})
        if cfg.pg_dsn:
            send_activity_rows(cfg.pg_dsn, [ActivityEvent(
                event_id=make_event_id("eval", json.dumps(rec, sort_keys=True)),
                ts=now.isoformat(), agent="label", kind="eval", session_id=None, turn_id=None,
                attrs=rec, machine=cfg.machine)])
        recs.append(rec)
    return recs


def passes(rec: dict, coverage: float, precision: float) -> bool:
    """The bar (CONTRACT.md, the bake-off): a held-out report, coverage at tau at least label_coverage, every
    label that passed held-out items at its tau right at least `precision` of the time, and no
    label's share on known items off by more than SHARE_ERROR_PP."""
    per = rec["per_label"].values()
    errs = [v["share_error_pp"] for v in per]
    return bool(rec["n_held"]) and (rec["coverage"] or 0) >= coverage \
        and all(v.get("precision_at_tau") is None or v["precision_at_tau"] >= precision
                for v in per) \
        and all(e is not None and e <= SHARE_ERROR_PP for e in errs)


def choose(cfg: Config, task: str) -> dict:
    """Write the smallest backend whose newest eval passes into label.json; others untouched."""
    newest: dict[str, dict] = {}
    for r in read_jsonl(eval_path(task, "eval")):
        if r.get("task_ver") == TASKS[task].ver:
            newest[r["model_id"]] = r                   # the file is in time order
    ok = [r for r in newest.values() if passes(r, cfg.label_coverage, cfg.label_precision)]
    if not ok:
        why = {m: {"coverage": r["coverage"], "n_held": r["n_held"]} for m, r in newest.items()}
        raise AutError(f"label choose: no backend passes the bar for {task}: {json.dumps(why)}")
    win = min(ok, key=lambda r: (r["size_mb"] if r["size_mb"] is not None else math.inf,
                                 -(r["macro_f1"] or 0)))
    p = choice_path()
    try:
        obj = read_choices()
    except (OSError, ValueError) as exc:    # overwriting would lose every other task's choice
        raise AutError(f"label choose: {p} is unusable ({exc}); nothing written. Fix or move "
                       "it aside and run choose again for each task.") from None
    obj.setdefault("tasks", {})[task] = {
        "backend": win["backend"], "model_id": win["model_id"], "model_rev": win["model_rev"],
        "temperature": win["temperature"], "calib_ver": win["calib_ver"],
        "tau": {k: v["tau"] for k, v in win["per_label"].items() if v["tau"] is not None}}
    atomic_write_text(p, json.dumps(obj, indent=2, sort_keys=True))
    return obj["tasks"][task]


MIN_UPDATES = 2000


def _fit(data: list[tuple[list[int], list[float], float]], k: int, epochs: int = 12,
         lr: float = 0.3, l2: float = 1e-4, seed: int = 0) -> dict[int, list[float]]:
    """Multinomial logistic regression by SGD on (features, target distribution, weight)."""
    w: dict[int, list[float]] = {}
    rng, data = random.Random(seed), list(data)
    # a small gold set gets more passes: 12 passes over 20 items underfit even a separable set
    epochs = max(epochs, -(-MIN_UPDATES // max(1, len(data))))
    for ep in range(epochs):
        rng.shuffle(data)
        step = lr / (1 + ep)
        for feats, target, wt in data:
            rows = [w.setdefault(f, [0.0] * k) for f in feats]
            p = softmax([sum(r[j] for r in rows) for j in range(k)])
            g = [(p[j] - target[j]) * wt * step for j in range(k)]
            for r in rows:
                for j in range(k):
                    r[j] -= g[j] + step * l2 * r[j]
    return w


def train_encoder(cfg: Config, task: str, days: int = DAYS, *, src: Source | None = None,
                  today: date | None = None) -> dict:
    """Fit the encoder on half the calibration-day gold (in_encoder_train_split, GOLD_WEIGHT) and
    silver (teachers averaged), never on a held-out gold unit nor on the gold eval calibrates it with;
    report accuracy and macro-F1 on the held-out gold only."""
    labels = [c for c, _d in TASKS[task].candidates]
    reqs = window_units(cfg, task, days, src, today)
    gold, silver = gold_latest(task), silver_by_unit(task)
    test = {u for u in gold if u in reqs and held_out(reqs[u])}
    data = []
    for u, g in gold.items():
        if u in reqs and u not in test and in_encoder_train_split(reqs[u]):
            data.append((features(reqs[u].state), [float(x == g) for x in labels], GOLD_WEIGHT))
    for u, ts in silver.items():
        if u in reqs and u not in test and u not in gold and ts:
            avg = [sum(d.get(x, 0.0) for d in ts.values()) / len(ts) for x in labels]
            data.append((features(reqs[u].state), avg, 1.0))
    if not data:
        raise AutError(f"label train-encoder: no gold or silver labels for {task} yet")
    w = _fit(data, len(labels))
    p = encoder_path(task)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"task": task, "task_ver": TASKS[task].ver, "labels": labels,
                             "w": {str(f): [round(x, 5) for x in r] for f, r in w.items()}}))
    enc = Encoder(task)
    pairs = [(top_label(enc.distribution(reqs[u])), gold[u]) for u in sorted(test)]
    f1s = []
    for lab in labels:
        tp = sum(a == lab == b for a, b in pairs)
        pred, true = sum(a == lab for a, _ in pairs), sum(b == lab for _, b in pairs)
        if pred or true:
            f1s.append(2 * tp / (pred + true))
    return {"model_id": enc.model_id, "model_rev": enc.model_rev, "trained_on": len(data),
            "held_out": len(pairs),
            "accuracy": _round4(sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
            "macro_f1": _round4(sum(f1s) / len(f1s)) if f1s else None}


def print_eval(task: str, records: list[dict]) -> None:
    for r in records:
        print(f"label eval {task} {r['model_id']}: macro-F1 {r['macro_f1']}, kappa "
              f"{r['kappa']}, ECE {r['ece_raw']} -> {r['ece_cal']}, coverage "
              f"{r['coverage']}, n {r['n']} (held out {r['n_held']}), "
              f"{r['s_per_decision']} s per decision")


def print_choose(task: str, c: dict) -> None:
    print(f"label choose {task}: {c['backend']} {c['model_id']} (T "
          f"{c['temperature']}, tau {json.dumps(c['tau'])})")


def print_train_encoder(task: str, r: dict) -> None:
    print(f"label train-encoder {task}: {json.dumps(r)}")
