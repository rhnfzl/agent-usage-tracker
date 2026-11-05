"""`aut label ensemble`: odd combinations of models, from saved scores alone. No model runs here.

Every row (each model alone, each combination with each method) is scored on the same items: the
gold units outside the final test days that every candidate has a score for. Whatever a method fits
(member temperatures, weights, a stacked model) and the calibration and tau bakeoff.tune fits on
top use the training folds only, and every number is pooled out of fold. Trying hundreds of rows
flatters the best one, so `selection` also reports the score of picking a row without looking at
the fold it is scored on. Final test days stay out; `aut label eval --final --model ensemble:<id>`
scores the one chosen ensemble there.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import sys
from datetime import date, datetime, timezone
from typing import Any, NamedTuple, Sequence

from ..config import LABEL_DAYS, AutError, Config
from .backends import (BackendUnavailable, Encoder, combine, ensemble_rev, find_model, make_backend,
                       short_name)
from .bakeoff import (Dist, _fails, _gold_units, _macro, _macro_f1, _per_label, _saved_result,
                      _warn_thin, failures, newest, pareto, recommend, tune, weights)
from .core import ENSEMBLE_METHODS, Choice, Request, fit_temperature, softmax, temper, top_label
from .evalset import (FOLDS, append_jsonl, eval_path, final_test, fold, in_encoder_train_split,
                      read_jsonl, scores_path)
from .tasks import TASKS, Source

SIZES = (3, 5, 7)
NOISE = 0.01                 # a smaller gain of ensembles over single models does not recommend one
TOP_OTHERS = 10              # rows printed beyond the single models and the frontier


class Scores(NamedTuple):
    """Saved scores of the candidates on the items they all have: raws[member][item]."""
    task: str
    labels: list[str]
    recs: list[dict[str, Any]]
    raws: list[list[Dist]]
    golds: list[str]
    w: list[float]
    folds: list[int]


class _Fit(NamedTuple):
    """What one training split fits per member, before any combination."""
    train: list[int]
    temps: list[float]
    cal: list[list[Dist]]        # cal[member][item], every item, at the temperature of this split
    f1s: list[float]


class _Run(NamedTuple):
    rec: dict[str, Any]
    preds: list[str | None]


def _train(folds: Sequence[int], k: int) -> list[int]:
    """The items a fit without fold k may read. k = FOLDS: all of them, for what gets saved."""
    return [i for i, f in enumerate(folds) if f != k]


def _fit_members(s: Scores) -> list[_Fit]:
    """One _Fit per fold, and last the one on every item."""
    out = []
    for k in range(FOLDS + 1):
        train = _train(s.folds, k)
        golds = [s.golds[i] for i in train]
        temps = [fit_temperature([raws[i] for i in train], golds) for raws in s.raws]
        out.append(_Fit(train, temps,
                        [[temper(d, t) for d in raws] for raws, t in zip(s.raws, temps)],
                        [_macro_f1([(top_label(raws[i]), s.golds[i]) for i in train], s.labels)
                         or 0.0 for raws in s.raws]))
    return out


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with pivoting; a is positive definite here (a ridge is in it)."""
    m = [row[:] + [x] for row, x in zip(a, b)]
    n = len(m)
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    out = [0.0] * n
    for c in range(n - 1, -1, -1):
        out[c] = (m[c][n] - sum(m[c][j] * out[j] for j in range(c + 1, n))) / m[c][c]
    return out


def fit_stack(rows: list[list[Dist]], golds: list[str], labels: list[str],
              l2: float = 1.0) -> dict[str, Any]:
    """The stacked logistic model: softmax over labels of one coefficient per member on its log
    probability, plus a bias per label (the first stays 0). Few parameters on purpose, gold is
    small: one set per member and label would overfit. Damped Newton, pulled towards the equal
    log pool, so correlated members stay finite. The pool itself without data."""
    if not rows:
        return {"a": [], "b": {}}
    n_m = len(rows[0])
    prior = [1 / n_m] * n_m + [0.0] * (len(labels) - 1)
    data = [([[math.log(max(d[lab], 1e-12)) for d in r] + [float(lab == x) for x in labels[1:]]
              for lab in labels], labels.index(g)) for r, g in zip(rows, golds)]
    dim = len(prior)

    def probs(t: list[float], xs: list[list[float]]) -> list[float]:
        return softmax([sum(a * b for a, b in zip(t, x)) for x in xs])

    def loss(t: list[float]) -> float:
        return l2 / 2 * sum((a - b) ** 2 for a, b in zip(t, prior)) - sum(
            math.log(max(probs(t, xs)[g], 1e-300)) for xs, g in data)

    theta = prior[:]
    for _ in range(25):
        grad = [l2 * (a - b) for a, b in zip(theta, prior)]
        hess = [[l2 * (i == j) for j in range(dim)] for i in range(dim)]
        for xs, g in data:
            p = probs(theta, xs)
            mean = [sum(q * x[j] for q, x in zip(p, xs)) for j in range(dim)]
            for j in range(dim):
                grad[j] += mean[j] - xs[g][j]
                for h in range(dim):
                    hess[j][h] += sum(q * x[j] * x[h] for q, x in zip(p, xs)) - mean[j] * mean[h]
        step, base, delta = 1.0, loss(theta), _solve(hess, grad)
        while step > 1e-4 and loss([a - step * d for a, d in zip(theta, delta)]) > base:
            step /= 2
        theta = [a - step * d for a, d in zip(theta, delta)]
        if sum(abs(step * d) for d in delta) < 1e-4:
            break
    return {"a": [round(x, 4) for x in theta[:n_m]],
            "b": {lab: round(x, 4) for lab, x in zip(labels[1:], theta[n_m:])}}


def _params(s: Scores, fit: _Fit, members: Sequence[int], method: str) -> dict[str, Any]:
    """What a method fits, from fit.train alone."""
    if method == "weighted":
        f1s = [fit.f1s[m] for m in members]
        total = sum(f1s)
        return {"weights": [round(x / total, 4) for x in f1s] if total
                else [round(1 / len(f1s), 4)] * len(f1s)}
    if method == "stack":
        return {"stack": fit_stack([[fit.cal[m][i] for m in members] for i in fit.train],
                                   [s.golds[i] for i in fit.train], s.labels)}
    return {}


def combos(n: int) -> list[tuple[int, ...]]:
    """Every odd group of 3, 5 or 7 out of n members."""
    return [c for size in SIZES if size <= n for c in itertools.combinations(range(n), size)]


def _total(recs: list[dict[str, Any]], key: str) -> float | None:
    """The members' sum; unknown when any member's is: the table then does not compare that axis."""
    xs = [x for r in recs if (x := r.get(key)) is not None]
    return round(sum(xs), 4) if len(xs) == len(recs) else None


def _single(s: Scores, m: int, p: float, r: float, base: dict[str, Any]) -> _Run:
    fields, preds = tune(s.task, [s.raws[m]] * FOLDS, s.raws[m], s.golds, s.w, s.folds, p, r)
    rec = s.recs[m]
    return _Run({**base, "method": "single", **{k: rec.get(k) for k in (
        "backend", "model_id", "model_rev", "size_mb", "s_per_decision", "load_s", "peak_mb")},
        "members": [rec["model_id"]], **fields}, preds)


def _combined(s: Scores, fits: list[_Fit], members: tuple[int, ...], method: str, p: float,
              r: float, base: dict[str, Any]) -> _Run:
    every = range(len(s.golds))
    params = [_params(s, f, members, method) for f in fits]
    mixed = [[combine(method, [f.cal[m][i] for m in members], **q) for i in every]
             for f, q in zip(fits, params)]
    fields, preds = tune(s.task, mixed[:FOLDS], mixed[FOLDS], s.golds, s.w, s.folds, p, r)
    recs, saved = [s.recs[m] for m in members], fits[FOLDS]
    ids = [x["model_id"] for x in recs]
    key = hashlib.sha256("\0".join(sorted(ids)).encode()).hexdigest()[:8]
    recipe = {"id": f"{method}-{key}", "method": method,
              "members": [{"backend": x["backend"], "model_id": x["model_id"],
                           "model_rev": x["model_rev"], "temperature": saved.temps[m]}
                          for x, m in zip(recs, members)],
              "weights": None, "stack": None, **params[FOLDS]}
    return _Run({**base, "method": method, "backend": "ensemble",
                 "model_id": f"ensemble/{recipe['id']}", "model_rev": ensemble_rev(recipe),
                 "members": ids, "ensemble": recipe,
                 **{k: _total(recs, k) for k in ("size_mb", "s_per_decision", "load_s", "peak_mb")},
                 **fields}, preds)


def _without_fold(s: Scores, k: int) -> Scores:
    """The same scores with fold k's items gone."""
    keep = _train(s.folds, k)

    def only(xs: list[Any]) -> list[Any]:
        return [xs[i] for i in keep]
    return s._replace(raws=[only(x) for x in s.raws], golds=only(s.golds), w=only(s.w),
                      folds=only(s.folds))


def _pick_score(run: _Run, s: Scores, p: float, r: float) -> tuple[bool, float]:
    per = _per_label(run.preds, s.golds, s.labels)
    view = {"per_label": {lab: {"precision_at_tau": v["precision"],
                                "recall_at_tau": v["recall"]} for lab, v in per.items()}}
    return not failures(view, p, r), _macro(per) or 0.0


def nested_pick(runs: list[_Run], inner: list[list[_Run]], s: Scores, p: float,
                r: float) -> tuple[float | None, dict[str, str]]:
    """The macro-F1 of the pick itself: fold k's answers come from the row that did best (passing
    the floors first) in inner[k], the same rows in the same order, refit and scored with fold k
    gone. So neither the pick nor the fits it compares read fold k. shortcut: the way to calibrate
    in `runs` is still picked over all folds (bakeoff.tune, one pick of at most three), upgrade with
    tune's own nesting."""
    out: list[str | None] = [None] * len(s.golds)
    picks: dict[str, str] = {}
    for k in range(FOLDS):
        view = _without_fold(s, k)
        best = runs[max(range(len(runs)), key=lambda j: _pick_score(inner[k][j], view, p, r))]
        picks[str(k)] = best.rec["model_id"]
        for i, f in enumerate(s.folds):
            if f == k:
                out[i] = best.preds[i]
    return _macro(_per_label(out, s.golds, s.labels)), picks


def _candidates(cfg: Config, task: str, prompt_ver: str,
                models: list[str] | None) -> list[tuple[dict[str, Any], dict[str, Any], bool]]:
    """(newest eval record, saved scores by unit, is the encoder) per model on this prompt. Reads
    files only: building a backend finds its score file and loads nothing."""
    recs = {r["model_id"]: r for r in newest(task).values() if r.get("prompt_ver") == prompt_ver}
    if models:
        wanted = {m.repo if (m := find_model(x)) else x for x in models}
        if wanted - set(recs):
            raise AutError(f"label ensemble: no eval record of {sorted(wanted - set(recs))} for "
                           f"{task}. Known: {sorted(short_name(x) for x in recs)}")
        recs = {k: v for k, v in recs.items() if k in wanted}
    out = []
    for rec in recs.values():
        try:
            b = make_backend(task, Choice(backend=rec["backend"], model_id=rec["model_id"],
                                          model_rev=rec["model_rev"]), cfg.label_model,
                             cfg.label_llama_server)
        except BackendUnavailable as exc:
            print(f"aut: label ensemble: {rec['model_id']} left out: {exc}", file=sys.stderr)
            continue
        path = scores_path(task, rec["model_id"], rec["model_rev"], prompt_ver,
                           getattr(b, "protocol", ""))
        out.append((rec, {x["unit_id"]: x["dist"] for x in read_jsonl(path)},
                    isinstance(b, Encoder)))
    return out


def load_scores(cfg: Config, task: str, days: int, src: Source | None, today: date,
                models: list[str] | None = None) -> tuple[Scores, str]:
    """The items every candidate has. An item some candidate lacks leaves every row, and the
    candidate is named: rows are never compared over unequal samples."""
    pool = [(q, g) for q, g in _gold_units(cfg, task, days, src, today) if not final_test(q)]
    if not pool:
        raise AutError(f"label ensemble: no gold labels for {task} in the last {days} days outside "
                       "the final test days (label some with `aut label review`)")
    prompt_ver = pool[0][0].prompt_ver
    cands = _candidates(cfg, task, prompt_ver, models)
    if len(cands) < min(SIZES):
        raise AutError(f"label ensemble: {len(cands)} models with scores for {task}, at least "
                       f"{min(SIZES)} needed; run `aut label eval` first")

    def usable(q: Request, saved: dict[str, Any], enc: bool) -> bool:
        # the encoder's scores on its own training days would be in-sample
        return q.unit_id in saved and not (enc and in_encoder_train_split(q))

    for rec, saved, enc in cands:
        have = sum(usable(q, saved, enc) for q, _g in pool)
        if have < len(pool):
            print(f"aut: label ensemble: {short_name(rec['model_id'])} has scores for {have} of "
                  f"{len(pool)} tuning gold units; every row uses only what all have (--models "
                  "leaves a model out)", file=sys.stderr)
    items = [(q, g) for q, g in pool if all(usable(q, saved, enc) for _r, saved, enc in cands)]
    if not items:
        raise AutError(f"label ensemble: no gold unit of {task} has a score from every model; run "
                       "`aut label eval`, or leave a model out with --models")
    labels = [c for c, _d in TASKS[task].candidates]
    _warn_thin(task, [g for _q, g in items], "the shared gold")
    w = weights(task, [q.unit_id for q, _g in items])
    return Scores(task, labels, [r for r, _s, _e in cands],
                  [[{lab: saved[q.unit_id].get(lab, 0.0) for lab in labels} for q, _g in items]
                   for _r, saved, _e in cands],
                  [g for _q, g in items], [w[q.unit_id] for q, _g in items],
                  [fold(q) for q, _g in items]), prompt_ver


def evaluate_all(s: Scores, precision: float, recall: float,
                 base: dict[str, Any]) -> tuple[list[_Run], list[_Run]]:
    """(each model alone, each combination with each method), all on the same items."""
    fits = _fit_members(s)
    singles = [_single(s, m, precision, recall, base) for m in range(len(s.raws))]
    return singles, [_combined(s, fits, g, method, precision, recall, base)
                     for g in combos(len(s.raws)) for method in ENSEMBLE_METHODS]


def _nested(s: Scores, singles: list[_Run], mixed: list[_Run], p: float, r: float,
            base: dict[str, Any]) -> tuple[float | None, float | None, dict[str, str]]:
    """(nested_f1, nested_single_f1, picks). No numbers when one fold holds every item: there is
    nothing else to pick on."""
    if len(set(s.folds)) < 2:
        return None, None, {}
    inner = [evaluate_all(_without_fold(s, k), p, r, base) for k in range(FOLDS)]
    nested_mixed, picks = nested_pick(mixed, [x[1] for x in inner], s, p, r)
    return nested_mixed, nested_pick(singles, [x[0] for x in inner], s, p, r)[0], picks


def run_ensemble(cfg: Config, task: str, days: int = LABEL_DAYS, *, src: Source | None = None,
                 models: list[str] | None = None,
                 now: datetime | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fit and score every row from saved scores; the records go to <task>.ensemble.jsonl."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    s, prompt_ver = load_scores(cfg, task, days, src, today, models)
    p, r = cfg.label_precision, cfg.label_recall
    ver = TASKS[task].ver
    base = {"ts": now.isoformat(), "task": task, "task_ver": ver, "prompt_ver": prompt_ver,
            "calib_ver": f"{task}@{ver}/{today.isoformat()}"}
    print(f"aut: label ensemble: {task}: {len(combos(len(s.raws))) * len(ENSEMBLE_METHODS)} rows "
          f"from {len(s.raws)} models on {len(s.golds)} units, each fitted {FOLDS + 1} times; this "
          "can take many minutes", file=sys.stderr)
    singles, mixed = evaluate_all(s, p, r, base)
    nested_mixed, nested_single, picks = _nested(s, singles, mixed, p, r, base)
    rows = [x.rec for x in singles + mixed]
    selection = {"kind": "selection", "ts": base["ts"], "task": task, "task_ver": base["task_ver"],
                 "n": len(s.golds), "n_tried": len(mixed),
                 "n_pass": sum(not _fails(x.rec, cfg) for x in mixed),
                 "best_f1": max(x.rec["macro_f1_tuned"] or 0 for x in mixed),
                 "nested_f1": nested_mixed,
                 "best_single_f1": max(x.rec["macro_f1_tuned"] or 0 for x in singles),
                 "nested_single_f1": nested_single, "picks": picks}
    path = eval_path(task, "ensemble")
    for row in [*rows, selection]:
        append_jsonl(path, row)
    return rows, selection


def table(cfg: Config, task: str, rows: list[dict[str, Any]]) -> list[str]:
    """As bakeoff.table: `*` on the Pareto frontier of the rows that pass, `!` fails. Each model
    alone, the frontier and the best others; speed and memory of a combination are its members'
    sums. All rows are in the file."""
    rows = sorted(rows, key=lambda x: -(x.get("macro_f1_tuned") or 0))
    front = pareto([x for x in rows if not _fails(x, cfg)])
    others = [x for x in rows if x["method"] != "single" and not any(x is o for o in front)]
    shown = [x for x in rows if x["method"] == "single" or any(x is o for o in front)
             or any(x is o for o in others[:TOP_OTHERS])]

    def name(x: dict[str, Any]) -> str:
        return short_name(x["model_id"]) if x["method"] == "single" else x["ensemble"]["id"]

    lines = [f"label ensemble {task}: floors precision {cfg.label_precision}, recall "
             f"{cfg.label_recall}, coverage {cfg.label_coverage}; {rows[0]['n']} units in every row",
             f"   {'id':<30} {'F1':>6} {'F1@.5':>6} {'Brier':>6} {'NLL':>7} {'calib':<11} "
             f"{'s/item':>7} {'peak MB':>7} {'n':>5}  members"]
    for x in shown:
        mark = ("*" if any(x is o for o in front) else " ") + ("!" if _fails(x, cfg) else " ")
        lines.append(f"{mark} {name(x):<30} {x['macro_f1_tuned']!s:>6} {x['macro_f1_default']!s:>6} "
                     f"{x['brier']!s:>6} {x['nll']!s:>7} {x['calibration']:<11} "
                     f"{x.get('s_per_decision')!s:>7} {x.get('peak_mb')!s:>7} {x['n']:>5}  "
                     + ("" if x["method"] == "single"
                        else ", ".join(short_name(m) for m in x["members"])))
    return lines + [f"{len(shown)} of {len(rows)} rows; all in {eval_path(task, 'ensemble')}",
                    "* Pareto frontier of the rows that pass   ! fails: "
                    + "; ".join(f"{name(x)}: {', '.join(_fails(x, cfg))}"
                                for x in shown if _fails(x, cfg))]


def _gain(sel: dict[str, Any]) -> float | None:
    if sel["nested_f1"] is None or sel["nested_single_f1"] is None:
        return None
    return round(sel["nested_f1"] - sel["nested_single_f1"], 4)


def _is_noise(sel: dict[str, Any]) -> bool:
    gain = _gain(sel)
    return gain is not None and gain < NOISE


def selection_lines(sel: dict[str, Any]) -> list[str]:
    """What the pick is worth, in plain numbers."""
    return [f"selection: {sel['n_tried']} combinations tried on {sel['n']} units, {sel['n_pass']} "
            f"pass. The best row's F1 {sel['best_f1']} is flattered by that many tries.",
            f"  picked without reading the fold it is scored on: ensembles {sel['nested_f1']}, "
            f"single models {sel['nested_single_f1']}, gain {_gain(sel)}. Under {NOISE} counts as "
            "noise (a rule of thumb, as in the two-decimal tie, not measured on this gold)."
            + (" That is noise here: prefer the best single model." if _is_noise(sel) else "")]


def print_ensemble(cfg: Config, task: str, rows: list[dict[str, Any]],
                   selection: dict[str, Any]) -> None:
    single = recommend([x for x in rows if x["method"] == "single"], cfg)
    win = single if single and _is_noise(selection) else recommend(rows, cfg)
    pick = None if win is None else (
        f"ensemble:{win['ensemble']['id']}" if win.get("ensemble") else short_name(win["model_id"]))
    print("\n".join(table(cfg, task, rows) + selection_lines(selection)))
    print(f"recommended: aut label choose --task {task} --model {pick}" if pick else
          "recommended: none, no row passes the floors. They stay as they are.")
    if win is None:
        return
    if win.get("ensemble"):
        if _is_noise(selection):
            print("no single model passes the floors, so it is this ensemble despite the noise")
        print(f"then, once, the last check: aut label eval --task {task} --final --model {pick}")
        return
    own = _saved_result(task, win["model_id"])  # what choose writes: other items than this row's
    if (own["n"], own["macro_f1_tuned"]) != (win["n"], win["macro_f1_tuned"]):
        print(f"choose writes its own eval record, not this row: n {own['n']}, F1 "
              f"{own['macro_f1_tuned']}, floors missed: {', '.join(_fails(own, cfg)) or 'none'}")
