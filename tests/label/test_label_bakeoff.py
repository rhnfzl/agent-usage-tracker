"""Hermetic tests for label/bakeoff.py: `aut label eval`, `choose` and `train-encoder`. No model.

Locks the rules:
  - raw scores are saved without text and re-used: a rerun calls the model only for new gold;
  - the bake-off tunes on the training folds and reports out of fold; the final test days reach
    nothing but `eval --final`; the precision and recall floors are never relaxed; calibration is
    kept only when it lowers the out-of-fold Brier; the record has the fields agents.eval_latest
    reads, and no text; the table marks the Pareto frontier and failures, and only choose --model
    writes label.json;
  - the encoder trains on gold + silver of its own days, never on a final test day or a day eval
    calibrates it with, and its file holds no word.
"""

from __future__ import annotations

import json
import math
from datetime import timedelta

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.label import backends
from agent_usage_tracker.label import bakeoff
from agent_usage_tracker.label import core
from agent_usage_tracker.label import evalset
from agent_usage_tracker.label import tasks
from label_helpers import BASE, FIXED, LABELS, Oracle, TASK, TODAY, WORD, _cfg, _env, _gold


@pytest.fixture
def no_sink(monkeypatch):
    monkeypatch.setattr(bakeoff, "send_activity_rows", lambda dsn, rows: None)


def test_metrics():
    assert bakeoff.kappa([("a", "a"), ("b", "b")]) == 1.0
    assert bakeoff.kappa([("a", "b"), ("b", "a")]) == -1.0
    assert bakeoff.ece([(0.9, True)] * 9 + [(0.9, False)]) < 1e-9
    assert abs(bakeoff.ece([(1.0, False)] * 4) - 1.0) < 1e-9
    assert 0.83 < bakeoff.wilson_lb(29, 29) < 0.9 and bakeoff.wilson_lb(0, 0) is None


class Table:
    """A backend answering from a function of the request; counts its calls."""
    backend, size_mb = "mlx_letter", None

    def __init__(self, fn, model_id="fake/table", model_rev="r1"):
        self.fn, self.model_id, self.model_rev, self.calls = fn, model_id, model_rev, []

    def distribution(self, req):
        self.calls.append(req.unit_id)
        return self.fn(req)


def _items(n_days=60, per_day=3, task=TASK, unit="steer"):
    """Items on fixed days outside the final test; gold cycles the candidates.
    A unit_id carries its gold and its index."""
    t, labels = tasks.TASKS[task], [c for c, _d in tasks.TASKS[task].candidates]
    out = []
    for i in range(n_days * per_day * 2):
        g = labels[i % len(labels)]
        req = t.request(unit, f"{g}#{i}", FIXED - timedelta(days=i // per_day), {})
        if not evalset.final_test(req):
            out.append(bakeoff._Item(req, g, 1.0))
    return out[:n_days * per_day]


def _sure(p=0.9, labels=LABELS):
    """Answers the unit's own gold with probability p."""
    def fn(req):
        g = req.unit_id.split("#")[0]
        return {c: p if c == g else (1 - p) / (len(labels) - 1) for c in labels}
    return fn


def _rec(tmp_path, b, items=None, precision=0.9, recall=0.6, task=TASK):
    with _env(tmp_path):
        return bakeoff.evaluate(task, b, items or _items(), precision, recall, TODAY)


def test_eval_record_choose_and_event(tmp_path, monkeypatch):
    golds = LABELS * 20
    sent = []
    monkeypatch.setattr(bakeoff, "send_activity_rows", lambda dsn, rows: sent.extend(rows))
    with _env(tmp_path):
        cfg, src, reqs = _gold(tmp_path, golds)
        recs = bakeoff.run_eval(cfg, TASK, src=src, now=BASE,
                                backends=[Oracle(size_mb=4000), Oracle("stop", size_mb=10)])
        good, bad = recs
        final = sum(evalset.final_test(q) for q in reqs.values())
        assert 0 < final and good["n"] == len(golds) - final == good["n_cal"] == good["n_held"]
        assert sum(good["folds"].values()) == good["n"] and len(good["folds"]) == evalset.FOLDS
        assert good["macro_f1"] == good["macro_f1_tuned"] == 1.0 and good["kappa"] == 1.0
        assert good["coverage"] == 1.0 and good["no_threshold_folds"] == {}
        assert good["calibration"] == "temperature" and good["temperature"] < 1
        assert not good["platt"]
        assert good["ece_cal"] <= good["ece_raw"] and good["brier"] < 0.01 and good["nll"] < 0.05
        assert bad["macro_f1"] < 0.2 and bad["kappa"] <= 0
        assert set(good["per_label"]) == set(LABELS)
        for v in good["per_label"].values():
            assert {"n", "precision", "recall", "f1", "tau", "coverage", "precision_at_tau",
                    "recall_at_tau", "precision_default", "recall_default",
                    "share_error_pp"} <= set(v)
            assert v["share_error_pp"] == 0.0 and v["tau"] >= core.TAU_FLOOR
            assert v["recall_at_tau"] == 1.0
        assert [e.kind for e in sent] == ["eval", "eval"] and sent[0].attrs == good
        assert sent[0].agent == "label" and sent[0].machine == "test"
        blob = json.dumps([e.attrs for e in sent])
        assert WORD not in blob and not any(u in blob for u in reqs)     # no unit ids, no text
        assert WORD not in evalset.eval_path(TASK, "eval").read_text()
        assert len(evalset.read_jsonl(evalset.eval_path(TASK, "eval"))) == 2
        assert bakeoff._fails(good, cfg) == [] and bakeoff._fails(bad, cfg)
        # no model named: nothing is written, whatever passes
        assert bakeoff.choose(cfg, TASK) is None and not core.choice_path().exists()
        # the owner's pick reads back
        win = bakeoff.choose(cfg, TASK, "fake/oracle")
        assert win["model_id"] == "fake/oracle" and win["temperature"] == good["temperature"]
        c = core.load_choice(TASK)
        assert c.tau == {k: v["tau"] for k, v in good["per_label"].items()}
        assert c.calib_ver == good["calib_ver"] and c.model_rev == "r1" and c.platt is None
        with pytest.raises(AutError, match="no eval record"):
            bakeoff.choose(cfg, TASK, "fake/unknown")
        assert core.load_choice(TASK).model_rev == "r1"
        evalset.eval_path(TASK, "gold").unlink()
        with pytest.raises(AutError):
            bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[Oracle()])


def test_scores_are_saved_without_text_and_reused(tmp_path, monkeypatch, no_sink):
    golds = LABELS * 20
    with _env(tmp_path):
        cfg, src, reqs = _gold(tmp_path, golds)
        o = Oracle()
        first = bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[o])[0]
        open_units = [u for u, q in reqs.items() if not evalset.final_test(q)]
        assert o.calls == 2 * len(open_units)                       # two candidate orders per unit
        files = list((evalset.eval_path(TASK, "eval").parent / "scores").iterdir())
        assert len(files) == 1 and files[0].name.startswith(f"{TASK}.fake-oracle.")
        rows = evalset.read_jsonl(files[0])
        assert {r["unit_id"] for r in rows} == set(open_units)
        assert all(set(r) == {"unit_id", "dist"} for r in rows)
        assert WORD not in files[0].read_text()
        # a rerun loads no model and gives the same numbers, the speed of the first run included
        o2 = Oracle()
        again = bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[o2])[0]
        assert o2.calls == 0 and again == first
        # one new gold unit: only that one is scored
        u = open_units[0]
        kept = evalset.read_jsonl(files[0])
        evalset.write_jsonl(files[0], [r for r in kept if r["unit_id"] != u])
        o3 = Oracle()
        bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[o3])
        assert o3.calls == 2
        # a new revision or prompt is another file and scores again
        o4 = Oracle()
        o4.model_rev = "r2"
        bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[o4])
        assert o4.calls == 2 * len(open_units)
        monkeypatch.setattr(core, "PROMPT_VER", core.PROMPT_VER + 1)
        o5 = Oracle()
        rec = bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[o5])[0]
        assert o5.calls == 2 * len(open_units) and rec["prompt_ver"].endswith("p3")
        keys = set(bakeoff.newest(TASK))
        assert keys == {("fake/oracle", "r1", "steer_intent@2/p2"),
                        ("fake/oracle", "r2", "steer_intent@2/p2"),
                        ("fake/oracle", "r1", "steer_intent@2/p3")}


def test_final_test_days_reach_only_eval_final(tmp_path, capsys, no_sink):
    golds = LABELS * 20
    with _env(tmp_path):
        cfg, src, reqs = _gold(tmp_path, golds)
        sealed = {u for u, q in reqs.items() if evalset.final_test(q)}
        assert sealed
        with pytest.raises(AutError, match="no eval record"):
            bakeoff.run_final(cfg, TASK, "fake/oracle", src=src, now=BASE, backend=Oracle())
        seen = []

        class Spy(Oracle):
            def distribution(self, req):
                seen.append(req.unit_id)
                return super().distribution(req)
        rec = bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[Spy()])[0]
        assert seen and not sealed & set(seen)
        # changing every final gold answer changes nothing the bake-off reports
        for u in sealed:
            evalset.append_jsonl(evalset.eval_path(TASK, "gold"), {
                "unit_id": u, "gold": "other", "labeller": "human", "date": "d", "task_ver": 2})
        assert bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[Oracle()])[0] == rec
        # eval, the table and choose never open the final file: a broken one cannot matter
        evalset.eval_path(TASK, "final").mkdir(parents=True)
        assert bakeoff.choose(cfg, TASK) is None and bakeoff.choose(cfg, TASK, "fake/oracle")
        bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[Oracle()])
        evalset.eval_path(TASK, "final").rmdir()
        capsys.readouterr()
        # --final scores those days only, with the saved calibration, and warns on thin gold
        seen.clear()
        res = bakeoff.run_final(cfg, TASK, "fake/oracle", src=src, now=BASE, backend=Spy())
        assert set(seen) == sealed and res["n"] == len(sealed)
        assert "too small to mean much" in capsys.readouterr().err
        assert set(res["per_label"]["other"]) >= {"precision_at_tau", "recall_at_tau"}
        assert res["fails"] and "floors missed" in bakeoff.final_line(res)    # answers now all wrong
        rows = evalset.read_jsonl(evalset.eval_path(TASK, "final"))
        assert len(rows) == 1 and WORD not in json.dumps(rows)
        assert not any(u in json.dumps(rows) for u in reqs)
        # the second look is saved scores, and it says so
        seen.clear()
        bakeoff.run_final(cfg, TASK, "fake/oracle", src=src, now=BASE, backend=Spy())
        assert not seen and "has seen the final test set 1 times" in capsys.readouterr().err
        # and it never changed what the bake-off holds
        assert bakeoff.newest(TASK)[("fake/oracle", "r1", "steer_intent@2/p2")]["n"] == rec["n"]


def test_recall_floor_fails_a_model_and_is_never_relaxed(tmp_path):
    def half_stop(req):            # sure of every label, but half of `stop` goes to `other`
        g, i = req.unit_id.split("#")
        if g == "stop" and int(i) % 2 == 0:
            return {c: 0.55 if c == "other" else 0.3 if c == "stop" else 0.15 / 7 for c in LABELS}
        return {c: 0.92 if c == g else 0.08 / 8 for c in LABELS}
    rec = _rec(tmp_path, Table(half_stop))
    stop = rec["per_label"]["stop"]
    assert stop["tau"] is None and stop["recall_at_tau"] < 0.6        # the saved tau never passes
    assert any("stop" in v for v in rec["no_threshold_folds"].values())
    assert bakeoff.failures(rec, 0.9, 0.6) == [f"stop recall {stop['recall_at_tau']}"]
    assert stop["recall"] < 0.6                       # its best: the floor is not reachable
    # the same scores under a floor they can reach: only the floor made the difference
    easy = _rec(tmp_path / "b", Table(half_stop), recall=0.3)
    assert easy["per_label"]["stop"]["tau"] is not None
    assert bakeoff.failures(easy, 0.9, 0.3) == [] and easy["no_threshold_folds"] == {}
    # a precision floor fails too
    assert bakeoff.failures(rec, 1.0, 0.0) == [] and bakeoff.failures(
        {"per_label": {"a": {"precision_at_tau": 0.5, "recall_at_tau": None}}}, 0.9, 0.6) == [
        "a precision 0.5"]
    # no threshold on the training folds: fit_tau refuses, whatever precision alone says
    items = [("a", 0.9, "a")] * 9 + [("b", 0.9, "a")] * 9
    assert core.fit_tau(items, 0.9)["a"] == 0.9 and core.fit_tau(items, 0.9, recall=0.6)["a"] is None


def _binary(tmp_path, fn):
    t = "aw_title_relevance"
    return _rec(tmp_path, Table(fn), _items(task=t, unit="aw_title"), 0.8, 0.6, t)


def test_calibration_is_kept_only_when_it_helps(tmp_path):
    # already calibrated: 80% of the units sure at 0.8 are right, per fold about the same
    def honest(req):
        g, i = req.unit_id.split("#")
        o = LABELS[(LABELS.index(g) + 1) % len(LABELS)]
        hit = int(i) % 5 != 0
        rest = 0.2 / (len(LABELS) - 1)
        return {c: 0.8 if c == (g if hit else o) else rest for c in LABELS}
    rec = _rec(tmp_path, Table(honest))
    assert rec["calibration"] == "none" and rec["temperature"] == 1.0 and rec["platt"] is None
    assert set(rec["brier_by_calibration"]) == {"none", "temperature"}
    # overconfident: the temperature lowers the out-of-fold Brier, and T > 1 is saved
    def over(req):
        g, i = req.unit_id.split("#")
        o = LABELS[(LABELS.index(g) + 1) % len(LABELS)]
        rest = 0.01 / (len(LABELS) - 1)
        return {c: 0.99 if c == (g if int(i) % 3 else o) else rest for c in LABELS}
    rec = _rec(tmp_path / "b", Table(over))
    assert rec["calibration"] == "temperature" and rec["temperature"] > 1
    b = rec["brier_by_calibration"]
    assert b["temperature"] < b["none"] and rec["brier"] == b["temperature"]
    # a yes/no model biased low: nothing "relevant" reaches 0.5, Platt on log-odds repairs it
    def low(req):
        p = 0.4 if req.unit_id.startswith("relevant") else 0.1
        return {"relevant": p, "not_relevant": 1 - p}
    rec = _binary(tmp_path / "c", low)
    b = rec["brier_by_calibration"]
    assert rec["calibration"] == "platt" and b["platt"] < b["temperature"] <= b["none"]
    assert rec["platt"]["yes"] == "relevant" and rec["temperature"] == 1.0
    assert rec["macro_f1_tuned"] == 1.0 and rec["macro_f1_default"] < 0.5
    assert rec["per_label"]["relevant"]["recall_default"] == 0.0
    # what won is saved, and the nightly decision applies it
    with _env(tmp_path / "c"):
        evalset.append_jsonl(evalset.eval_path("aw_title_relevance", "eval"), {"ts": "t", **rec})
        cfg = _cfg(tmp_path)
        win = bakeoff.choose(cfg, "aw_title_relevance", "fake/table")
        assert win["platt"] == rec["platt"]
        c = core.load_choice("aw_title_relevance")
        req = tasks.TASKS["aw_title_relevance"].request("aw_title", "k", FIXED, {})
        res = core.decide(req, {"relevant": 0.4, "not_relevant": 0.6}, c, "m", "r")
        assert res.chosen == "relevant" and not res.below_tau and res.prob > 0.9
        assert abs(sum(res.dist.values()) - 1) < 1e-9


def test_report_numbers_speed_and_memory(tmp_path, monkeypatch, no_sink):
    assert bakeoff.brier([({"a": 1.0, "b": 0.0}, "a")]) == 0.0
    assert bakeoff.brier([({"a": 0.0, "b": 1.0}, "a")]) == 2.0
    assert abs(bakeoff.nll([({"a": 0.5, "b": 0.5}, "a")]) - math.log(2)) < 1e-9
    rel = bakeoff.reliability([(i / 45, i % 2 == 0) for i in range(45)])
    assert [g["n"] for g in rel] == [20, 25] and rel[0]["p"] < rel[1]["p"]
    assert [g["n"] for g in bakeoff.reliability([(0.5, True)] * 60)] == [20, 20, 20]
    assert bakeoff.reliability([(0.5, True)] * 3) == [{"n": 3, "p": 0.5, "right": 1.0}]
    clock = {"t": 0.0}
    monkeypatch.setattr(bakeoff.time, "monotonic", lambda: clock["t"])

    class Timed(Table):
        closed = 0

        def distribution(self, req):
            clock["t"] += 10.0 if not self.calls else 1.0      # the first call loads the model
            return super().distribution(req)

        def peak_memory_mb(self):
            return 1234

        def close(self):
            Timed.closed += 1
    rec = _rec(tmp_path, Timed(_sure()))
    assert rec["load_s"] == 10.0 and rec["s_per_decision"] == 1.0 and rec["peak_mb"] == 1234
    assert rec["n_timed"] == rec["n"] - 1
    assert sum(g["n"] for g in rec["reliability_cal"]) == rec["n"]
    assert sum(g["n"] for g in rec["reliability_raw"]) == rec["n"]
    assert 19 <= rec["reliability_raw"][0]["n"] <= 30 or len(rec["reliability_raw"]) == 1
    # no method for it: null. A backend that is not called reports nothing new
    plain = _rec(tmp_path / "p", Table(_sure()))
    assert plain["peak_mb"] is None and plain["size_mb"] is None
    with _env(tmp_path):
        again = bakeoff.evaluate(TASK, Timed(_sure()), _items(), 0.9, 0.6, TODAY, prev=rec)
    assert again["load_s"] == 10.0 and again["peak_mb"] == 1234
    assert again["n_timed"] == rec["n_timed"]
    # at the default threshold next to the tuned one: a model sure at 0.4 is right but under 0.5
    shy = _rec(tmp_path / "s", Table(_sure(0.4)))
    assert shy["macro_f1_default"] == 0.0 and shy["macro_f1"] == 1.0
    assert shy["per_label"]["stop"]["recall_default"] == 0.0
    # run_eval closes each model, so the next one gets its own memory and peak
    with _env(tmp_path / "c"):
        cfg, src, _reqs = _gold(tmp_path / "c", LABELS * 20)
        Timed.closed = 0
        bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[Timed(_sure()), Timed(_sure(), "fake/two")])
    assert Timed.closed == 2


def _row(model, f1=0.9, s=1.0, mb=100.0, rev="r1", **v):
    return {"ts": "t", "task": TASK, "task_ver": 2, "backend": "mlx_letter", "model_id": model,
            "model_rev": rev, "prompt_ver": "steer_intent@2/p1", "calib_ver": "c",
            "calibration": "none", "temperature": 1.0, "platt": None, "n": 100,
            "macro_f1_tuned": f1, "macro_f1_default": 0.5,
            "brier": 0.2, "nll": 0.5, "coverage": 0.9, "s_per_decision": s, "load_s": 3.0,
            "peak_mb": mb, "per_label": {"stop": {
                "n": 40, "tau": 0.6, "precision_at_tau": 0.95, "recall_at_tau": 0.9,
                "share_error_pp": 1.0, **v}}}


def test_pareto_table_and_choose(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    big, fast = _row("m/big", 0.95, 5.0, 900.0), _row("m/fast", 0.8, 0.5, 900.0)
    small, beaten = _row("m/small", 0.8, 5.0, 90.0), _row("m/beaten", 0.8, 5.0, 900.0)
    unknown = _row("m/unknown", 0.7, 9.0, None)         # null memory: not compared on memory
    assert bakeoff.pareto([big, fast, small, beaten]) == [big, fast, small]
    assert bakeoff.pareto([beaten, unknown]) == [beaten]
    assert bakeoff.pareto([small, _row("m/null", 0.8, 5.0, None)]) == [
        small, _row("m/null", 0.8, 5.0, None)]
    failing = _row("m/fail", 0.99, 0.1, 10.0, recall_at_tau=0.2)
    old = {"task_ver": 2, "model_id": "m/old", "model_rev": "r", "prompt_ver": "p", "per_label": {},
           "macro_f1": 1.0, "n_held": 9, "coverage": 1.0, "size_mb": 1}       # before the folds
    with _env(tmp_path):
        for r in (old, big, fast, small, beaten, failing):
            evalset.append_jsonl(evalset.eval_path(TASK, "eval"), r)
        lines = bakeoff.table(cfg, TASK)
        mark = {ln[3:].split()[0]: ln[:2] for ln in lines if ln[3:5] == "m/"}
        assert mark == {"m/big": "* ", "m/fast": "* ", "m/small": "* ", "m/beaten": "  ",
                        "m/fail": " !"}
        assert "m/fail: stop recall 0.2" in lines[-1] and "m/old" not in "\n".join(lines)
        # no --model: the table and a recommendation, nothing written
        assert bakeoff.choose(cfg, TASK) is None
        out = capsys.readouterr().out
        assert "--model m/big" in out and "m/beaten" in out and not core.choice_path().exists()
        # the recommendation breaks a quality tie by speed
        assert bakeoff.recommend([small, fast], cfg)["model_id"] == "m/fast"
        # the owner may pick a failing model, and is told so
        win = bakeoff.choose(cfg, TASK, "m/fail")
        assert win["model_id"] == "m/fail" and "stop recall 0.2" in capsys.readouterr().err
        # two revisions keep their own rows, and the newest is what a model name means
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), _row("m/big", 0.9, rev="r2"))
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), _row("m/fast", 0.8, 0.5, 900.0))
        assert {k[1] for k in bakeoff.newest(TASK) if k[0] == "m/big"} == {"r1", "r2"}
        assert bakeoff.choose(cfg, TASK, "m/big")["model_rev"] == "r2"
        # a short name finds the model's repo, and the table and the advice show the short name
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"),
                             _row(backends.MODELS["kev-4b"].repo, 0.95, 0.1, 1.0))
        win = bakeoff.choose(cfg, TASK, "kev-4b")
        assert win["model_id"] == backends.MODELS["kev-4b"].repo
        assert any(x[3:].startswith("kev-4b ") for x in bakeoff.table(cfg, TASK))
        # nothing passes: nothing is recommended, and the floors stay
        evalset.eval_path(TASK, "eval").unlink()
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), failing)
        assert bakeoff.choose(cfg, TASK) is None and "no model passes" in capsys.readouterr().out
        evalset.eval_path(TASK, "eval").unlink()
        with pytest.raises(AutError, match="no eval record"):
            bakeoff.choose(cfg, TASK)


def test_backend_name_and_mlx_reports_memory_lazily():
    assert backends.Encoder.backend == "encoder" and backends.MlxLetter("m").backend == "mlx_letter"
    assert backends.MlxLetter("m", "abc").model_rev == "abc"
    backends.MlxLetter("m").close()                     # nothing loaded: mlx is not imported


def test_share_error_is_reweighted_to_natural_frequency(tmp_path):
    with _env(tmp_path):
        # silver says 9 of 10 units are `stop`, gold oversampled the rare label 1:1
        for i in range(10):
            evalset.append_jsonl(evalset.eval_path(TASK, "silver"), {
                "unit_id": f"p{i}", "teacher": "t", "task_ver": 2, "date": "d",
                "dist": {"stop": 0.9, "framing": 0.1} if i < 9 else {"framing": 0.9,
                                                                     "stop": 0.1}})
        for u, d in (("g1", "stop"), ("g2", "framing")):
            evalset.append_jsonl(evalset.eval_path(TASK, "silver"), {
                "unit_id": u, "teacher": "t", "task_ver": 2, "date": "d",
                "dist": {d: 0.9}})
        w = bakeoff.weights(TASK, ["g1", "g2", "g3"])
        # population stop 10 : framing 2; gold 1 : 1 (g3 has no silver: weight 1)
        assert abs(w["g1"] / w["g2"] - 5) < 1e-9 and w["g3"] == 1.0


def test_train_encoder_and_backend(tmp_path):
    golds = (["stop", "framing", "correction"] * 24)
    with _env(tmp_path):
        cfg, src, reqs = _gold(tmp_path, golds)
        with pytest.raises(backends.BackendUnavailable):
            backends.Encoder(TASK).distribution(next(iter(reqs.values())))
        r = bakeoff.train_encoder(cfg, TASK, src=src, now=BASE)
        # each steer also carries its neighbour's text, so a small fit is not perfect
        assert (r["trained_on"], r["held_out"]) == (30, 28)
        assert (r["accuracy"], r["macro_f1"]) == (0.9286, 0.928)
        sealed = {u for u, q in reqs.items() if evalset.final_test(q)}
        fit = {u for u, q in reqs.items() if u not in sealed and evalset.in_encoder_train_split(q)}
        assert sealed and 0 < len(fit) < len(reqs) - len(sealed) and r["trained_on"] == len(fit)
        assert r["held_out"] == len(reqs) - len(sealed) - len(fit)
        # silver of a final test day or an eval day never trains it
        for u in set(reqs) - fit:
            evalset.append_jsonl(evalset.eval_path(TASK, "silver"), {
                "unit_id": u, "teacher": "t", "task_ver": 2, "date": "d", "dist": {"stop": 1.0}})
        assert bakeoff.train_encoder(cfg, TASK, src=src, now=BASE)["trained_on"] == len(fit)
        # eval calibrates the encoder only on the gold it never trained on
        rec = bakeoff.evaluate(TASK, backends.Encoder(TASK), [
            bakeoff._Item(q, q.facts["text"].split()[0], 1.0)
            for u, q in reqs.items() if u not in sealed], 0.9, 0.6, TODAY)
        assert rec["n"] == rec["n_cal"] == len(reqs) - len(sealed) - len(fit)
        assert rec["backend"] == "encoder"
        assert bakeoff.evaluate(TASK, backends.Encoder(TASK), [
            bakeoff._Item(reqs[u], "stop", 1.0) for u in fit], 0.9, 0.6, TODAY) is None
        raw = backends.encoder_path(TASK).read_text()
        assert WORD not in raw and json.loads(raw)["labels"] == LABELS
        assert all(k.isdigit() for k in json.loads(raw)["w"])     # weights keyed by bucket ids
        enc = backends.make_backend(TASK, core.Choice(backend="encoder"), "unused")
        req = next(iter(reqs.values()))
        d = enc.distribution(req)
        assert max(d, key=d.get) == req.facts["text"].split()[0] and abs(sum(d.values()) - 1) < 1e-9
        assert enc.model_rev and enc.size_mb is not None
        with pytest.raises(backends.BackendUnavailable):            # retrained since chosen
            backends.make_backend(TASK, core.Choice(backend="encoder", model_rev="0" * 12), "x")
        req.task_ver = 3
        with pytest.raises(backends.BackendUnavailable):
            enc.distribution(req)
        assert any(isinstance(b, backends.Encoder) for b in
                   bakeoff.default_backends(_cfg(tmp_path), TASK))


def test_corrupt_choice_file_warns_and_choose_keeps_it(tmp_path, monkeypatch, capsys):
    p = tmp_path / "label.json"
    monkeypatch.setattr(core, "choice_path", lambda: p)
    assert core.load_choice(TASK) == core.Choice() and capsys.readouterr().err == ""    # missing
    for bad in ("{not json", "[1]", '{"tasks": 5}'):
        p.write_text(bad)
        assert core.load_choice(TASK) == core.Choice()
        assert str(p) in capsys.readouterr().err
    p.write_text("{not json")
    monkeypatch.setattr(bakeoff, "read_jsonl", lambda _p: [_row("m")])
    with pytest.raises(AutError, match="unusable"):
        bakeoff.choose(_cfg(tmp_path), TASK, "m")
    assert p.read_text() == "{not json", "another task's choice must survive"


def _record(coverage=1.0, **per) -> dict:
    return {**_row("m", coverage=None), "coverage": coverage,
            "per_label": {"stop": {"tau": 0.7, "precision_at_tau": 0.95, "recall_at_tau": 0.9,
                                   "share_error_pp": 1.0},
                          "framing": {"tau": None, "precision_at_tau": None, "recall_at_tau": None,
                                      "share_error_pp": 2.0},
                          **per}}


def test_failures_name_every_part_of_the_bar():
    def fails(rec):
        return bakeoff.failures(rec, 0.9, 0.6, 0.75)
    assert fails(_record()) == []
    assert fails(_record(coverage=0.7)) == ["coverage 0.7"]
    assert fails({**_record(), "coverage": None}) == ["coverage None"]
    edge = {"tau": 0.7, "precision_at_tau": None, "recall_at_tau": None,
            "share_error_pp": bakeoff.SHARE_ERROR_PP}
    assert fails(_record(other=edge)) == []
    assert fails(_record(other={**edge, "share_error_pp": 5.01})) == ["other share error 5.01"]
    # nothing passed anywhere (share error unknown) is coverage 0: the bar still fails
    assert fails({**_record(coverage=0.0), "per_label": {"other": {**edge, "share_error_pp": None}}})
    assert fails(_record(other={**edge, "precision_at_tau": 0.89})) == ["other precision 0.89"]
    assert fails(_record(other={**edge, "recall_at_tau": 0.59})) == ["other recall 0.59"]


def test_wilson_lb_known_values():
    assert abs(bakeoff.wilson_lb(0, 10)) < 1e-12
    assert abs(bakeoff.wilson_lb(50, 100) - 0.4038) < 1e-3
    assert bakeoff.wilson_lb(9, 10) < 0.9                     # a bound below the plain share


def test_kappa_known_value_and_edges():
    assert bakeoff.kappa([]) is None
    assert bakeoff.kappa([("a", "a")] * 3) == 1.0              # one label only: chance agreement 1
    # agreement 0.75, chance (3*2 + 1*2) / 16 = 0.5
    assert bakeoff.kappa([("a", "a"), ("a", "a"), ("a", "b"), ("b", "b")]) == 0.5


def test_ece_weighs_each_bin_by_its_items():
    assert bakeoff.ece([]) is None
    assert abs(bakeoff.ece([(0.95, True), (0.55, False)]) - 0.3) < 1e-9      # (0.05 + 0.55) / 2


def test_label_metrics_of_a_label_with_items():
    top, gold_ = ["a", "a", "b"], {0: "a", 1: "b", 2: "b"}
    m = bakeoff._label_metrics("a", [0, 1, 2], top, gold_, [0, 2], {0: 1.0, 1: 1.0, 2: 1.0}, 0.6)
    assert (m["n"], m["precision"], m["recall"], m["f1"]) == (1, 0.5, 1.0, 0.6667)
    assert (m["tau"], m["coverage"], m["precision_at_tau"]) == (0.6, 0.5, 1.0)
    assert m["precision_lb"] == 0.2065
    assert m["share_error_pp"] == 16.6667       # 1 of 2 known items against 1 of 3 true ones


def test_label_metrics_of_a_label_nobody_chose_or_passed():
    m = bakeoff._label_metrics("c", [0], ["a"], {0: "a"}, [0], {0: 1.0}, None)
    assert (m["n"], m["precision"], m["recall"], m["f1"], m["coverage"]) == (0, None, None, None, None)
    assert (m["precision_at_tau"], m["precision_lb"], m["share_error_pp"]) == (None, None, 0.0)
    wrong = bakeoff._label_metrics("a", [0], ["b"], {0: "a"}, [], {0: 1.0}, None)
    assert wrong["f1"] == 0.0 and wrong["share_error_pp"] is None    # nothing passed: unknown share


def _choices(tmp_path, monkeypatch, records):
    monkeypatch.setattr(core, "choice_path", lambda: tmp_path / "label.json")
    monkeypatch.setattr(bakeoff, "choice_path", lambda: tmp_path / "label.json")
    for r in records:
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), r)


def test_choose_writes_the_named_model_and_keeps_other_tasks(tmp_path, monkeypatch):
    with _env(tmp_path):
        _choices(tmp_path, monkeypatch, [_row("big", 0.95), _row("small", 0.9)])
        (tmp_path / "label.json").write_text('{"tasks": {"other": {"backend": "x"}}}')
        win = bakeoff.choose(_cfg(tmp_path), TASK, "small")
        assert win["model_id"] == "small" and win["tau"] == {"stop": 0.6}
        saved = json.loads((tmp_path / "label.json").read_text())
        assert saved["tasks"]["other"] == {"backend": "x"} and saved["tasks"][TASK] == win


def test_choose_reads_only_the_newest_record_of_a_model(tmp_path, monkeypatch):
    with _env(tmp_path):
        _choices(tmp_path, monkeypatch, [_row("m", 0.9), {**_row("m", 0.8), "temperature": 2.0}])
        assert bakeoff.choose(_cfg(tmp_path), TASK, "m")["temperature"] == 2.0
        # and a bad newest record is what the recommendation judges
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), _row("m", recall_at_tau=0.1))
        assert bakeoff.recommend(list(bakeoff.newest(TASK).values()), _cfg(tmp_path)) is None


def test_macro_f1_averages_the_labels_that_occur():
    assert bakeoff._macro_f1([], ["a"]) is None
    # a: 2 * 1 / (2 + 1) = 0.6667, b: 2 * 1 / (1 + 2) = 0.6667, c never occurs
    assert bakeoff._macro_f1([("a", "a"), ("a", "b"), ("b", "b")], ["a", "b", "c"]) == 0.6667


def test_fit_learns_a_separable_set_and_leaves_its_input_alone():
    data = [([1], [1.0, 0.0], 1.0), ([2], [0.0, 1.0], 1.0)]
    w = bakeoff._fit(data, 2)
    assert w[1][0] > w[1][1] and w[2][1] > w[2][0]
    assert data == [([1], [1.0, 0.0], 1.0), ([2], [0.0, 1.0], 1.0)]
    assert bakeoff._fit(data, 2) == w                        # a fixed seed: the same weights


def test_training_data_takes_only_what_it_is_given():
    labels = ["stop", "framing"]
    reqs = {f"u{i}": tasks.TASKS[TASK].request("steer", f"u{i}", BASE, {"text": f"t{i}"})
            for i in range(6)}
    train = {u: reqs[u] for u in ("u0", "u1", "u2", "u3")}
    gold_ = {"u0": "stop", "u1": "framing", "u4": "stop", "gone": "stop"}
    silver = {"u2": {"x": {"stop": 1.0}, "y": {"stop": 0.5, "framing": 0.5}},
              "u0": {"x": {"framing": 1.0}},       # has gold: the gold row stands alone
              "u3": {},
              "u5": {"x": {"stop": 1.0}}}          # another day: never trains it
    data = bakeoff._training_data(gold_, silver, train, labels)
    assert [(t, w) for _f, t, w in data] == [([1.0, 0.0], bakeoff.GOLD_WEIGHT),
                                             ([0.0, 1.0], bakeoff.GOLD_WEIGHT),
                                             ([0.75, 0.25], 1.0)]
    assert data[0][0] == backends.features(reqs["u0"].facts)


def test_gold_units_say_how_many_fell_out_of_the_window(tmp_path, capsys):
    with _env(tmp_path):
        cfg, src, reqs = _gold(tmp_path, ["stop", "framing"])
        evalset.append_jsonl(evalset.eval_path(TASK, "gold"), {
            "unit_id": "gone", "gold": "stop", "task_ver": tasks.TASKS[TASK].ver})
        pairs = bakeoff._gold_units(cfg, TASK, 90, src, TODAY)
        assert {r.unit_id for r, _g in pairs} == set(reqs)
        assert "1 gold units are outside the 90-day window" in capsys.readouterr().err
        for r, g in pairs:
            assert g == r.facts["text"].split()[0]


def test_a_model_that_cannot_run_is_left_out_and_the_others_go_on(tmp_path, capsys, no_sink):
    closed = []

    class Broken(Oracle):
        model_id = "fake/broken"

        def distribution(self, req):
            raise backends.BackendUnavailable("label: fake/broken could not be loaded (OSError)")

        def close(self):
            closed.append(self.model_id)

    class Absent(Oracle):
        model_id = "fake/absent"

        def prepare(self):
            raise backends.BackendUnavailable("label: fake/absent is not fully in the cache")

    with _env(tmp_path):
        cfg, src, _reqs = _gold(tmp_path, LABELS * 20)
        recs = bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[Broken(), Absent(), Oracle()])
    assert [r["model_id"] for r in recs] == ["fake/oracle"] and closed == ["fake/broken"]
    err = capsys.readouterr().err
    assert "fake/broken left out" in err and "fake/absent left out" in err


def test_default_backends_dedups_models_and_names_the_ones_left_out(tmp_path, monkeypatch, capsys):
    class Missing:
        def prepare(self):
            raise backends.BackendUnavailable("not on disk")

    table = {"one": backends.Model("mlx_letter", "repo/one"),
             "gone": backends.Model("mlx_letter", "repo/gone")}
    monkeypatch.setattr(bakeoff, "MODELS", table)
    monkeypatch.setattr(backends, "MODELS", table)
    monkeypatch.setattr(bakeoff, "make_backend", lambda task, c, fallback, llama: (
        Missing() if c.model_id == "gone" else c.model_id))
    with _env(tmp_path):
        # label_model given by repo is the table's row, not a second backend
        got = bakeoff.default_backends(_cfg(tmp_path, label_model="repo/one"), TASK)
        assert got == ["one"] and "gone left out: not on disk" in capsys.readouterr().err
        got = bakeoff.default_backends(_cfg(tmp_path, label_model="repo/own"), TASK)
    assert got == ["one", "repo/own"]


def test_print_lines_carry_the_numbers(capsys):
    bakeoff.print_final({"task": TASK, "model_id": "m", "n": 3, "macro_f1_tuned": 0.9,
                         "macro_f1_default": 0.8, "brier": 0.1, "nll": 0.2, "coverage": 0.7,
                         "fails": []})
    bakeoff.print_choose(TASK, {"backend": "b", "model_id": "m", "temperature": 1.5, "platt": None,
                                "tau": {"a": 0.6}})
    bakeoff.print_train_encoder(TASK, {"accuracy": 0.5})
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (f"label eval --final {TASK} m: n 3, macro-F1 tuned 0.9 (default 0.5: 0.8), "
                      "Brier 0.1, NLL 0.2, coverage 0.7, floors missed: none")
    assert out[1] == f'label choose {TASK}: b m (T 1.5, platt null, tau {{"a": 0.6}})'
    assert out[2] == f'label train-encoder {TASK}: {{"accuracy": 0.5}}'


def test_print_eval_is_the_table_then_the_details(tmp_path, capsys):
    with _env(tmp_path):
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), _row("m/one"))
        rec = {**_row("m/one"), "platt": None, "brier_by_calibration": {"none": 0.2},
               "folds": {"0": 100}, "no_threshold_folds": {}, "reliability_raw": [],
               "reliability_cal": [],
               "per_label": {"stop": {"n": 40, "tau": 0.6, "precision_at_tau": 0.95,
                                      "recall_at_tau": 0.9, "precision_default": 0.9,
                                      "recall_default": 0.8}}}
        bakeoff.print_eval(_cfg(tmp_path), TASK, [rec])
    out = capsys.readouterr().out
    assert out.index("label eval steer_intent: floors precision 0.9") < out.index("m/one: none (T 1.0")
