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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
