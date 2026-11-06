"""Binary judges can accept a rare label below 0.5 without relaxing quality floors."""

from dataclasses import replace
import json

import pytest

from agent_usage_tracker.label import backends, binary
from agent_usage_tracker.label.evalset import append_jsonl, eval_path
from agent_usage_tracker.config import AutError
from label_helpers import _cfg
from test_label_models import BOOL


def test_structured_binary_request_reaches_native_prompts():
    req = replace(BOOL, task="steer_intent")
    derived = binary.request(req, "correction")
    assert derived.task_ver == 3 and derived.unit_id == req.unit_id
    assert derived.facts == req.facts and derived.decision == "boolean"
    assert derived.candidates[0]["id"] == "yes"
    assert "do_not_count" in derived.candidates[1]["desc"]
    text, ids = backends.pplx_user(derived, [backends.PPLX_MARKER])
    assert ids == ["no", "yes"] and '"do_not_count"' in text
    assert '"definition"' in backends.elephant_user(derived)
    body = backends.systemone_body(derived)["questions"]["q"]
    assert body["type"] == "noul" and isinstance(body["criteria"]["true"], dict)
    assert json.loads(derived.question)["evaluation_rules"]


def test_group_question_has_a_separate_score_key():
    req = replace(BOOL, task="work_category")
    assert binary.request(req, "research").task != binary.request(req, "group:research").task
    with pytest.raises(ValueError):
        binary.request(req, "missing")


def test_weighted_threshold_includes_low_probabilities_and_ties():
    assert binary.threshold([.3, .3, .1], [True, True, False], [1, 1, 1], .7, .7) == .3
    assert binary.threshold([.3, .3], [True, False], [1, 9], .7, .7) is None


def test_day_folds_never_fit_their_own_labels():
    # Every fold has both classes. Constant scores cannot separate them.
    folds = [k for k in range(5) for _ in range(2)]
    gold = [True, False] * 5
    result, probs, preds = binary.tune([.5] * 10, gold, [1.] * 10, folds, .7, .7)
    assert result["infeasible_folds"] == 5
    assert preds == [False] * 10
    assert len(probs) == 10 and result["passes"] is False


def test_combination_prefers_fine_labels_then_groups_and_rechecks_recall():
    assert binary.combine({"correction": .8, "framing": .9, "group:pushback": .99},
                          {"correction": True, "framing": True, "group:pushback": True}) == "framing"
    assert binary.combine({"correction": .4, "group:pushback": .8},
                          {"correction": False, "group:pushback": True}) == "group:pushback"
    assert binary.combine({}, {}) is None
    row = binary.metrics([True, False], [True, True], [1., 1.])
    assert row["recall"] == .5


def test_wilson_intervals_report_sample_uncertainty():
    row = binary.metrics([True] * 10, [True] * 10, [1.] * 10)
    assert row["precision_wilson"][0] == pytest.approx(.72246, abs=.0001)
    assert row["precision_wilson"][1] == pytest.approx(1.)


def test_log_odds_ensemble_is_finite_at_extremes():
    assert binary.log_odds_mean([0., 1.]) == pytest.approx(.5)
    assert binary.log_odds_mean([.8, .8]) == pytest.approx(.8)


def test_binary_eval_refuses_old_gold_before_loading_a_model(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with pytest.raises(AutError, match="version 3 gold"):
        binary.run_eval(_cfg(tmp_path), "steer_intent", 180, ["clef-flash"])


@pytest.mark.parametrize("p,w", [([float("nan")], [1.]), ([.5], [0.])])
def test_invalid_scores_or_weights_are_rejected(p, w):
    with pytest.raises(ValueError):
        binary.tune(p, [True], w, [0], .7, .7)


def test_binary_eval_saves_scores_resumes_and_uses_fixed_pilot_floors(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    reqs = {str(i): replace(BOOL, task="steer_intent", unit_id=str(i),
                           facts={"positive": i % 2 == 0}) for i in range(10)}
    for i in range(10):
        append_jsonl(eval_path("steer_intent", "gold"), {
            "unit_id": str(i), "task_ver": 3, "weight": 1.,
            "gold": "correction" if i % 2 == 0 else "instruction"})
    monkeypatch.setattr(binary, "window_units", lambda *a: reqs)
    monkeypatch.setattr(binary, "final_test", lambda r: False)
    monkeypatch.setattr(binary, "fold", lambda r: int(r.unit_id) // 2)
    monkeypatch.setattr(binary, "_memory_guard", lambda: None)

    class Judge:
        backend, model_id, model_rev, size_mb = "fake", "fake/model", "1", 1.
        calls = 0

        def distribution(self, req):
            self.calls += 1
            p = .9 if req.facts["positive"] else .1
            return {"yes": p, "no": 1 - p}

    judge = Judge()
    monkeypatch.setattr(binary, "make_backend", lambda *a: judge)
    for _ in range(2):
        result = binary.run_eval(_cfg(tmp_path), "steer_intent", 180, ["fake/model"],
                                 selected=["correction"])[0]
        assert result["per_label"]["correction"]["passes"]
        assert result["combined"]["correction"]["passes"]
        assert result["precision_floor"] == result["recall_floor"] == .7
    assert judge.calls == 10
    reqs["0"].facts = {"positive": False}
    binary.run_eval(_cfg(tmp_path), "steer_intent", 180, ["fake/model"],
                    selected=["correction"])
    assert judge.calls == 11
    second = Judge()
    second.model_id = "fake/second"
    monkeypatch.setattr(binary, "make_backend", lambda *a: second)
    record = binary.run_eval(_cfg(tmp_path), "steer_intent", 180, ["fake/second"],
                             selected=["correction"])[0]
    assert second.calls == 10
    ensemble = binary.run_ensemble("steer_intent")[0]
    assert ensemble["members"] == ["fake/model", "fake/second"]
    assert ensemble["combined"]["correction"]["recall"] >= .7
    assert second.calls == 10
    record["input_hashes"]["correction"][0] = "different-snapshot"
    append_jsonl(eval_path("steer_intent", "binary-eval"), record)
    with pytest.raises(AutError, match="different input snapshots"):
        binary.run_ensemble("steer_intent")


def test_held_fold_gold_cannot_choose_its_calibration():
    p = [.1417, .8405, .7585, .2600, .4955, .4505, .6486, .7829, .1020, .0378]
    g = [False, False, True, False, False, True, True, False, True, True]
    folds = [i // 2 for i in range(10)]
    a = binary.tune(p, g, [1.] * 10, folds, .7, .7)
    changed = [not v if folds[i] == 0 else v for i, v in enumerate(g)]
    b = binary.tune(p, changed, [1.] * 10, folds, .7, .7)
    assert a[0]["fold_platt"][0] == b[0]["fold_platt"][0]
    assert a[1][:2] == b[1][:2] and a[2][:2] == b[2][:2]


def test_cli_accepts_binary_eval_and_ensemble():
    from agent_usage_tracker.cli import _build_parser

    for command in ("eval", "ensemble"):
        args = _build_parser().parse_args([
            "label", command, "--task", "steer_intent", "--binary", "--labels",
            "group:pushback,correction", "--models", "pplx-decider-flash,clef-flash"])
        assert args.binary and args.labels == "group:pushback,correction"


def test_short_context_model_keeps_question_when_truncating_facts():
    class Tokenizer:
        def encode(self, text, **kwargs):
            return list(text)

        def apply_chat_template(self, messages, **kwargs):
            return "\n".join(m["content"] for m in messages)

    model = backends.MlxCard("unused")
    req = replace(BOOL, facts={"text": "x" * 5000})
    prompt = model.limited_prompt(Tokenizer(), req)
    assert len(prompt) <= 2048
    assert '"question": "Is it?"' in prompt and '"description": "work"' in prompt
    assert "[input truncated]" in prompt
