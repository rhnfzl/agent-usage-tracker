"""`aut label eval`, `aut label choose` and `aut label train-encoder`: the bake-off on gold.

Raw scores are saved per task, model, revision and prompt, so a rerun calls the model only for new
gold units and tuning needs no model. Calibration and the per-label tau fit on four of five folds
(evalset.fold: same for every model, grouped by day) and report on the fifth; the saved calibration
and tau then fit on all five. A floor on precision and on recall per label is never relaxed: a
model that misses one is marked as failing. A stable part of the days (evalset.final_test) stays
out of all of this; only `aut label eval --final --model M` scores it, into <task>.final.jsonl.

Per backend, one eval record of numbers (and model ids) is appended to <task>.eval.jsonl and sent
as an activity_event of kind `eval`. `aut label eval` prints one table per task (quality, speed,
memory, with Pareto and failure marks); the owner picks with `aut label choose --task T --model M`,
which writes that model's saved result into label.json, which the nightly run reads.
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Iterable, NamedTuple, Sequence

from ..config import LABEL_DAYS, AutError, Config
from ..event import ActivityEvent, make_event_id
from ..push import send_activity_rows
from ..state import atomic_write_text
from .backends import (MODELS, BackendUnavailable, Encoder, encoder_path, features, find_model,
                       make_backend, short_name)
from .core import (Backend, Choice, Request, calibrate, choice_path, fit_platt, fit_tau,
                   fit_temperature, read_choices, softmax, top_label)
from .evalset import (FLOORS, FOLDS, append_jsonl, eval_path, final_test, fold, gold_latest,
                      in_encoder_train_split, read_jsonl, scores_path, silver_by_unit, stratum,
                      window_units)
from .tasks import TASKS, Source

SHARE_ERROR_PP = 5.0         # a label's share on known items may be off this much (CONTRACT.md)
GOLD_WEIGHT = 3.0            # train-encoder: one gold answer counts as three silver ones
Dist = dict[str, float]


class _Item(NamedTuple):
    """One gold unit of a bake-off."""
    req: Request
    gold: str
    weight: float


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
    by: dict[int, list[Any]] = {}
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


def brier(rows: list[tuple[Dist, str]]) -> float:
    """Mean over (distribution, gold) of the squared error summed over candidates: 0 to 2."""
    return sum(sum((p - (k == g)) ** 2 for k, p in d.items()) for d, g in rows) / len(rows)


def nll(rows: list[tuple[Dist, str]]) -> float:
    return -sum(math.log(max(d[g], 1e-12)) for d, g in rows) / len(rows)


def reliability(tops: list[tuple[float, bool]], size: int = 20) -> list[dict[str, Any]]:
    """(top probability, was right), lowest first, in groups of about `size`: mean p, share
    right."""
    tops = sorted(tops)
    groups = [tops[i:i + size] for i in range(0, len(tops), size)]
    if len(groups) > 1 and len(groups[-1]) < size / 2:
        groups[-2:] = [groups[-2] + groups[-1]]
    return [{"n": len(g), "p": _round4(sum(p for p, _ in g) / len(g)),
             "right": _round4(sum(h for _, h in g) / len(g))} for g in groups]


def _decide(d: Dist, tau: dict[str, Any]) -> str | None:
    """The top answer when it reaches its label's tau, as core.decide; None is unknown."""
    top = top_label(d)
    return top if tau.get(top) is not None and d[top] >= tau[top] else None


def _label_metrics(lab: str, rep: list[int], top: list[str], gold: dict[int, str],
                   passed: list[int], w: dict[int, float],
                   tau: float | None) -> dict[str, Any]:
    """One label's row of the eval record, over the reported items `rep`; `passed`: those at or
    above their tau; w: each item's weight. tau stays exact: rounded up, it would refuse the very
    units at the cut."""
    tp = sum(top[i] == lab == gold[i] for i in rep)
    pred = sum(top[i] == lab for i in rep)
    true = sum(gold[i] == lab for i in rep)
    p = tp / pred if pred else None
    r = tp / true if true else None
    ok = [i for i in passed if top[i] == lab]
    hits = sum(gold[i] == lab for i in ok)
    wk, wa = sum(w[i] for i in passed), sum(w.values())
    return {"n": true, "precision": _round4(p), "recall": _round4(r),
            "f1": _round4(2 * p * r / (p + r)) if p and r else (0.0 if pred or true else None),
            "tau": tau, "coverage": _round4(len(ok) / pred) if pred else None,
            "precision_at_tau": _round4(hits / len(ok)) if ok else None,
            "precision_lb": _round4(wilson_lb(hits, len(ok))),
            "share_error_pp": _round4(abs(sum(w[i] for i in ok) / wk
                                          - sum(w[i] for i in rep if gold[i] == lab) / wa)
                                      * 100) if wk else None}


def _per_label(preds: Sequence[str | None], golds: list[str],
               labels: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for lab in labels:
        tp = sum(p == lab == g for p, g in zip(preds, golds))
        pred, true = sum(p == lab for p in preds), sum(g == lab for g in golds)
        out[lab] = {"n": true, "precision": _round4(tp / pred) if pred else None,
                    "recall": _round4(tp / true) if true else None,
                    "f1": _round4(2 * tp / (pred + true)) if pred or true else None}
    return out


def _macro(per: dict[str, dict[str, Any]]) -> float | None:
    f1s = [v["f1"] for v in per.values() if v["f1"] is not None]
    return _round4(sum(f1s) / len(f1s)) if f1s else None


def _report(raws: list[Dist], dists: list[Dist], preds: Sequence[str | None], golds: list[str],
            labels: list[str]) -> dict[str, Any]:
    """The numbers of one report: at the tuned tau, without a threshold, and off the shelf (raw
    scores at the default threshold of 0.5)."""
    tops = [top_label(d) for d in dists]
    plain, tuned = _per_label(tops, golds, labels), _per_label(preds, golds, labels)
    half = dict.fromkeys(labels, 0.5)
    default = _per_label([_decide(d, half) for d in raws], golds, labels)
    rows = list(zip(dists, golds))
    return {"n": len(golds), "macro_f1": _macro(plain), "macro_f1_tuned": _macro(tuned),
            "macro_f1_default": _macro(default), "kappa": _round4(kappa(list(zip(tops, golds)))),
            "brier": _round4(brier(rows)), "nll": _round4(nll(rows)),
            "coverage": _round4(sum(p is not None for p in preds) / len(preds)),
            "per_label": {lab: {**plain[lab], "precision_at_tau": tuned[lab]["precision"],
                                "recall_at_tau": tuned[lab]["recall"],
                                "f1_at_tau": tuned[lab]["f1"],
                                "precision_default": default[lab]["precision"],
                                "recall_default": default[lab]["recall"]} for lab in labels}}


def _score(backend: Backend, reqs: Sequence[Request],
           prev: dict[str, Any] | None = None) -> tuple[dict[str, Dist], dict[str, Any]]:
    """(unit_id -> raw distribution, speed): saved scores, and the model only for units without
    one. model_rev must be final by now, so run its prepare() first. The first call is the warm-up
    (load plus one decision), timed apart; when this run timed no more calls than `prev` did,
    prev's speed stands. shortcut: a unit whose state changed after it was scored (an episode's
    req.rev) keeps its saved score; upgrade by keying rows on rev."""
    p = scores_path(reqs[0].task, backend.model_id, backend.model_rev, reqs[0].prompt_ver,
                    getattr(backend, "protocol", ""))
    saved = {r["unit_id"]: r["dist"] for r in read_jsonl(p)}
    times = []
    for r in reqs:
        if r.unit_id not in saved:
            t0 = time.monotonic()
            saved[r.unit_id] = backend.distribution(r)
            times.append(time.monotonic() - t0)
            append_jsonl(p, {"unit_id": r.unit_id, "dist": saved[r.unit_id]})
    warm = times[1:]
    if prev and prev.get("n_timed", 0) >= len(warm):
        return saved, {k: prev.get(k) for k in ("s_per_decision", "load_s", "peak_mb", "n_timed")}
    peak = getattr(backend, "peak_memory_mb", None)
    return saved, {"s_per_decision": _round4(sum(warm) / len(warm)) if warm else None,
                   "load_s": _round4(times[0]) if times else None, "n_timed": len(warm),
                   "peak_mb": peak() if peak and times else None}


def _fit_calibration(kind: str, raws: list[Dist], golds: list[str], yes: str) -> dict[str, Any]:
    """Keyword arguments of core.calibrate for one way to calibrate, fitted on these units."""
    if kind == "temperature":
        return {"temperature": fit_temperature(raws, golds), "platt": None}
    return {"temperature": 1.0, "platt": fit_platt(raws, golds, yes) if kind == "platt" else None}


def evaluate(task: str, backend: Backend, items: Sequence[_Item], precision: float, recall: float,
             today: date, prev: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Items never come from a final test day. The eval record of one backend (numbers), out of
    fold; None when nothing is left to report on."""
    labels = [c for c, _d in TASKS[task].candidates]
    # the encoder's training days never calibrate it
    items = [it for it in items
             if not (isinstance(backend, Encoder) and in_encoder_train_split(it.req))]
    if not items:
        return None
    reqs = [it.req for it in items]
    golds = [it.gold for it in items]
    w = [it.weight for it in items]
    saved, speed = _score(backend, reqs, prev)
    raws = [{lab: saved[r.unit_id].get(lab, 0.0) for lab in labels} for r in reqs]
    folds = [fold(r) for r in reqs]
    every = range(len(reqs))
    split = [([i for i in every if folds[i] != k], [i for i in every if folds[i] == k])
             for k in range(FOLDS)]

    def fit(kind: str, on: Iterable[int]) -> dict[str, Any]:
        on = list(on)
        return _fit_calibration(kind, [raws[i] for i in on], [golds[i] for i in on], labels[0])

    def out_of_fold(kind: str) -> list[Dist]:
        out: list[Dist] = [{}] * len(reqs)
        for train, test in split:
            c = fit(kind, train)
            for i in test:
                out[i] = calibrate(raws[i], **c)
        return out

    # T = 1 unless a fit lowers the out-of-fold Brier. shortcut: the way to calibrate is picked on
    # the folds it reports on (one pick of at most three), upgrade by nesting it if gold grows.
    kinds = ("none", "temperature") + (("platt",) if TASKS[task].decision == "boolean" else ())
    briers = {k: _round4(brier(list(zip(out_of_fold(k), golds)))) or 0.0 for k in kinds}
    kind = min(kinds, key=briers.__getitem__)
    dists = out_of_fold(kind)
    preds: list[str | None] = [None] * len(reqs)
    no_threshold: dict[str, list[str]] = {}
    for k, (train, test) in enumerate(split):
        c = fit(kind, train)
        tau = fit_tau([(top_label(d), d[top_label(d)], golds[i]) for i in train
                       for d in [calibrate(raws[i], **c)]], precision, recall=recall)
        missing = sorted(lab for lab in set(golds) if tau.get(lab) is None)
        if test and missing:
            no_threshold[str(k)] = missing
        for i in test:
            preds[i] = _decide(dists[i], tau)
    saved_cal = fit(kind, every)
    full = [calibrate(r, **saved_cal) for r in raws]
    tau = fit_tau([(top_label(d), d[top_label(d)], g) for d, g in zip(full, golds)], precision,
                  recall=recall)
    rep = _report(raws, dists, preds, golds, labels)
    tops = [top_label(d) for d in dists]
    passed = [i for i in every if preds[i]]
    for lab, v in rep["per_label"].items():
        v.update(_label_metrics(lab, list(every), tops, dict(enumerate(golds)), passed,
                                dict(enumerate(w)), tau.get(lab)))
    ver = TASKS[task].ver
    return {"task": task, "task_ver": ver, "backend": backend.backend,
            "model_id": backend.model_id, "model_rev": backend.model_rev,
            "size_mb": backend.size_mb, "prompt_ver": reqs[0].prompt_ver,
            "calib_ver": f"{task}@{ver}/{today.isoformat()}", "calibration": kind, **saved_cal,
            "brier_by_calibration": briers, "n_cal": len(reqs), "n_held": len(reqs),
            "folds": {str(k): len(test) for k, (_t, test) in enumerate(split)},
            "no_threshold_folds": no_threshold, **rep, **speed,
            "ece_raw": _round4(ece([(max(r.values()), top_label(r) == g)
                                    for r, g in zip(raws, golds)])),
            "ece_cal": _round4(ece([(max(d.values()), top_label(d) == g)
                                    for d, g in zip(dists, golds)])),
            "reliability_raw": reliability([(max(r.values()), top_label(r) == g)
                                            for r, g in zip(raws, golds)]),
            "reliability_cal": reliability([(max(d.values()), top_label(d) == g)
                                            for d, g in zip(dists, golds)])}


def _gold_units(cfg: Config, task: str, days: int, src: Source | None,
                today: date | None = None) -> list[tuple[Request, str]]:
    """(request, gold) for every gold unit still in the window, final test days included: each
    caller takes its side of evalset.final_test."""
    gold = gold_latest(task)
    reqs = window_units(cfg, task, days, src, today)
    found = [u for u in gold if u in reqs]
    if len(found) < len(gold):
        print(f"aut: label eval: {len(gold) - len(found)} gold units are outside the {days}-day "
              "window; left out", file=sys.stderr)
    return [(reqs[u], gold[u]) for u in found]


def _warn_thin(task: str, golds: list[str], what: str) -> None:
    floor = FLOORS[task][0]
    n = Counter(golds)
    thin = {c: n[c] for c, _d in TASKS[task].candidates if n[c] < floor}
    if thin:
        print(f"aut: label eval: {what} is too small to mean much: under {floor} gold per label "
              f"for {json.dumps(thin)}", file=sys.stderr)


def default_backends(cfg: Config, task: str) -> list[Backend]:
    """Every model in the table (and label_model) that is fully on disk, plus the encoder once
    trained. The others are printed with what it takes to fetch them."""
    out: list[Backend] = []
    for name in dict.fromkeys(list(MODELS) + [short_name(cfg.label_model)]):
        b = make_backend(task, Choice(model_id=name), cfg.label_model, cfg.label_llama_server)
        try:
            getattr(b, "prepare", lambda: None)()
        except BackendUnavailable as exc:
            print(f"aut: label eval: {name} left out: {exc}", file=sys.stderr)
            continue
        out.append(b)
    if encoder_path(task).exists():
        out.append(Encoder(task))
    return out


def _key(rec: dict[str, Any]) -> tuple[Any, ...]:
    return rec.get("model_id"), rec.get("model_rev"), rec.get("prompt_ver")


def newest(task: str) -> dict[tuple[Any, ...], dict[str, Any]]:
    """The newest record per model, revision and prompt of the current task version, the most
    recent one last. Records from before the five folds have no Brier and do not count."""
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in read_jsonl(eval_path(task, "eval")):
        if r.get("task_ver") == TASKS[task].ver and "brier" in r:
            out.pop(_key(r), None)
            out[_key(r)] = r
    return out


def run_eval(cfg: Config, task: str, days: int = LABEL_DAYS, *, src: Source | None = None,
             backends: list[Backend] | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    pool = [(r, g) for r, g in _gold_units(cfg, task, days, src, today) if not final_test(r)]
    if not pool:
        raise AutError(f"label eval: no gold labels for {task} in the last {days} days outside "
                       "the final test days (label some with `aut label review`)")
    _warn_thin(task, [g for _q, g in pool], "the tuning gold")
    w = weights(task, [r.unit_id for r, _g in pool])
    items = [_Item(r, g, w[r.unit_id]) for r, g in pool]
    before, recs = newest(task), []
    for b in backends if backends is not None else default_backends(cfg, task):
        try:
            getattr(b, "prepare", lambda: None)()
            rec = evaluate(task, b, items, cfg.label_precision, cfg.label_recall, today,
                           before.get((b.model_id, b.model_rev, items[0].req.prompt_ver)))
        except BackendUnavailable as exc:   # one model that cannot run here: the others still run
            print(f"aut: label eval: {b.model_id} left out: {exc}", file=sys.stderr)
            continue
        finally:
            getattr(b, "close", lambda: None)()
        if rec is None:
            print(f"aut: label eval: {b.model_id} has no gold outside its training days; left out",
                  file=sys.stderr)
            continue
        append_jsonl(eval_path(task, "eval"), {"ts": now.isoformat(), **rec})
        if cfg.pg_dsn:
            send_activity_rows(cfg.pg_dsn, [ActivityEvent(
                event_id=make_event_id("eval", json.dumps(rec, sort_keys=True)),
                ts=now.isoformat(), agent="label", kind="eval", session_id=None, turn_id=None,
                attrs=rec, machine=cfg.machine)])
        recs.append(rec)
    return recs


def run_final(cfg: Config, task: str, model: str, days: int = LABEL_DAYS, *,
              src: Source | None = None, backend: Backend | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """Score one model's saved calibration and tau on the final test days. Nothing fits here, and
    only this function reads those days; the result goes to its own file, which nothing reads."""
    now = now or datetime.now(timezone.utc)
    rec = _saved_result(task, model)
    pairs = [(r, g) for r, g in _gold_units(cfg, task, days, src, now.astimezone().date())
             if final_test(r)]
    if not pairs:
        raise AutError(f"label eval --final: no gold labels for {task} on the final test days "
                       "yet (label more days with `aut label review`)")
    reqs, golds = [r for r, _g in pairs], [g for _r, g in pairs]
    if rec["prompt_ver"] != reqs[0].prompt_ver:
        raise AutError(f"label eval --final: {model} was tuned on prompt {rec['prompt_ver']}; "
                       "run `aut label eval` again first")
    _warn_thin(task, golds, "the final test set")
    out = eval_path(task, "final")
    looks = sum(r.get("model_id") == rec["model_id"] for r in read_jsonl(out))
    if looks:
        print(f"aut: label eval: {rec['model_id']} has seen the final test set {looks} times; "
              "if you changed anything because of it, it no longer tests that", file=sys.stderr)
    b = backend or make_backend(task, Choice(backend=rec["backend"], model_id=rec["model_id"],
                                             model_rev=rec["model_rev"]), cfg.label_model,
                                cfg.label_llama_server)
    try:
        getattr(b, "prepare", lambda: None)()
        saved, _speed = _score(b, reqs)
    finally:
        getattr(b, "close", lambda: None)()
    labels = [c for c, _d in TASKS[task].candidates]
    raws = [{lab: saved[r.unit_id].get(lab, 0.0) for lab in labels} for r in reqs]
    tau = {k: v["tau"] for k, v in rec["per_label"].items()}
    dists = [calibrate(r, rec["temperature"], rec.get("platt")) for r in raws]
    res = {"ts": now.isoformat(), "task": task, "task_ver": rec["task_ver"],
           "backend": rec["backend"], "model_id": rec["model_id"], "model_rev": rec["model_rev"],
           "prompt_ver": rec["prompt_ver"], "calib_ver": rec["calib_ver"],
           **_report(raws, dists, [_decide(d, tau) for d in dists], golds, labels)}
    append_jsonl(out, res)
    res["fails"] = failures(res, cfg.label_precision, cfg.label_recall)
    return res


def final_line(r: dict[str, Any]) -> str:
    fails = "; ".join(r["fails"]) or "none"
    return (f"label eval --final {r['task']} {r['model_id']}: n {r['n']}, macro-F1 tuned "
            f"{r['macro_f1_tuned']} (default 0.5: {r['macro_f1_default']}), Brier {r['brier']}, "
            f"NLL {r['nll']}, coverage {r['coverage']}, floors missed: {fails}")


def failures(rec: dict[str, Any], precision: float, recall: float,
             coverage: float | None = None) -> list[str]:
    """Why a report misses the bar: a label under the precision or recall floor at its tau (a
    label with no gold has no recall to judge), and, when coverage is given, the rest of the bar
    (CONTRACT.md, the bake-off): coverage at tau, and no label's share on known items off by over
    SHARE_ERROR_PP."""
    out = []
    for lab, v in rec["per_label"].items():
        if v["precision_at_tau"] is not None and v["precision_at_tau"] < precision:
            out.append(f"{lab} precision {v['precision_at_tau']}")
        if v["recall_at_tau"] is not None and v["recall_at_tau"] < recall:
            out.append(f"{lab} recall {v['recall_at_tau']}")
        if coverage is not None and (v["share_error_pp"] or 0) > SHARE_ERROR_PP:
            out.append(f"{lab} share error {v['share_error_pp']}")
    if coverage is not None and (rec["coverage"] or 0) < coverage:
        out.append(f"coverage {rec['coverage']}")
    return out


def _fails(rec: dict[str, Any], cfg: Config) -> list[str]:
    return failures(rec, cfg.label_precision, cfg.label_recall, cfg.label_coverage)


def pareto(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows no other row beats on quality (tuned macro-F1), speed and memory. An axis a row
    does not know (null) is not compared."""
    def axes(r: dict[str, Any]) -> tuple[Any, ...]:
        return -(r.get("macro_f1_tuned") or 0), r.get("s_per_decision"), r.get("peak_mb")

    def beats(a: dict[str, Any], b: dict[str, Any]) -> bool:
        pairs = [(x, y) for x, y in zip(axes(a), axes(b)) if x is not None and y is not None]
        return all(x <= y for x, y in pairs) and any(x < y for x, y in pairs)
    return [r for r in rows if not any(beats(o, r) for o in rows)]


def recommend(rows: list[dict[str, Any]], cfg: Config) -> dict[str, Any] | None:
    """Of the frontier of models that pass: the best quality (to two decimals: closer is noise on
    gold this size), then the fastest."""
    front = pareto([r for r in rows if not _fails(r, cfg)])
    return min(front, key=lambda r: (-round(r.get("macro_f1_tuned") or 0, 2),
                                     r.get("s_per_decision") or math.inf,
                                     r.get("peak_mb") or math.inf), default=None)


def table(cfg: Config, task: str) -> list[str]:
    """Quality, speed and memory per model. `*` on the Pareto frontier of the models that pass,
    `!` fails a floor or the bar. Numbers are out of fold; none comes from the final test days."""
    rows = sorted(newest(task).values(), key=lambda r: -(r.get("macro_f1_tuned") or 0))
    front = pareto([r for r in rows if not _fails(r, cfg)])
    lines = [f"label eval {task}: floors precision {cfg.label_precision}, recall "
             f"{cfg.label_recall}, coverage {cfg.label_coverage}",
             f"   {'model':<46} {'rev':<8} {'F1':>6} {'F1@.5':>6} {'Brier':>6} {'NLL':>6} "
             f"{'calib':<11} {'s/dec':>7} {'load s':>7} {'peak MB':>7} {'n':>5}"]
    for r in rows:
        mark = ("*" if any(r is o for o in front) else " ") + ("!" if _fails(r, cfg) else " ")
        lines.append(f"{mark} {short_name(r['model_id']):<46} {r['model_rev'][:8]:<8} "
                     f"{r['macro_f1_tuned']!s:>6} {r['macro_f1_default']!s:>6} {r['brier']!s:>6} "
                     f"{r['nll']!s:>6} {r['calibration']:<11} {r.get('s_per_decision')!s:>7} "
                     f"{r.get('load_s')!s:>7} {r.get('peak_mb')!s:>7} {r['n']:>5}")
    return lines + ["* Pareto frontier of the models that pass   ! fails: "
                    + "; ".join(f"{short_name(r['model_id'])}: {', '.join(_fails(r, cfg))}"
                                for r in rows if _fails(r, cfg))]


def details(recs: list[dict[str, Any]]) -> list[str]:
    """Per label and reliability of the models just evaluated."""
    def rel(rows: list[dict[str, Any]]) -> str:
        return " ".join(f"{g['p']}:{g['right']}" for g in rows)

    out = []
    for r in recs:
        out += ["", f"{r['model_id']}: {r['calibration']} (T {r['temperature']}, platt "
                    f"{json.dumps(r['platt'])}), Brier {r['brier_by_calibration']}, folds "
                    f"{r['folds']}, no threshold in folds {r['no_threshold_folds'] or 'none'}",
                f"  {'label':<16} {'n':>4} {'tau':>6} {'P':>6} {'R':>6}   "
                f"at 0.5: {'P':>6} {'R':>6}"]
        out += [f"  {lab:<16} {v['n']:>4} {_round4(v['tau'])!s:>6} {v['precision_at_tau']!s:>6} "
                f"{v['recall_at_tau']!s:>6}           {v['precision_default']!s:>6} "
                f"{v['recall_default']!s:>6}" for lab, v in r["per_label"].items()]
        out += [f"  reliability raw (mean p:share right) {rel(r['reliability_raw'])}",
                f"  reliability calibrated               {rel(r['reliability_cal'])}"]
    return out


def _saved_result(task: str, model: str) -> dict[str, Any]:
    """The newest record of a model id (or a short name from backends.MODELS)."""
    known = find_model(model)
    repo = known.repo if known else model
    match = [r for r in newest(task).values() if r["model_id"] == repo]
    if not match:
        raise AutError(f"label: no eval record of {model} for {task}; run `aut label eval` "
                       f"first. Known: {sorted({r['model_id'] for r in newest(task).values()})}")
    return match[-1]


def choose(cfg: Config, task: str, model: str | None = None) -> dict[str, Any] | None:
    """With a model: write its saved result into label.json; other tasks untouched. Without one:
    print the table and a recommendation, and write nothing."""
    if model is None:
        rows = list(newest(task).values())
        if not rows:
            raise AutError(f"label choose: no eval record for {task}; run `aut label eval` first")
        rec = recommend(rows, cfg)
        print("\n".join(table(cfg, task)))
        print(f"recommended: aut label choose --task {task} --model "
              f"{short_name(rec['model_id'])}" if rec else
              "recommended: none, no model passes the floors. They stay as they are: label more "
              "gold, or try other models.")
        return None
    win = _saved_result(task, model)
    fails = _fails(win, cfg)
    if fails:
        print(f"aut: warning: {win['model_id']} fails the floors for {task}: {', '.join(fails)}. "
              "Labels without a tau stay unknown.", file=sys.stderr)
    p = choice_path()
    try:
        obj = read_choices()
    except (OSError, ValueError) as exc:    # overwriting would lose every other task's choice
        raise AutError(f"label choose: {p} is unusable ({exc}); nothing written. Fix or move "
                       "it aside and run choose again for each task.") from None
    obj.setdefault("tasks", {})[task] = {
        "backend": win["backend"], "model_id": win["model_id"], "model_rev": win["model_rev"],
        "temperature": win["temperature"], "platt": win.get("platt"),
        "calib_ver": win["calib_ver"], "prompt_ver": win["prompt_ver"],
        "tau": {k: v["tau"] for k, v in win["per_label"].items() if v["tau"] is not None}}
    atomic_write_text(p, json.dumps(obj, indent=2, sort_keys=True))
    return obj["tasks"][task]


MIN_UPDATES = 2000


def _fit(data: list[tuple[list[int], list[float], float]], k: int, epochs: int = 12,
         lr: float = 0.3, l2: float = 1e-4, seed: int = 0) -> dict[int, list[float]]:
    """Multinomial logistic regression by SGD on (features, target distribution, weight)."""
    w: dict[int, list[float]] = {}
    rng, data = random.Random(seed), list(data)  # nosec B311 # SGD shuffle order, not security
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


def _training_data(gold: dict[str, str], silver: dict[str, dict[str, Any]], train: dict[str, Request],
                   labels: list[str]) -> list[tuple[list[int], list[float], float]]:
    """(features, target distribution, weight) of each gold and silver unit of the training days."""
    data = []
    for u, g in gold.items():
        if u in train:
            data.append((features(train[u].facts), [float(x == g) for x in labels], GOLD_WEIGHT))
    for u, ts in silver.items():
        if u in train and u not in gold and ts:
            avg = [sum(d.get(x, 0.0) for d in ts.values()) / len(ts) for x in labels]
            data.append((features(train[u].facts), avg, 1.0))
    return data


def _macro_f1(pairs: list[tuple[str, str]], labels: list[str]) -> float | None:
    f1s = []
    for lab in labels:
        tp = sum(a == lab == b for a, b in pairs)
        pred, true = sum(a == lab for a, _ in pairs), sum(b == lab for _, b in pairs)
        if pred or true:
            f1s.append(2 * tp / (pred + true))
    return _round4(sum(f1s) / len(f1s)) if f1s else None


def train_encoder(cfg: Config, task: str, days: int = LABEL_DAYS, *, src: Source | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """Fit the encoder on the gold (GOLD_WEIGHT) and silver (teachers averaged) of its training
    days (in_encoder_train_split), never on a final test day nor on a day eval calibrates it with;
    report accuracy and macro-F1 on the gold of those other days."""
    labels = [c for c, _d in TASKS[task].candidates]
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    reqs = window_units(cfg, task, days, src, today)
    gold, silver = gold_latest(task), silver_by_unit(task)
    open_days = {u: r for u, r in reqs.items() if not final_test(r)}
    train = {u: r for u, r in open_days.items() if in_encoder_train_split(r)}
    test = {u for u in gold if u in open_days and u not in train}
    data = _training_data(gold, silver, train, labels)
    if not data:
        raise AutError(f"label train-encoder: no gold or silver labels for {task} yet")
    w = _fit(data, len(labels))
    # atomic: a crash mid-write must not leave a corrupt encoder that the nightly run then loads
    atomic_write_text(encoder_path(task), json.dumps(
        {"task": task, "task_ver": TASKS[task].ver, "labels": labels,
         "w": {str(f): [round(x, 5) for x in r] for f, r in w.items()}}))
    enc = Encoder(task)
    pairs = [(top_label(enc.distribution(reqs[u])), gold[u]) for u in sorted(test)]
    return {"model_id": enc.model_id, "model_rev": enc.model_rev, "trained_on": len(data),
            "held_out": len(pairs),
            "accuracy": _round4(sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
            "macro_f1": _macro_f1(pairs, labels)}


def print_eval(cfg: Config, task: str, records: list[dict[str, Any]]) -> None:
    print("\n".join(table(cfg, task) + details(records)))


def print_final(r: dict[str, Any]) -> None:
    print(final_line(r))


def print_choose(task: str, c: dict[str, Any]) -> None:
    print(f"label choose {task}: {c['backend']} {c['model_id']} (T {c['temperature']}, platt "
          f"{json.dumps(c['platt'])}, tau {json.dumps(c['tau'])})")


def print_train_encoder(task: str, r: dict[str, Any]) -> None:
    print(f"label train-encoder {task}: {json.dumps(r)}")
