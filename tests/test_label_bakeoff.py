"""Hermetic tests for label/bakeoff.py: `aut label eval`, `choose` and `train-encoder`. No model.

Run: `python tests/test_label_bakeoff.py`  (or `pytest`).
Locks the rules: the bake-off fits on the calibration days and reports on the held-out ones; its
record has the fields agents.eval_latest reads, and no text; choose writes the smallest passing
backend; the encoder trains on gold + silver, never on a held-out gold unit, and its file holds no
word.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.config import AutError  # noqa: E402
from agent_usage_tracker.label import backends  # noqa: E402
from agent_usage_tracker.label import bakeoff  # noqa: E402
from agent_usage_tracker.label import core  # noqa: E402
from agent_usage_tracker.label import evalset  # noqa: E402
from agent_usage_tracker.label import tasks  # noqa: E402
from label_helpers import BASE, LABELS, Oracle, TASK, TODAY, WORD, _cfg, _env, _gold  # noqa: E402


def test_metrics():
    assert bakeoff.kappa([("a", "a"), ("b", "b")]) == 1.0
    assert bakeoff.kappa([("a", "b"), ("b", "a")]) == -1.0
    assert bakeoff.ece([(0.9, True)] * 9 + [(0.9, False)]) < 1e-9
    assert abs(bakeoff.ece([(1.0, False)] * 4) - 1.0) < 1e-9
    assert 0.83 < bakeoff.wilson_lb(29, 29) < 0.9 and bakeoff.wilson_lb(0, 0) is None


def test_eval_record_choose_and_event(tmp_path, monkeypatch):
    golds = (LABELS * 6)[:48]
    sent = []
    monkeypatch.setattr(bakeoff, "send_activity_rows", lambda dsn, rows: sent.extend(rows))
    with _env(tmp_path):
        cfg, src, reqs = _gold(tmp_path, golds)
        recs = bakeoff.run_eval(cfg, TASK, src=src, now=BASE,
                                backends=[Oracle(size_mb=4000), Oracle("stop", size_mb=10)])
        good, bad = recs
        assert good["n"] == 48 and good["n_cal"] + good["n_held"] == 48 and good["n_held"]
        assert good["macro_f1"] == 1.0 and good["kappa"] == 1.0 and good["coverage"] == 1.0
        assert good["temperature"] < 1 and good["ece_cal"] <= good["ece_raw"]
        assert bad["macro_f1"] < 0.2 and bad["kappa"] <= 0
        assert set(good["per_label"]) == set(LABELS)
        for v in good["per_label"].values():
            assert {"n", "precision", "recall", "f1", "tau", "coverage",
                    "share_error_pp"} <= set(v)
            assert v["share_error_pp"] == 0.0 and v["tau"] >= core.TAU_FLOOR
            assert v["f1"] == 1.0 if v["n"] else v["f1"] is None
        assert [e.kind for e in sent] == ["eval", "eval"] and sent[0].attrs == good
        assert sent[0].agent == "label" and sent[0].machine == "test"
        blob = json.dumps([e.attrs for e in sent])
        assert WORD not in blob and not any(u in blob for u in reqs)     # no unit ids, no text
        assert WORD not in evalset.eval_path(TASK, "eval").read_text()
        assert len(evalset.read_jsonl(evalset.eval_path(TASK, "eval"))) == 2
        # the small backend fails the bar, so the big one wins; the choice reads back
        win = bakeoff.choose(cfg, TASK)
        assert win["model_id"] == "fake/oracle" and win["temperature"] == good["temperature"]
        c = core.load_choice(TASK)
        assert c.tau == {k: v["tau"] for k, v in good["per_label"].items()}
        assert c.calib_ver == good["calib_ver"] and c.model_rev == "r1"
        # a held-out precision at tau below label_precision fails the bar, whatever coverage
        rec = json.loads(json.dumps(good))
        assert bakeoff.passes(rec, 0.75, 0.9)
        rec["per_label"]["stop"]["precision_at_tau"] = 0.7
        assert not bakeoff.passes(rec, 0.75, 0.9)
        # nothing passes -> no choice is written over the old one
        evalset.eval_path(TASK, "eval").unlink()
        bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[Oracle("stop")])
        with pytest.raises(AutError):
            bakeoff.choose(cfg, TASK)
        assert core.load_choice(TASK).model_rev == "r1"
        evalset.eval_path(TASK, "gold").unlink()
        with pytest.raises(AutError):
            bakeoff.run_eval(cfg, TASK, src=src, now=BASE, backends=[Oracle()])


def test_share_error_is_reweighted_to_natural_frequency(tmp_path):
    with _env(tmp_path):
        # silver says 9 of 10 units are `stop`, gold oversampled the rare label 1:1
        for i in range(10):
            evalset.append_jsonl(evalset.eval_path(TASK, "silver"), {
                "unit_id": f"p{i}", "teacher": "t", "task_ver": 1, "date": "d",
                "dist": {"stop": 0.9, "framing": 0.1} if i < 9 else {"framing": 0.9,
                                                                     "stop": 0.1}})
        for u, d in (("g1", "stop"), ("g2", "framing")):
            evalset.append_jsonl(evalset.eval_path(TASK, "silver"), {
                "unit_id": u, "teacher": "t", "task_ver": 1, "date": "d",
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
        r = bakeoff.train_encoder(cfg, TASK, src=src, today=TODAY)
        # each steer also carries its neighbour's text, so a small fit is not perfect
        assert (r["trained_on"], r["held_out"]) == (16, 38)
        assert (r["accuracy"], r["macro_f1"]) == (0.8947, 0.8916)
        held = {u for u, q in reqs.items() if evalset.held_out(q)}
        fit = {u for u, q in reqs.items() if u not in held and evalset.in_encoder_train_split(q)}
        assert 0 < len(fit) < len(reqs) - len(held) and r["trained_on"] == len(fit)
        # eval calibrates the encoder only on the gold it never trained on
        rec = bakeoff.evaluate(TASK, backends.Encoder(TASK), [
            (q, q.state["text"].split()[0], u in held, 1.0) for u, q in reqs.items()], 0.9, TODAY)
        assert rec["n_cal"] == len(reqs) - len(held) - len(fit) and rec["n_held"] == len(held)
        raw = backends.encoder_path(TASK).read_text()
        assert WORD not in raw and json.loads(raw)["labels"] == LABELS
        assert all(k.isdigit() for k in json.loads(raw)["w"])     # weights keyed by bucket ids
        enc = backends.make_backend(TASK, core.Choice(backend="encoder"), "unused")
        req = next(iter(reqs.values()))
        d = enc.distribution(req)
        assert max(d, key=d.get) == req.state["text"].split()[0] and abs(sum(d.values()) - 1) < 1e-9
        assert enc.model_rev and enc.size_mb is not None
        with pytest.raises(backends.BackendUnavailable):                            # retrained since chosen
            backends.make_backend(TASK, core.Choice(backend="encoder", model_rev="0" * 12), "x")
        req.task_ver = 2
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
    monkeypatch.setattr(bakeoff, "read_jsonl", lambda _p: [
        {"task_ver": tasks.TASKS[TASK].ver, "model_id": "m", "backend": "b", "model_rev": "r",
         "temperature": 1.0, "calib_ver": "c", "per_label": {}, "coverage": 1.0, "n_held": 9,
         "size_mb": 1, "macro_f1": 1.0}])
    monkeypatch.setattr(bakeoff, "passes", lambda *a: True)
    with pytest.raises(AutError, match="unusable"):
        bakeoff.choose(_cfg(tmp_path), TASK)
    assert p.read_text() == "{not json", "another task's choice must survive"


def _record(model="m", size=1.0, f1=0.9, coverage=1.0, **per) -> dict:
    return {"task_ver": tasks.TASKS[TASK].ver, "model_id": model, "backend": "mlx_letter",
            "model_rev": "r", "temperature": 1.5, "calib_ver": "c", "size_mb": size,
            "macro_f1": f1, "coverage": coverage, "n_held": 9,
            "per_label": {"stop": {"tau": 0.7, "precision_at_tau": 0.95, "share_error_pp": 1.0},
                          "framing": {"tau": None, "precision_at_tau": None, "share_error_pp": 2.0},
                          **per}}


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


def test_passes_needs_every_part_of_the_bar():
    assert bakeoff.passes(_record(), 0.75, 0.9)
    assert not bakeoff.passes({**_record(), "n_held": 0}, 0.75, 0.9)
    assert not bakeoff.passes(_record(coverage=0.7), 0.75, 0.9)
    assert not bakeoff.passes({**_record(), "coverage": None}, 0.75, 0.9)
    edge = {"tau": 0.7, "precision_at_tau": None, "share_error_pp": bakeoff.SHARE_ERROR_PP}
    assert bakeoff.passes(_record(other=edge), 0.75, 0.9)
    assert not bakeoff.passes(_record(other={**edge, "share_error_pp": 5.01}), 0.75, 0.9)
    assert not bakeoff.passes(_record(other={**edge, "share_error_pp": None}), 0.75, 0.9)


def _choices(tmp_path, monkeypatch, records):
    monkeypatch.setattr(core, "choice_path", lambda: tmp_path / "label.json")
    monkeypatch.setattr(bakeoff, "choice_path", lambda: tmp_path / "label.json")
    for r in records:
        evalset.append_jsonl(evalset.eval_path(TASK, "eval"), r)


def test_choose_takes_the_smallest_then_the_best_and_keeps_other_tasks(tmp_path, monkeypatch):
    with _env(tmp_path):
        _choices(tmp_path, monkeypatch, [
            _record("big", size=100), _record("small-worse", size=10, f1=0.8),
            _record("small-better", size=10, f1=0.9), _record("unknown-size", size=None)])
        (tmp_path / "label.json").write_text('{"tasks": {"other": {"backend": "x"}}}')
        win = bakeoff.choose(_cfg(tmp_path), TASK)
        assert win["model_id"] == "small-better" and win["tau"] == {"stop": 0.7}
        saved = json.loads((tmp_path / "label.json").read_text())
        assert saved["tasks"]["other"] == {"backend": "x"} and saved["tasks"][TASK] == win


def test_choose_reads_only_the_newest_record_of_a_model(tmp_path, monkeypatch):
    with _env(tmp_path):
        _choices(tmp_path, monkeypatch, [_record("m"), _record("m", coverage=0.1)])
        with pytest.raises(AutError, match="no backend passes") as exc:
            bakeoff.choose(_cfg(tmp_path), TASK)
        assert json.loads(str(exc.value).split(f"{TASK}: ", 1)[1]) == {
            "m": {"coverage": 0.1, "n_held": 9}}
        assert not (tmp_path / "label.json").exists()


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


def test_training_data_keeps_held_out_and_eval_gold_away():
    labels = ["stop", "framing"]
    reqs = {f"u{i}": tasks.TASKS[TASK].request("steer", f"u{i}", BASE, {"text": f"t{i}"})
            for i in range(30)}
    train = [u for u, r in reqs.items() if evalset.in_encoder_train_split(r)]
    eval_half = [u for u in reqs if u not in train]
    gold_ = {train[0]: "stop", train[1]: "framing", eval_half[0]: "stop", "gone": "stop"}
    silver = {eval_half[1]: {"x": {"stop": 1.0}, "y": {"stop": 0.5, "framing": 0.5}},
              train[0]: {"x": {"framing": 1.0}},       # has gold: the gold row stands alone
              eval_half[2]: {}}
    data = bakeoff._training_data(gold_, silver, reqs, {train[1]}, labels)
    assert [(t, w) for _f, t, w in data] == [([1.0, 0.0], bakeoff.GOLD_WEIGHT), ([0.75, 0.25], 1.0)]
    assert data[0][0] == backends.features(reqs[train[0]].state)


def test_gold_items_says_how_many_fell_out_of_the_window(tmp_path):
    with _env(tmp_path):
        cfg, src, reqs = _gold(tmp_path, ["stop", "framing"])
        evalset.append_jsonl(evalset.eval_path(TASK, "gold"), {
            "unit_id": "gone", "gold": "stop", "task_ver": tasks.TASKS[TASK].ver})
        items, gone = bakeoff.gold_items(cfg, TASK, 30, src, TODAY)
        assert gone == 1 and {r.unit_id for r, *_ in items} == set(reqs)
        for r, g, held, weight in items:
            assert g == r.state["text"].split()[0] and held == evalset.held_out(r) and weight == 1.0


def test_default_backends_dedups_models_and_names_the_ones_left_out(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bakeoff, "MODELS", {"one": "repo/one", "two": "repo/one", "none": None})
    monkeypatch.setattr(bakeoff, "make_backend", lambda task, c, fallback: c.model_id)
    with _env(tmp_path):
        got = bakeoff.default_backends(_cfg(tmp_path, label_model="repo/own"), TASK)
    assert got == ["repo/one", "repo/own"]
    assert "none has no official MLX build" in capsys.readouterr().err


def test_print_lines_carry_the_numbers(capsys):
    rec = {"model_id": "m", "macro_f1": 0.9, "kappa": 0.8, "ece_raw": 0.2, "ece_cal": 0.1,
           "coverage": 0.7, "n": 10, "n_held": 4, "s_per_decision": 0.5}
    bakeoff.print_eval(TASK, [rec])
    bakeoff.print_choose(TASK, {"backend": "b", "model_id": "m", "temperature": 1.5, "tau": {"a": 0.6}})
    bakeoff.print_train_encoder(TASK, {"accuracy": 0.5})
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (f"label eval {TASK} m: macro-F1 0.9, kappa 0.8, ECE 0.2 -> 0.1, coverage 0.7, "
                      "n 10 (held out 4), 0.5 s per decision")
    assert out[1] == f'label choose {TASK}: b m (T 1.5, tau {{"a": 0.6}})'
    assert out[2] == f'label train-encoder {TASK}: {{"accuracy": 0.5}}'


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
