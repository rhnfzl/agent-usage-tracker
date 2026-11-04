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
import os
import random
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone

from ..config import Config
from ..event import ActivityEvent, make_event_id
from ..push import send_activity_rows
from .backends import MODELS, Encoder, encoder_path, features, make_backend
from .core import Choice, choice_path, fit_tau, fit_temperature, softmax, temper
from .evalset import (DAYS, append_jsonl, encoder_fit, gold_latest, held_out, path, read_jsonl,
                      silver_by_unit, stratum, window_units)
from .tasks import TASKS, Source

SHARE_ERROR_PP = 5.0         # map.md 4.5: a label's share on known items may be off this much
GOLD_WEIGHT = 3.0            # train-encoder: one gold answer counts as three silver ones


def _r(x: float | None) -> float | None:
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


def evaluate(task: str, backend, items: list[tuple], precision: float,
             today: str) -> dict:
    """items: (request, gold, held_out, weight). The eval record of one backend (numbers)."""
    ver, labels = TASKS[task].ver, [c for c, _d in TASKS[task].candidates]
    t0 = time.monotonic()
    raws = [backend.distribution(r) for r, *_ in items]
    secs = (time.monotonic() - t0) / max(len(items), 1)
    enc = isinstance(backend, Encoder)      # its training half never calibrates it
    cal = [i for i, it in enumerate(items) if not it[2] and not (enc and encoder_fit(it[0]))]
    held = [i for i, it in enumerate(items) if it[2]]
    t = fit_temperature([raws[i] for i in cal], [items[i][1] for i in cal])
    dists = [temper(d, t) for d in raws]
    top = [max(d, key=d.get) for d in dists]
    tau = fit_tau([(top[i], dists[i][top[i]], items[i][1]) for i in cal], precision)
    rep = held or cal        # with too few days for a split, report on what there is (n_held 0)
    gold = {i: items[i][1] for i in rep}
    w = {i: items[i][3] for i in rep}
    passed = [i for i in rep if tau.get(top[i]) is not None and dists[i][top[i]] >= tau[top[i]]]
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
        per[lab] = {"n": true, "precision": _r(p), "recall": _r(r),
                    "f1": _r(2 * p * r / (p + r)) if p and r else (0.0 if pred or true else None),
                    "tau": _r(tau.get(lab)), "coverage": _r(len(ok) / pred) if pred else None,
                    "precision_at_tau": _r(hits / len(ok)) if ok else None,
                    "precision_lb": _r(wilson_lb(hits, len(ok))),
                    "share_error_pp": _r(abs(sum(w[i] for i in passed if top[i] == lab) / wk
                                             - sum(w[i] for i in rep if gold[i] == lab) / wa)
                                         * 100) if wk else None}
    f1s = [v["f1"] for v in per.values() if v["f1"] is not None]
    return {"task": task, "task_ver": ver, "backend": "encoder" if isinstance(backend, Encoder)
            else "mlx_letter", "model_id": backend.model_id, "model_rev": backend.model_rev,
            "size_mb": getattr(backend, "size_mb", None), "prompt_ver": items[0][0].prompt_ver,
            "calib_ver": f"{task}@{ver}/{today}", "temperature": t, "n": len(items),
            "n_cal": len(cal), "n_held": len(held),
            "macro_f1": _r(sum(f1s) / len(f1s)) if f1s else None,
            "kappa": _r(kappa([(top[i], gold[i]) for i in rep])),
            "ece_raw": _r(ece([(max(raws[i].values()), max(raws[i], key=raws[i].get) == gold[i])
                               for i in rep])),
            "ece_cal": _r(ece([(dists[i][top[i]], top[i] == gold[i]) for i in rep])),
            "coverage": _r(len(passed) / len(rep)) if rep else None,
            "s_per_decision": _r(secs), "per_label": per}


def gold_items(cfg: Config, task: str, days: int, src: Source | None) -> tuple[list[tuple], int]:
    """(request, gold, held_out, weight) for every gold unit still in the window; and how many
    gold units fell out of it."""
    gold = gold_latest(task)
    reqs = window_units(cfg, task, days, src)
    found = [u for u in gold if u in reqs]
    w = weights(task, found)
    return [(reqs[u], gold[u], held_out(reqs[u]), w[u]) for u in found], len(gold) - len(found)


def default_backends(cfg: Config, task: str) -> list:
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


def run_eval(cfg: Config, task: str, days: int = DAYS, src: Source | None = None,
             backends: list | None = None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    items, gone = gold_items(cfg, task, days, src)
    if not items:
        raise SystemExit(f"label eval: no gold labels for {task} in the last {days} days "
                         "(label some with `aut label review`)")
    if gone:
        print(f"aut: label eval: {gone} gold units are outside the {days}-day window; left out",
              file=sys.stderr)
    recs = []
    for b in backends if backends is not None else default_backends(cfg, task):
        rec = evaluate(task, b, items, cfg.label_precision, now.astimezone().date().isoformat())
        append_jsonl(path(task, "eval"), {"ts": now.isoformat(), **rec})
        if cfg.has_pg:
            send_activity_rows(cfg.pg_dsn, [ActivityEvent(
                event_id=make_event_id("eval", json.dumps(rec, sort_keys=True)),
                ts=now.isoformat(), agent="label", kind="eval", session_id=None, turn_id=None,
                attrs=rec, machine=cfg.machine)])
        recs.append(rec)
    return recs


def passes(rec: dict, coverage: float, precision: float) -> bool:
    """The bar (map.md 4): a held-out report, coverage at tau at least label_coverage, every
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
    for r in read_jsonl(path(task, "eval")):
        if r.get("task_ver") == TASKS[task].ver:
            newest[r["model_id"]] = r                   # the file is in time order
    ok = [r for r in newest.values() if passes(r, cfg.label_coverage, cfg.label_precision)]
    if not ok:
        why = {m: {"coverage": r["coverage"], "n_held": r["n_held"]} for m, r in newest.items()}
        raise SystemExit(f"label choose: no backend passes the bar for {task}: {json.dumps(why)}")
    win = min(ok, key=lambda r: (r["size_mb"] if r["size_mb"] is not None else math.inf,
                                 -(r["macro_f1"] or 0)))
    p = choice_path()
    try:
        obj = json.loads(p.read_text())
        obj = obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        obj = {}
    obj.setdefault("tasks", {})[task] = {
        "backend": win["backend"], "model_id": win["model_id"], "model_rev": win["model_rev"],
        "temperature": win["temperature"], "calib_ver": win["calib_ver"],
        "tau": {k: v["tau"] for k, v in win["per_label"].items() if v["tau"] is not None}}
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
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


def train_encoder(cfg: Config, task: str, days: int = DAYS, src: Source | None = None) -> dict:
    """Fit the encoder on half the calibration-day gold (encoder_fit, GOLD_WEIGHT) and silver
    (teachers averaged), never on a held-out gold unit nor on the gold eval calibrates it with;
    report accuracy and macro-F1 on the held-out gold only."""
    labels = [c for c, _d in TASKS[task].candidates]
    reqs = window_units(cfg, task, days, src)
    gold, silver = gold_latest(task), silver_by_unit(task)
    test = {u for u in gold if u in reqs and held_out(reqs[u])}
    data = []
    for u, g in gold.items():
        if u in reqs and u not in test and encoder_fit(reqs[u]):
            data.append((features(reqs[u].state), [float(x == g) for x in labels], GOLD_WEIGHT))
    for u, ts in silver.items():
        if u in reqs and u not in test and u not in gold and ts:
            avg = [sum(d.get(x, 0.0) for d in ts.values()) / len(ts) for x in labels]
            data.append((features(reqs[u].state), avg, 1.0))
    if not data:
        raise SystemExit(f"label train-encoder: no gold or silver labels for {task} yet")
    w = _fit(data, len(labels))
    p = encoder_path(task)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"task": task, "task_ver": TASKS[task].ver, "labels": labels,
                             "w": {str(f): [round(x, 5) for x in r] for f, r in w.items()}}))
    enc = Encoder(task)
    pairs = [(max(d, key=d.get), gold[u]) for u in sorted(test)
             for d in [enc.distribution(reqs[u])]]
    f1s = []
    for lab in labels:
        tp = sum(a == lab == b for a, b in pairs)
        pred, true = sum(a == lab for a, _ in pairs), sum(b == lab for _, b in pairs)
        if pred or true:
            f1s.append(2 * tp / (pred + true))
    return {"model_id": enc.model_id, "model_rev": enc.model_rev, "trained_on": len(data),
            "held_out": len(pairs),
            "accuracy": _r(sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
            "macro_f1": _r(sum(f1s) / len(f1s)) if f1s else None}
