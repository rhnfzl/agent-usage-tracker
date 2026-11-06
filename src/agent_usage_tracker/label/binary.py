"""Evaluate narrow yes/no judges before enabling any of them in the nightly labels.

Version 3 gold is required. Scores and reports have separate keys, so the experiment cannot
replace a version 2 choice or publish a label. Only the yes side has quality floors.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import date
from itertools import combinations
from typing import Any

from ..config import AutError, Config
from . import bakeoff
from .backends import close_all, make_backend
from .core import Choice
from .core import Request, calibrate, fit_platt
from .evalset import append_jsonl, eval_path, final_test, fold, read_jsonl, window_units
from .questions import GROUP_QUESTIONS, GROUPS, QUESTIONS, VER, noul


def labels(task: str) -> list[str]:
    return [q.label for q in QUESTIONS[task] if q.label != "other"] + [
        f"group:{q.label}" for q in GROUP_QUESTIONS[task]]


def members(task: str, label: str) -> tuple[str, ...]:
    return GROUPS[task][label[6:]] if label.startswith("group:") else (label,)


def request(req: Request, label: str) -> Request:
    group = label.startswith("group:")
    qs = GROUP_QUESTIONS[req.task] if group else QUESTIONS[req.task]
    q = next((q for q in qs if q.label == (label[6:] if group else label)), None)
    if q is None:
        raise ValueError(f"unknown binary question: {req.task}.{label}")
    body = noul(req.task, q)
    return replace(req, task=f"{req.task}.binary.{label.replace(':', '-')}", task_ver=VER,
                   question=json.dumps(body["instructions"], ensure_ascii=False),
                   decision="boolean", none_fits=None, candidates=[
                       {"id": "yes", "desc": body["criteria"]["true"]},
                       {"id": "no", "desc": body["criteria"]["false"]}])


def metrics(pred: list[bool], gold: list[bool], w: list[float]) -> dict[str, Any]:
    tp = sum(x for p, g, x in zip(pred, gold, w) if p and g)
    n_pred = sum(x for p, x in zip(pred, w) if p)
    n_true = sum(x for g, x in zip(gold, w) if g)

    def interval(denominator: float, subset: list[float]) -> list[float] | None:
        if not denominator:
            return None
        # shortcut: Kish effective n approximates unequal sampling, use a day bootstrap
        # when the audit needs intervals that also account for within-day dependence.
        n = denominator ** 2 / sum(x * x for x in subset)
        p, z2 = tp / denominator, 1.96 ** 2
        center = (p + z2 / (2 * n)) / (1 + z2 / n)
        radius = math.sqrt(z2 * (p * (1 - p) / n + z2 / (4 * n * n))) / (1 + z2 / n)
        return [max(0., center - radius), min(1., center + radius)]

    return {"n": len(gold), "positives": sum(gold),
            "precision": tp / n_pred if n_pred else None,
            "recall": tp / n_true if n_true else None,
            "f1": 2 * tp / (n_pred + n_true) if n_pred + n_true else None,
            "coverage": n_pred / sum(w),
            "precision_wilson": interval(n_pred, [x for p, x in zip(pred, w) if p]),
            "recall_wilson": interval(n_true, [x for g, x in zip(gold, w) if g]),
            "interval_method": "Wilson with Kish effective n (approximate for unequal weights)"}


def threshold(p: list[float], gold: list[bool], w: list[float],
              precision: float, recall: float) -> float | None:
    """Best weighted F1 within both floors. A tie never splits identical probabilities."""
    total = sum(x for g, x in zip(gold, w) if g)
    if not total:
        return None
    rows = sorted(zip(p, gold, w), reverse=True)
    tp = pred = 0.0
    best, best_f1 = None, -1.0
    for i, (prob, g, weight) in enumerate(rows):
        tp += weight * g
        pred += weight
        if i + 1 < len(rows) and rows[i + 1][0] == prob:
            continue
        f1 = 2 * tp / (pred + total)
        if tp / pred >= precision and tp / total >= recall and f1 >= best_f1:
            best, best_f1 = prob, f1
    return best


def _dist(p: float) -> dict[str, float]:
    return {"yes": p, "no": 1 - p}


def _cal(p: float, fit: dict[str, Any] | None) -> float:
    return calibrate(_dist(p), platt=fit)["yes"]


def tune(p: list[float], gold: list[bool], w: list[float], folds: list[int],
         precision: float, recall: float, *, by_fold: list[list[float]] | None = None,
         ) -> tuple[dict[str, Any], list[float], list[bool]]:
    """Choose calibration on inner training folds, then report on untouched outer days."""
    if not p or not len(p) == len(gold) == len(w) == len(folds):
        raise ValueError("binary eval needs aligned nonempty scores, gold, weights and folds")
    if any(not math.isfinite(x) or not 0 <= x <= 1 for x in p) or any(
            not math.isfinite(x) or x <= 0 for x in w):
        raise ValueError("binary eval needs finite probabilities and positive weights")
    if set(folds) != set(range(5)):
        raise ValueError("binary eval needs all five day folds, numbered 0 to 4")
    scores = by_fold if by_fold is not None else [p] * 5
    if len(scores) != 5 or any(len(s) != len(p) for s in scores):
        raise ValueError("binary eval needs five aligned score vectors")
    raw_oof = [scores[k][i] for i, k in enumerate(folds)]

    def brier(probs: list[float]) -> float:
        return sum(x * (v - g) ** 2 for v, g, x in zip(probs, gold, w)) / sum(w)

    def choose(values: list[float], indices: list[int]) -> dict[str, Any] | None:
        if len({folds[i] for i in indices}) < 2:
            return None
        raw_loss = cal_loss = 0.0
        for k in sorted({folds[i] for i in indices}):
            train = [i for i in indices if folds[i] != k]
            fit = fit_platt([_dist(values[i]) for i in train],
                            ["yes" if gold[i] else "no" for i in train], "yes")
            for i in indices:
                if folds[i] == k:
                    raw_loss += w[i] * (values[i] - gold[i]) ** 2
                    cal_loss += w[i] * (_cal(values[i], fit) - gold[i]) ** 2
        if cal_loss >= raw_loss:
            return None
        return fit_platt([_dist(values[i]) for i in indices],
                         ["yes" if gold[i] else "no" for i in indices], "yes")

    selected = [choose(scores[k], [i for i in range(len(p)) if folds[i] != k]) for k in range(5)]
    probs, pred = [0.] * len(p), [False] * len(p)
    cuts = []
    for k, selected_fit in enumerate(selected):
        train = [i for i in range(len(p)) if folds[i] != k]
        values = [_cal(v, selected_fit) for v in scores[k]]
        cut = threshold([values[i] for i in train], [gold[i] for i in train],
                        [w[i] for i in train], precision, recall)
        cuts.append(cut)
        for i in range(len(p)):
            if folds[i] == k:
                probs[i], pred[i] = values[i], cut is not None and values[i] >= cut
    full_fit = choose(p, list(range(len(p))))
    cut = threshold([_cal(v, full_fit) for v in p], gold, w, precision, recall)
    row = metrics(pred, gold, w)
    infeasible = sum(cuts[k] is None for k in set(folds))
    row.update(tau=cut, fold_tau=cuts, infeasible_folds=infeasible,
               calibration="platt" if full_fit else "none", platt=full_fit,
               fold_platt=selected, brier_raw=brier(raw_oof), brier_cal=brier(probs),
               passes=(row["precision"] is not None and row["precision"] >= precision
                       and row["recall"] is not None and row["recall"] >= recall
                       and infeasible <= 1))
    return row, probs, pred


def combine(probs: dict[str, float], passed: dict[str, bool]) -> str | None:
    candidates = [lab for lab in probs if passed.get(lab) and not lab.startswith("group:")]
    if not candidates:
        candidates = [lab for lab in probs if passed.get(lab)]
    return max(candidates, key=probs.__getitem__) if candidates else None


def log_odds_mean(probs: list[float]) -> float:
    z = sum(math.log(max(p, 1e-12) / max(1 - p, 1e-12)) for p in probs) / len(probs)
    return 1 / (1 + math.exp(-z))


def _memory_guard() -> None:
    if sys.platform != "darwin":
        return
    p = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True,
                       text=True, timeout=10)
    free = re.search(r"System-wide memory free percentage:\s*(\d+)%", p.stdout)
    if p.returncode or free is None:
        raise AutError("binary eval: cannot check free memory; no model loaded")
    if int(free[1]) < 15:
        raise AutError("binary eval stopped: free memory is under 15%; resume manually")


class _Guarded:
    def __init__(self, backend: Any):
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def distribution(self, req: Request) -> dict[str, float]:
        _memory_guard()
        return self._backend.distribution(req)


def _report(task: str, labs: list[str], golds: list[str], w: list[float],
            folds: list[int], raw: dict[str, list[float]], precision: float, recall: float,
            *, by_fold: dict[str, list[list[float]]] | None = None) -> dict[str, Any]:
    reports, probs, pred = {}, {}, {}
    for lab in labs:
        yes = [g in members(task, lab) for g in golds]
        reports[lab], probs[lab], pred[lab] = tune(
            raw[lab], yes, w, folds, precision, recall,
            by_fold=by_fold[lab] if by_fold else None)
    chosen = [combine({lab: probs[lab][i] for lab in labs},
                      {lab: pred[lab][i] for lab in labs})
              for i in range(len(golds))]
    combined = {}
    for lab in labs:
        true = [g in members(task, lab) for g in golds]
        picked = [c == lab or (lab.startswith("group:") and c in members(task, lab))
                  for c in chosen]
        row = metrics(picked, true, w)
        row["passes"] = (reports[lab]["passes"] and row["precision"] is not None
                         and row["precision"] >= precision and row["recall"] is not None
                         and row["recall"] >= recall)
        combined[lab] = row
    return {"per_label": reports, "combined": combined,
            "coverage": sum(x for c, x in zip(chosen, w) if c is not None) / sum(w)}


def run_eval(cfg: Config, task: str, days: int, models: list[str], *,
             selected: list[str] | None = None, precision: float = .70,
             recall: float = .70) -> list[dict[str, Any]]:
    """A draft experiment. No choices, sink rows, final days or old gold are modified."""
    gold = {r["unit_id"]: r for r in read_jsonl(eval_path(task, "gold"))
            if r.get("task_ver") == VER and r.get("gold") in {
                q.label for q in QUESTIONS[task]}}
    if not gold:
        raise AutError(f"binary eval: {task} needs version 3 gold; version 2 scores cannot test it")
    labs = selected or labels(task)
    if not labs or set(labs) - set(labels(task)):
        raise AutError("binary eval: unknown or empty label selection")
    reqs = window_units(cfg, task, days)
    found = sorted(set(reqs) & set(gold))
    missing = len(gold) - len(found)
    if missing:
        raise AutError(f"binary eval: {missing} gold units lack current inputs; rebuild the snapshot")
    ids = [u for u in found if not final_test(reqs[u])]
    if not ids:
        raise AutError("binary eval: no version 3 gold outside the final-test days")
    truth = [gold[u]["gold"] for u in ids]
    if any("weight" not in gold[u] for u in ids):
        raise AutError("binary eval: version 3 gold needs explicit population weights")
    w = [float(gold[u]["weight"]) for u in ids]
    if any(not math.isfinite(x) or x <= 0 for x in w):
        raise AutError("binary eval: reference weights must be finite and positive")
    folds = [fold(reqs[u]) for u in ids]
    inputs = {lab: [request(reqs[u], lab) for u in ids] for lab in labs}
    hashes = {lab: [hashlib.sha256(json.dumps(asdict(r), sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
                    for r in inputs[lab]] for lab in labs}
    out = []
    for model in models:
        _memory_guard()
        backend = make_backend(task, Choice(model_id=model), cfg.label_model,
                               cfg.label_llama_server)
        raw, speeds = {}, {}
        try:
            if prepare := getattr(backend, "prepare", None):
                prepare()
            for lab in labs:
                requests = [replace(r, unit_id=f"{r.unit_id}:{h}")
                            for r, h in zip(inputs[lab], hashes[lab])]
                saved, speeds[lab] = bakeoff._score(_Guarded(backend), requests)
                raw[lab] = [saved[r.unit_id]["yes"] for r in requests]
        finally:
            close_all([backend])
        report = _report(task, labs, truth, w, folds, raw, precision, recall)
        record = {"task": task, "task_ver": VER, "date": date.today().isoformat(),
                  "model_id": backend.model_id, "model_rev": backend.model_rev,
                  "precision_floor": precision, "recall_floor": recall,
                  "unit_ids": ids, "golds": truth, "weights": w, "folds": folds,
                  "raw": raw, "input_hashes": hashes, "speed": speeds, **report}
        append_jsonl(eval_path(task, "binary-eval"), record)
        out.append(record)
    return out


def run_ensemble(task: str, models: list[str] | None = None, *, selected: list[str] | None = None,
                 precision: float = .70, recall: float = .70) -> list[dict[str, Any]]:
    """Average calibrated log-odds on the same units, fitted without the reporting fold."""
    from .backends import short_name

    latest = {r["model_id"]: r for r in read_jsonl(eval_path(task, "binary-eval"))
              if r.get("task_ver") == VER and (not models or r["model_id"] in models
                                               or short_name(r["model_id"]) in models)}
    if len(latest) < 2:
        raise AutError("binary ensemble needs saved binary eval records from at least two models")
    out = []
    for n in (2, 3):
        for rows in combinations(latest.values(), n):
            first = rows[0]
            if any(any(r[k] != first[k] for k in ("unit_ids", "golds", "weights", "folds"))
                   for r in rows[1:]):
                raise AutError("binary ensemble: model records have different reference sets")
            labs = sorted(set.intersection(*(set(r["raw"]) for r in rows)))
            if selected:
                if set(selected) - set(labs):
                    raise AutError("binary ensemble: selected questions lack saved scores")
                labs = selected
            if not labs:
                raise AutError("binary ensemble: no shared questions")
            if any(r["input_hashes"][lab] != first["input_hashes"][lab]
                   for r in rows[1:] for lab in labs):
                raise AutError("binary ensemble: model records have different input snapshots")
            full, by_fold = {}, {}
            for lab in labs:
                full[lab] = [log_odds_mean([
                    _cal(r["raw"][lab][i], r["per_label"][lab]["platt"]) for r in rows])
                    for i in range(len(first["unit_ids"]))]
                by_fold[lab] = [[log_odds_mean([
                    _cal(r["raw"][lab][i], r["per_label"][lab]["fold_platt"][k]) for r in rows])
                    for i in range(len(first["unit_ids"]))] for k in range(5)]
            report = _report(task, labs, first["golds"], first["weights"], first["folds"],
                             full, precision, recall, by_fold=by_fold)
            record = {"task": task, "task_ver": VER, "date": date.today().isoformat(),
                      "members": [r["model_id"] for r in rows],
                      "precision_floor": precision, "recall_floor": recall, **report}
            append_jsonl(eval_path(task, "binary-ensemble"), record)
            out.append(record)
    return out
