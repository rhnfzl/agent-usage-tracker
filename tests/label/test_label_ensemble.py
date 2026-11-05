"""Hermetic tests for label/ensemble.py and the Ensemble backend. No model.

Locks the rules:
  - a fold's weights, member temperatures, stacked model and decisions never read that fold's items
    (mutation tests, and a sabotage test that shows those tests catch a leak);
  - every row is on the same items, an item a member lacks leaves every row, and the row says so;
  - the final test days reach nothing but `eval --final`, for the one chosen ensemble;
  - saved scores alone: no model runs in `aut label ensemble`;
  - the backend reproduces at night what was evaluated, loads each member once, and names a missing
    member before anything loads.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from types import SimpleNamespace

import pytest

from agent_usage_tracker import cli
from agent_usage_tracker.config import AutError, load_config
from agent_usage_tracker.label import backends, bakeoff, core, ensemble, evalset, run, tasks
from label_helpers import BASE, LABELS, NOW, TASK, TODAY, WORD, FakeSource, _cfg, _env, _gold


@pytest.fixture
def no_sink(monkeypatch):
    monkeypatch.setattr(bakeoff, "send_activity_rows", lambda dsn, rows: None)


def _rnd(*parts) -> float:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16) % 10**6 / 10**6


class Noisy:
    """Answers the gold word in the steer text, wrong on a stable `miss` share of units."""
    backend, size_mb = "mlx_letter", 100.0

    def __init__(self, name, miss=0.1, peak=1000.0):
        self.name, self.miss, self.peak = name, miss, peak
        self.model_id, self.model_rev = f"fake/{name}", "r1"
        self.calls = 0

    def peak_memory_mb(self):
        return self.peak

    def distribution(self, req):
        self.calls += 1
        gold = req.facts["text"].split()[0]
        i = LABELS.index(gold)
        top = LABELS[(i + 1) % len(LABELS)] if _rnd(self.name, req.unit_id) < self.miss else gold
        p = 0.55 + 0.4 * _rnd(self.name, "p", req.unit_id)
        return {c: p if c == top else (1 - p) / (len(LABELS) - 1) for c in LABELS}


MEMBERS = (("a", 0.05), ("b", 0.1), ("c", 0.15))


def _scored(tmp_path, members=MEMBERS, n=8):
    """Gold, saved scores and eval records of fakes. Call inside _env."""
    cfg, src, reqs = _gold(tmp_path, LABELS * n)
    fakes = [Noisy(*m) for m in members]
    bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=fakes)
    return cfg, src, reqs, fakes


def _synthetic(n=120, members=3):
    """Scores with no files: models of different skill, items dealt to the folds in turn."""
    rng = random.Random(7)
    golds = [LABELS[i % len(LABELS)] for i in range(n)]
    raws = []
    for m in range(members):
        rows = []
        for g in golds:
            top = g if rng.random() < 0.9 - 0.15 * m else rng.choice(LABELS)
            p = rng.uniform(0.5, 0.95)
            rows.append({c: p if c == top else (1 - p) / (len(LABELS) - 1) for c in LABELS})
        raws.append(rows)
    recs = [{"backend": "mlx_letter", "model_id": f"m/{m}", "model_rev": "r", "size_mb": 1.0,
             "s_per_decision": 0.5, "load_s": 2.0, "peak_mb": 100.0} for m in range(members)]
    return ensemble.Scores(TASK, list(LABELS), recs, raws, golds, [1.0] * n,
                           [i % evalset.FOLDS for i in range(n)])


def _flip_gold(s, k):
    other = {g: LABELS[(LABELS.index(g) + 1) % len(LABELS)] for g in LABELS}
    return s._replace(golds=[other[g] if f == k else g for g, f in zip(s.golds, s.folds)])


def _flip_scores(s, k):
    swap = list(reversed(LABELS))
    flipped = [[{c: d[swap[j]] for j, c in enumerate(LABELS)} if f == k else d
                for d, f in zip(raws, s.folds)] for raws in s.raws]
    return s._replace(raws=flipped)


def _fitted(s, k, method):
    """Everything fold k's model is made of."""
    fit = ensemble._fit_members(s)[k]
    return fit.temps, fit.f1s, ensemble._params(s, fit, (0, 1, 2), method)


# ---- leakage ----

@pytest.mark.parametrize("method", ["weighted", "stack"])
@pytest.mark.parametrize("flip", [_flip_gold, _flip_scores])
def test_a_folds_fit_never_reads_its_own_items(method, flip):
    s = _synthetic()
    before = _fitted(s, 0, method)
    assert _fitted(flip(s, 0), 0, method) == before         # its own fold changed: no effect
    assert _fitted(flip(s, 1), 0, method) != before         # another fold changed: it reads that


@pytest.mark.parametrize("method", ["weighted", "stack"])
def test_the_mutation_test_catches_a_leak(method, monkeypatch):
    s = _synthetic()
    monkeypatch.setattr(ensemble, "_train", lambda folds, k: list(range(len(folds))))
    assert _fitted(_flip_gold(s, 0), 0, method) != _fitted(s, 0, method)


@pytest.mark.parametrize("method", core.ENSEMBLE_METHODS)
@pytest.mark.parametrize("kind", ["none", "temperature"])
def test_a_folds_decisions_do_not_move_when_its_own_gold_does(method, kind, monkeypatch):
    # With the way to calibrate held still: tune picks it over all folds, as evaluate() always did,
    # one pick of at most three (tested apart below). Three Brier calls per tune: none,
    # temperature, the report.
    calls = []

    def brier(rows):
        calls.append(1)
        first = len(calls) % 3 == 1
        return 1.0 if first == (kind == "temperature") else 0.5
    monkeypatch.setattr(bakeoff, "brier", brier)
    s, flipped = _synthetic(), _flip_gold(_synthetic(), 0)
    one = ensemble._combined(s, ensemble._fit_members(s), (0, 1, 2), method, 0.5, 0.2, {})
    two = ensemble._combined(flipped, ensemble._fit_members(flipped), (0, 1, 2), method, 0.5, 0.2,
                             {})
    assert one.rec["calibration"] == two.rec["calibration"] == kind
    own = [i for i, f in enumerate(s.folds) if f == 0]
    assert [one.preds[i] for i in own] == [two.preds[i] for i in own]
    assert one.preds != two.preds                           # the other folds did read that gold


def test_picking_a_row_is_reported_without_the_flattery():
    """Noise models: the best of many rows looks good, the honest pick does not."""
    rng = random.Random(3)
    n = 100
    golds = [rng.choice(LABELS) for _ in range(n)]
    runs = []
    for j in range(60):
        preds = [rng.choice(LABELS) for _ in range(n)]
        runs.append(ensemble._Run({"model_id": f"m/{j}"}, preds))
    s = ensemble.Scores(TASK, list(LABELS), [], [], golds, [1.0] * n,
                        [i % evalset.FOLDS for i in range(n)])
    best = max(bakeoff._macro(bakeoff._per_label(r.preds, golds, LABELS)) for r in runs)
    inner = [[r._replace(preds=[r.preds[i] for i in ensemble._train(s.folds, k)]) for r in runs]
             for k in range(evalset.FOLDS)]
    nested, picks = ensemble.nested_pick(runs, inner, s, 0.9, 0.6)
    assert nested < best - 0.05 and set(picks) == {"0", "1", "2", "3", "4"}


def _picks(s):
    singles, mixed = ensemble.evaluate_all(s, 0.5, 0.2, {})
    return ensemble._nested(s, singles, mixed, 0.5, 0.2, {})[2]


def test_a_folds_pick_never_reads_its_own_items():
    s = _synthetic(members=4)
    assert _picks(_flip_gold(s, 0))["0"] == _picks(s)["0"]


def test_that_pick_test_catches_a_leak(monkeypatch):
    s = _synthetic(members=4)
    monkeypatch.setattr(ensemble, "_without_fold", lambda s, k: s)
    assert _picks(_flip_gold(s, 0))["0"] != _picks(s)["0"]


def test_one_fold_with_every_item_has_nothing_to_pick_on():
    s = _synthetic()
    s = s._replace(folds=[0] * len(s.golds))
    singles, mixed = ensemble.evaluate_all(s, 0.5, 0.2, {})
    assert ensemble._nested(s, singles, mixed, 0.5, 0.2, {}) == (None, None, {})
    sel = {"n_tried": 1, "n": 1, "n_pass": 1, "best_f1": 1, "nested_f1": None,
           "nested_single_f1": None}
    assert "That is noise" not in "\n".join(ensemble.selection_lines(sel))


# ---- the methods ----

def test_combine_each_method():
    a = {"x": 0.8, "y": 0.1, "z": 0.1}
    b = {"x": 0.1, "y": 0.6, "z": 0.3}
    c = {"x": 0.45, "y": 0.1, "z": 0.45}
    soft = backends.combine("soft", [a, b, c])
    assert soft["x"] == pytest.approx(0.45) and sum(soft.values()) == pytest.approx(1)
    assert backends.combine("confident", [a, b, c]) is a
    assert backends.combine("weighted", [a, b, c], weights=[0.0, 1.0, 0.0])["y"] == pytest.approx(.6)
    # a vote: the share of members, however sure each is
    votes = backends.combine("majority", [a, a, b])
    assert votes["x"] == pytest.approx(2 / 3) and votes["y"] == pytest.approx(1 / 3)
    # three members, three answers: the mean breaks the tie
    tied = backends.combine("majority", [a, b, {"x": 0.1, "y": 0.2, "z": 0.7}])
    assert core.top_label(tied) == "z" and tied["z"] < core.TAU_FLOOR
    # the stack: only the first member counts
    stacked = backends.combine("stack", [a, b], stack={"a": [1.0, 0.0], "b": {}})
    assert stacked == pytest.approx(a)


def test_combos_are_odd_and_never_more_than_seven():
    assert ensemble.combos(2) == []
    assert len(ensemble.combos(3)) == 1 and len(ensemble.combos(4)) == 4
    assert len(ensemble.combos(7)) == 35 + 21 + 1
    assert {len(c) for c in ensemble.combos(9)} == {3, 5, 7}


def test_the_stack_learns_which_member_to_trust():
    rng = random.Random(1)
    labels = list(LABELS)
    golds = [rng.choice(labels) for _ in range(200)]

    def answer(top, p=0.8):
        return {c: p if c == top else (1 - p) / (len(labels) - 1) for c in labels}

    rows = [[answer(g), answer(rng.choice(labels))] for g in golds]
    fitted = ensemble.fit_stack(rows, golds, labels)
    assert fitted["a"][0] > 0.5 > abs(fitted["a"][1]) and set(fitted["b"]) == set(labels[1:])
    # a member's coefficients stay finite on separable data, and no data gives no fit
    assert all(math.isfinite(x) for x in fitted["a"]) and ensemble.fit_stack([], [], labels)["a"] == []


def test_weights_come_from_the_training_f1_and_equal_when_nothing_scores():
    s = _synthetic()
    fit = ensemble._fit_members(s)[evalset.FOLDS]
    w = ensemble._params(s, fit, (0, 1, 2), "weighted")["weights"]
    assert w[0] > w[1] > w[2] and sum(w) == pytest.approx(1, abs=1e-3)
    assert ensemble._params(s, fit._replace(f1s=[0.0, 0.0, 0.0]), (0, 1, 2),
                            "weighted")["weights"] == [0.3333] * 3


# ---- the run ----

def test_every_row_is_on_the_same_items_from_saved_scores_alone(tmp_path, monkeypatch, no_sink,
                                                                capsys):
    with _env(tmp_path):
        cfg, src, reqs, fakes = _scored(tmp_path)
        calls = [f.calls for f in fakes]

        def no_model(*a, **k):
            raise AssertionError("a model ran")
        monkeypatch.setattr(backends.MlxLetter, "_load", no_model)
        monkeypatch.setattr(backends.MlxLetter, "distribution", no_model)
        rows, sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)
        assert [f.calls for f in fakes] == calls
        open_days = sum(not evalset.final_test(q) for q in reqs.values())
        assert {r["n"] for r in rows} == {open_days} and sel["n"] == open_days
        assert sum(r["method"] == "single" for r in rows) == 3
        assert {r["method"] for r in rows} == {"single", *core.ENSEMBLE_METHODS}
        assert sel["n_tried"] == len(core.ENSEMBLE_METHODS) and sel["nested_f1"] is not None
        # a single row is the same numbers as `aut label eval` on those items
        old = bakeoff.newest(TASK)[("fake/a", "r1", "steer_intent@2/p2")]
        one = next(r for r in rows if r["method"] == "single" and r["model_id"] == "fake/a")
        assert one["macro_f1_tuned"] == old["macro_f1_tuned"] and one["brier"] == old["brier"]
        # the records are numbers, ids and names: no text, no unit
        saved = evalset.read_jsonl(evalset.eval_path(TASK, "ensemble"))
        assert len(saved) == len(rows) + 1 and saved[-1]["kind"] == "selection"
        blob = json.dumps(saved)
        assert WORD not in blob and not any(u in blob for u in reqs)
        # the eval table and its records are untouched by ensemble rows
        assert all(r["backend"] != "ensemble" for r in bakeoff.newest(TASK).values())


def test_a_member_without_a_score_shrinks_every_row_and_says_so(tmp_path, no_sink, capsys):
    with _env(tmp_path):
        cfg, src, _reqs, _fakes = _scored(tmp_path, MEMBERS + (("d", 0.2),))
        rec = next(r for r in bakeoff.newest(TASK).values() if r["model_id"] == "fake/d")
        p = evalset.scores_path(TASK, "fake/d", "r1", rec["prompt_ver"])
        full = evalset.read_jsonl(p)
        evalset.write_jsonl(p, full[:-5])
        rows, sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)
        assert {r["n"] for r in rows} == {len(full) - 5} == {sel["n"]}
        assert f"fake/d has scores for {len(full) - 5} of {len(full)}" in capsys.readouterr().err
        # leaving it out brings the full sample back
        rows, _sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE,
                                           models=["fake/a", "fake/b", "fake/c"])
        assert {r["n"] for r in rows} == {len(full)}
        evalset.write_jsonl(p, [])
        with pytest.raises(AutError, match="no gold unit .* has a score from every model"):
            ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)


def test_models_filters_and_too_few_is_an_error(tmp_path, no_sink):
    with _env(tmp_path):
        cfg, src, _reqs, _fakes = _scored(tmp_path, MEMBERS + (("d", 0.2),))
        rows, _sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE,
                                           models=["fake/a", "fake/b", "fake/c"])
        assert {m for r in rows for m in r["members"]} == {"fake/a", "fake/b", "fake/c"}
        with pytest.raises(AutError, match="no eval record of .*fake/nope"):
            ensemble.run_ensemble(cfg, TASK, src=src, now=BASE, models=["fake/nope"])
        with pytest.raises(AutError, match="at least 3 needed"):
            ensemble.run_ensemble(cfg, TASK, src=src, now=BASE, models=["fake/a", "fake/b"])


def test_the_encoders_training_days_stay_out(tmp_path, monkeypatch, no_sink):
    """Its saved scores on those days would be in-sample: drop the days, even if a file holds them."""
    with _env(tmp_path):
        cfg, src, reqs, _fakes = _scored(tmp_path)
        rec = next(r for r in bakeoff.newest(TASK).values() if r["model_id"] == "fake/c")
        monkeypatch.setattr(ensemble, "make_backend", lambda task, c, m, llama: (
            backends.Encoder.__new__(backends.Encoder) if c.model_id == "fake/c"
            else backends.MlxLetter(c.model_id, c.model_rev)))
        p = evalset.scores_path(TASK, rec["model_id"], rec["model_rev"], rec["prompt_ver"])
        assert p.exists()
        rows, _sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)
        allowed = sum(not evalset.final_test(q) and not evalset.in_encoder_train_split(q)
                      for q in reqs.values())
        assert {r["n"] for r in rows} == {allowed}


def test_final_days_change_nothing(tmp_path, no_sink):
    with _env(tmp_path):
        cfg, src, reqs, _fakes = _scored(tmp_path)
        sealed = [u for u, q in reqs.items() if evalset.final_test(q)]
        assert sealed
        before = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)
        for u in sealed:
            evalset.append_jsonl(evalset.eval_path(TASK, "gold"), {
                "unit_id": u, "gold": "other", "labeller": "human", "date": "d", "task_ver": 2})
        for rec in bakeoff.newest(TASK).values():
            p = evalset.scores_path(TASK, rec["model_id"], rec["model_rev"], rec["prompt_ver"])
            for u in sealed:
                evalset.append_jsonl(p, {"unit_id": u, "dist": {LABELS[0]: 1.0}})
        assert ensemble.run_ensemble(cfg, TASK, src=src, now=BASE) == before
        # the final file is not opened: a broken one cannot matter
        evalset.eval_path(TASK, "final").mkdir(parents=True)
        assert ensemble.run_ensemble(cfg, TASK, src=src, now=BASE) == before


# ---- the table ----

def _row(method, f1, s, peak, model_id, fails=False):
    v = {"n": 5, "precision_at_tau": 0.5 if fails else 0.95, "recall_at_tau": 0.9,
         "share_error_pp": 0.0}
    rec = {"method": method, "model_id": model_id, "macro_f1_tuned": f1, "macro_f1_default": f1,
           "brier": 0.1, "nll": 0.1, "calibration": "none", "s_per_decision": s, "peak_mb": peak,
           "n": 50, "coverage": 0.9, "per_label": {LABELS[0]: v}, "members": [model_id]}
    if method != "single":
        rec["ensemble"] = {"id": model_id.split("/")[1]}
    return rec


def test_table_marks_and_recommendation_keep_the_owners_rule(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    best = _row("soft", 0.95, 3.0, 3000.0, "ensemble/soft-1")
    same = _row("majority", 0.951, 2.0, 3000.0, "ensemble/majority-2")      # same to two decimals
    single = _row("single", 0.80, 1.0, 1000.0, "m/one")
    beaten = _row("stack", 0.80, 9.0, 9000.0, "ensemble/stack-3")
    failing = _row("weighted", 0.99, 0.1, 1.0, "ensemble/weighted-4", fails=True)
    rows = [best, same, single, beaten, failing]
    with _env(tmp_path):
        lines = ensemble.table(cfg, TASK, rows)
    marks = {ln[3:].split()[0]: ln[:2] for ln in lines[2:-2]}
    assert marks == {"soft-1": "  ", "majority-2": "* ", "m/one": "* ", "stack-3": "  ",
                     "weighted-4": " !"}
    assert f"weighted-4: {LABELS[0]} precision 0.5" in lines[-1]
    # best score among those that pass, then the fastest
    assert bakeoff.recommend(rows, cfg) is same
    sel = {"n_tried": 4, "n": 50, "n_pass": 3, "best_f1": 0.95, "nested_f1": 0.9,
           "nested_single_f1": 0.8}
    with _env(tmp_path):
        ensemble.print_ensemble(cfg, TASK, rows, sel)
    out = capsys.readouterr().out
    assert "--model ensemble:majority-2" in out and "--final --model ensemble:majority-2" in out
    assert "gain 0.1" in out and "flattered" in out
    assert "That is noise" not in out
    assert "That is noise here" in "\n".join(ensemble.selection_lines({**sel, "nested_f1": 0.805}))


def test_speed_and_memory_are_the_members_sums_and_unknown_stays_unknown(tmp_path, no_sink):
    with _env(tmp_path):
        cfg, src, _reqs, _fakes = _scored(tmp_path)
        rows, _sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)
        ens = next(r for r in rows if r["method"] == "soft")
        solo = [r for r in rows if r["method"] == "single"]
        assert ens["peak_mb"] == 3000.0 and ens["size_mb"] == 300.0
        assert ens["s_per_decision"] == pytest.approx(sum(r["s_per_decision"] for r in solo), abs=1e-3)
    assert ensemble._total([{"peak_mb": 1.0}, {"peak_mb": None}], "peak_mb") is None


NOISY = {"n_tried": 4, "n": 50, "n_pass": 3, "best_f1": 0.95, "nested_f1": 0.805,
         "nested_single_f1": 0.8}


def test_a_gain_that_is_noise_recommends_the_single_model(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    rows = [_row("soft", 0.95, 3.0, 3000.0, "ensemble/soft-1"), _row("single", 0.80, 1.0, 1000.0, "m/one")]
    own = {**rows[1], "backend": "mlx_letter", "task_ver": tasks.TASKS[TASK].ver, "model_rev": "r",
           "prompt_ver": "p"}
    with _env(tmp_path):
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), own)
        ensemble.print_ensemble(cfg, TASK, rows, NOISY)
        out = capsys.readouterr().out
        assert "That is noise here" in out and "--model m/one" in out
        assert "ensemble:soft-1" not in out and "--final" not in out
        assert "choose writes its own" not in out       # the record is this row
        # no single model passes: the ensemble stands, and the output says why
        failing = _row("single", 0.80, 1.0, 1000.0, "m/one", fails=True)
        ensemble.print_ensemble(cfg, TASK, [rows[0], failing], NOISY)
        out = capsys.readouterr().out
        assert "--model ensemble:soft-1" in out and "despite the noise" in out


def test_a_single_models_own_record_is_named_when_it_is_not_the_row(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    row = _row("single", 0.90, 1.0, 1000.0, "m/one")
    own = {**row, "n": 80, "macro_f1_tuned": 0.7, "backend": "mlx_letter", "model_rev": "r",
           "task_ver": tasks.TASKS[TASK].ver, "prompt_ver": "p",
           "per_label": {LABELS[0]: {**row["per_label"][LABELS[0]], "precision_at_tau": 0.5}}}
    with _env(tmp_path):
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), own)
        ensemble.print_ensemble(cfg, TASK, [row], NOISY)
    out = capsys.readouterr().out
    assert "choose writes its own eval record, not this row: n 80, F1 0.7" in out
    assert f"{LABELS[0]} precision 0.5" in out


def test_unknown_speed_or_memory_does_not_lose_the_recommendation(tmp_path):
    cfg = _cfg(tmp_path)
    # each beats the next on the one axis they share, around a circle: pareto keeps none of them
    circle = [_row("soft", 0.9, 2.0, None, "ensemble/soft-a"), _row("soft", 0.9, 3.0, 1.0, "ensemble/soft-b"),
              _row("soft", 0.9, None, 2.0, "ensemble/soft-c"), _row("soft", 0.9, 1.0, 3.0, "ensemble/soft-d")]
    assert bakeoff.pareto(circle) == []
    assert bakeoff.recommend(circle, cfg) is circle[3]
    # unknown is last among equals; a real 0.0 (rounded) is the fastest, not unknown
    zero, slow = _row("soft", 0.9, 0.0, 1.0, "ensemble/soft-e"), _row("soft", 0.9, 9.0, 1.0, "ensemble/soft-f")
    assert bakeoff.recommend([slow, zero], cfg) is zero
    assert bakeoff.recommend([_row("soft", 0.9, None, 1.0, "ensemble/soft-g"), slow], cfg) is slow


# ---- choose, the backend and the nightly run ----

def _chosen(cfg, src, method="stack"):
    rows, _sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)
    rec = next(r for r in rows if r["method"] == method)
    return rec, bakeoff.choose(cfg, TASK, f"ensemble:{rec['ensemble']['id']}")


@pytest.mark.parametrize("method", core.ENSEMBLE_METHODS)
def test_the_backend_at_night_is_what_was_evaluated(tmp_path, no_sink, method):
    with _env(tmp_path):
        cfg, src, reqs, fakes = _scored(tmp_path)
        rec, entry = _chosen(cfg, src, method)
        assert entry["backend"] == "ensemble" and entry["ensemble"] == rec["ensemble"]
        assert entry["model_rev"] == rec["model_rev"]
        choice = core.load_choice(TASK)
        assert choice.backend == "ensemble" and choice.ensemble == rec["ensemble"]
        by_id = {f.model_id: f for f in fakes}
        b = backends.Ensemble(choice, lambda c: by_id[c.model_id])
        s, _pv = ensemble.load_scores(cfg, TASK, 90, src, TODAY)
        fits = ensemble._fit_members(s)
        order = [i for i, x in enumerate(s.recs) if x["model_id"] in rec["members"]]
        q = ensemble._params(s, fits[evalset.FOLDS], order, method)
        items = [r for r in reqs.values() if not evalset.final_test(r)]
        for j, r in enumerate(items):
            want = backends.combine(method, [fits[evalset.FOLDS].cal[m][j] for m in order], **q)
            got = b.distribution(r)
            assert got == pytest.approx(want)
        # run.py's decide applies the output calibration and tau exactly as tune saved them
        res = core.decide(items[0], b.distribution(items[0]), choice, b.model_id, b.model_rev)
        assert res.model_id == rec["model_id"] and res.model_rev == rec["model_rev"]
        assert res.calib_ver == rec["calib_ver"]


def test_make_backend_builds_it_and_it_fits_the_protocol(tmp_path, no_sink):
    with _env(tmp_path):
        cfg, src, _reqs, _fakes = _scored(tmp_path)
        _rec, _entry = _chosen(cfg, src)
        b = backends.make_backend(TASK, core.load_choice(TASK), "unused")
        assert isinstance(b, backends.Ensemble) and b.backend == "ensemble"
        assert [m.model_id for m in b.members] == [x["model_id"] for x in b.recipe["members"]]
        assert b.size_mb is None and not isinstance(b, core.NoteBackend)
        with pytest.raises(backends.BackendUnavailable, match="records no ensemble"):
            backends.make_backend(TASK, core.Choice(backend="ensemble", model_id="x"), "unused")


def test_a_missing_member_fails_clearly_before_anything_loads(tmp_path):
    loaded = []

    class Here(Noisy):
        def prepare(self):
            pass

        def distribution(self, req):
            loaded.append(self.model_id)
            return super().distribution(req)

    class Gone(Here):
        def prepare(self):
            raise backends.BackendUnavailable("label: not in the cache; fetch it with `hf download x`")

    recipe = {"id": "soft-1", "method": "soft", "weights": None, "stack": None, "members": [
        {"backend": "mlx_letter", "model_id": f"fake/{n}", "model_rev": "r1", "temperature": 1.0}
        for n in "abc"]}
    members = {"fake/a": Here("a"), "fake/b": Gone("b"), "fake/c": Here("c")}
    b = backends.Ensemble(core.Choice(backend="ensemble", ensemble=recipe),
                          lambda c: members[c.model_id])
    t = tasks.TASKS[TASK]
    req = t.request("steer", "u", NOW, {"text": LABELS[0]})
    with pytest.raises(backends.BackendUnavailable, match="member fake/b: .*hf download x"):
        b.distribution(req)
    assert not loaded
    # another revision on disk than was chosen: the same clear failure
    members["fake/b"] = Here("b")
    members["fake/b"].model_rev = "r2"
    b = backends.Ensemble(core.Choice(backend="ensemble", ensemble=recipe),
                          lambda c: members[c.model_id])
    with pytest.raises(backends.BackendUnavailable, match="revision r2, chosen at r1"):
        b.distribution(req)


def test_a_nightly_run_reports_a_missing_member_and_goes_on(tmp_path, monkeypatch, capsys):
    class Gone(Noisy):
        def prepare(self):
            raise backends.BackendUnavailable("label: not fetched")

    gone = core.Choice(backend="ensemble", model_id="ensemble/x", model_rev="r", ensemble={
        "id": "x", "method": "soft", "weights": None, "stack": None,
        "members": [{"backend": "mlx_letter", "model_id": "fake/gone", "model_rev": "r1",
                     "temperature": 1.0}]})
    monkeypatch.setattr(run, "make_backend", lambda *a: Gone("gone"))
    monkeypatch.setattr(run, "load_choice",
                        lambda name: gone if name == TASK else core.Choice())
    with _env(tmp_path):
        cfg, src, _reqs = _gold(tmp_path, LABELS)
        report = run.run_label(cfg, [TASK, "work_category"], dry_run=True, src=src, now=BASE)
    assert report[TASK]["failed"] == 1 and not report["work_category"]["failed"]
    assert "ensemble x: member fake/gone: label: not fetched" in capsys.readouterr().err


def test_members_load_once_across_tasks_and_close_with_the_run(tmp_path, monkeypatch):
    built, closed = [], []

    class Member(Noisy):
        def close(self):
            closed.append(self.model_id)

    def make(task, c, m, llama):
        built.append(c.model_id)
        return Member(c.model_id.split("/")[1])

    recipes = {
        t: {"id": f"soft-{t}", "method": "soft", "weights": None, "stack": None, "members": [
            {"backend": "mlx_letter", "model_id": f"fake/{n}", "model_rev": "r1", "temperature": 1.0}
            for n in "abc"]} for t in (TASK, "work_category")}
    monkeypatch.setattr(run, "make_backend", make)
    monkeypatch.setattr(run, "load_choice", lambda name: core.Choice(
        backend="ensemble", model_id=f"ensemble/soft-{name}", ensemble=recipes[name]))
    cfg = _cfg(tmp_path)
    with _env(tmp_path):
        run.run_label(cfg, list(recipes), dry_run=True, src=FakeSource(cfg), now=NOW)
    assert sorted(built) == ["fake/a", "fake/b", "fake/c"]
    assert closed.count("fake/a") >= 1


def test_every_mlx_member_loads_once_under_the_cache_cap(tmp_path, monkeypatch):
    caps, loads = [], []
    mlx = SimpleNamespace(set_cache_limit=caps.append)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mlx))
    monkeypatch.setitem(sys.modules, "mlx.core", mlx)
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(
        load=lambda p: loads.append(p) or ("m", "t")))

    class Member(backends.MlxLetter):
        def prepare(self):
            return tmp_path

        def distribution(self, req):
            self._load()
            return {c: 1 / len(LABELS) for c in LABELS}

    recipe = {"id": "soft-1", "method": "soft", "weights": None, "stack": None, "members": [
        {"backend": "mlx_letter", "model_id": f"fake/{n}", "model_rev": "r1", "temperature": 1.0}
        for n in "abc"]}
    b = backends.Ensemble(core.Choice(backend="ensemble", ensemble=recipe),
                          lambda c: Member(c.model_id, c.model_rev))
    req = tasks.TASKS[TASK].request("steer", "u", NOW, {"text": LABELS[0]})
    for _ in range(3):
        b.distribution(req)
    assert len(loads) == 3 and caps == [backends.MLX_CACHE_LIMIT] * 3


def test_a_broken_ensemble_entry_warns_and_runs_uncalibrated(tmp_path, capsys):
    with _env(tmp_path):
        core.choice_path().parent.mkdir(parents=True, exist_ok=True)
        m = {"backend": "mlx_letter", "model_id": "m", "model_rev": "r", "temperature": 1.0}
        for bad in ({"id": "x", "method": "psychic", "members": [m]},
                    {"id": "x", "method": "soft", "members": []},
                    {"id": "x", "method": "weighted", "weights": [1.0], "members": [m, m]},
                    {"id": "x", "method": "stack", "members": [m]},
                    {"id": "x", "method": "soft", "members": [{"model_id": "m", "temperature": 1.0}]},
                    {"id": "x", "method": "soft", "members": [{**m, "model_rev": None}]}):
            core.choice_path().write_text(json.dumps({"tasks": {TASK: {
                "backend": "ensemble", "ensemble": bad}}}))
            assert core.load_choice(TASK) == core.Choice()
            assert "the ensemble of" in capsys.readouterr().err


# ---- the final test days ----

def test_final_is_only_for_the_chosen_ensemble_and_only_one(tmp_path, no_sink, capsys):
    with _env(tmp_path):
        cfg, src, reqs, fakes = _scored(tmp_path)
        rows, _sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)
        first, second = [r for r in rows if r["method"] in ("soft", "majority")][:2]
        a, b = (f"ensemble:{r['ensemble']['id']}" for r in (first, second))
        by_id = {f.model_id: f for f in fakes}

        def backend(name):
            return backends.Ensemble(core.load_choice(TASK), lambda c: by_id[c.model_id])

        with pytest.raises(AutError, match="is not steer_intent's choice"):
            bakeoff.run_final(cfg, TASK, a, src=src, now=BASE, backend=object())
        bakeoff.choose(cfg, TASK, a)
        res = bakeoff.run_final(cfg, TASK, a, src=src, now=BASE, backend=backend(a))
        sealed = sum(evalset.final_test(q) for q in reqs.values())
        assert res["n"] == sealed and res["backend"] == "ensemble"
        assert "floors missed" in bakeoff.final_line(res)
        final_rows = evalset.read_jsonl(evalset.eval_path(TASK, "final"))
        assert len(final_rows) == 1 and WORD not in json.dumps(final_rows)
        # a second ensemble there would be a selection on the final days
        bakeoff.choose(cfg, TASK, b)
        with pytest.raises(AutError, match="already saw"):
            bakeoff.run_final(cfg, TASK, b, src=src, now=BASE, backend=backend(b))
        # the same one again: the warning single models get, and saved scores only
        bakeoff.choose(cfg, TASK, a)
        calls = [f.calls for f in fakes]
        capsys.readouterr()
        bakeoff.run_final(cfg, TASK, a, src=src, now=BASE, backend=backend(a))
        assert [f.calls for f in fakes] == calls
        assert "has seen the final test set 1 times" in capsys.readouterr().err


def _refit(task, rec):
    """What `aut label ensemble` writes after new gold moved a fit: same id, another revision."""
    recipe = {**rec["ensemble"], "members": [{**m, "temperature": m["temperature"] + 0.5}
                                             for m in rec["ensemble"]["members"]]}
    new = {**rec, "ensemble": recipe, "model_rev": backends.ensemble_rev(recipe)}
    evalset.append_jsonl(evalset.eval_path(task, "ensemble"), new)
    return new


def test_a_refit_under_the_same_id_is_not_the_choice_nor_a_second_look(tmp_path, no_sink):
    with _env(tmp_path):
        cfg, src, _reqs, fakes = _scored(tmp_path)
        rows, _sel = ensemble.run_ensemble(cfg, TASK, src=src, now=BASE)
        rec = next(r for r in rows if r["method"] == "soft")
        model = f"ensemble:{rec['ensemble']['id']}"
        by_id = {f.model_id: f for f in fakes}

        def backend():
            return backends.Ensemble(core.load_choice(TASK), lambda c: by_id[c.model_id])

        bakeoff.choose(cfg, TASK, model)
        new = _refit(TASK, rec)
        assert new["ensemble"]["id"] == rec["ensemble"]["id"] and new["model_rev"] != rec["model_rev"]
        # label.json holds the old fit: --final must not score the new one under its name
        with pytest.raises(AutError, match="refit it"):
            bakeoff.run_final(cfg, TASK, model, src=src, now=BASE, backend=object())
        # the same revision with another threshold is another decision too
        changed = json.loads(json.dumps(new))
        changed["per_label"][LABELS[0]]["tau"] = 0.123
        bakeoff.choose(cfg, TASK, model)
        evalset.append_jsonl(evalset.eval_path(TASK, "ensemble"), changed)
        with pytest.raises(AutError, match="refit it"):
            bakeoff.run_final(cfg, TASK, model, src=src, now=BASE, backend=object())
        bakeoff.choose(cfg, TASK, model)
        res = bakeoff.run_final(cfg, TASK, model, src=src, now=BASE, backend=backend())
        assert res["model_rev"] == new["model_rev"]
        # once those days have seen it, a refit under the same id is a second look
        _refit(TASK, changed)
        bakeoff.choose(cfg, TASK, model)
        with pytest.raises(AutError, match="already saw"):
            bakeoff.run_final(cfg, TASK, model, src=src, now=BASE, backend=object())


def test_every_member_closes_when_one_raises(tmp_path):
    closed = []

    class Member(Noisy):
        def close(self):
            closed.append(self.name)
            if self.name == "b":
                raise OSError("stuck")

    recipe = {"id": "soft-1", "method": "soft", "weights": None, "stack": None, "members": [
        {"backend": "mlx_letter", "model_id": f"fake/{n}", "model_rev": "r1", "temperature": 1.0}
        for n in "abc"]}
    b = backends.Ensemble(core.Choice(backend="ensemble", ensemble=recipe),
                          lambda c: Member(c.model_id.split("/")[1]))
    with pytest.raises(OSError, match="stuck"):
        b.close()
    assert closed == ["a", "b", "c"]


def test_choose_names_a_missing_ensemble(tmp_path):
    with _env(tmp_path):
        with pytest.raises(AutError, match="no record of ensemble:nope"):
            bakeoff.choose(_cfg(tmp_path), TASK, "ensemble:nope")


# ---- the CLI and the config ----

def test_cli_runs_the_table_and_loads_no_model(tmp_path, monkeypatch, no_sink, capsys):
    with _env(tmp_path):
        cfg, src, _reqs, _fakes = _scored(tmp_path)
        real = ensemble.run_ensemble
        seen = {}

        def run_with_src(cfg, task, days, models=None):
            seen.update(days=days, models=models)
            return real(cfg, task, days, src=src, now=BASE, models=models)

        monkeypatch.setattr(ensemble, "run_ensemble", run_with_src)
        args = cli._build_parser().parse_args(["label", "ensemble", "--task", TASK, "--days", "90",
                                               "--models", "fake/a,fake/b,fake/c"])
        cli.cmd_label(cfg, args)
        assert seen == {"days": 90, "models": ["fake/a", "fake/b", "fake/c"]}
        out = capsys.readouterr().out
        assert "label ensemble steer_intent" in out and "selection:" in out
        assert "recommended:" in out


def test_extra_models_become_candidates_and_a_bad_list_is_an_error(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('label_extra_models = ["org/more"]\n')
    monkeypatch.setenv("AUT_CONFIG", str(p))
    cfg = load_config()
    assert cfg.label_extra_models == ("org/more",)

    class Ready:
        pass
    built = []
    monkeypatch.setattr(bakeoff, "make_backend", lambda task, c, m, llama: built.append(c.model_id)
                        or Ready())
    with _env(tmp_path):
        bakeoff.default_backends(_cfg(tmp_path, label_extra_models=("org/more",)), TASK)
    assert "org/more" in built
    p.write_text('label_extra_models = "org/more"\n')
    monkeypatch.setenv("AUT_CONFIG", str(p))
    with pytest.raises(AutError, match="label_extra_models"):
        load_config()
