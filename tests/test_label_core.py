"""Hermetic tests for label/core.py: the decision interface, calibration and the stored label row.

Run: `python tests/test_label_core.py`  (or `pytest`).
Locks the rules:
  - a backend's distribution is normalized over the candidates, and the original and reversed
    candidate orders are averaged (letter position bias cancels);
  - temperature scaling keeps the top answer, a fitted T > 1 tempers an overconfident model, tau
    is the smallest p reaching the precision bar, and a label without tau never passes;
  - a label row carries enums, numbers and versions only, never the state text, and its event_id
    is stable for the same task, version, unit, model and calibration.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.label import backends  # noqa: E402
from agent_usage_tracker.label import core  # noqa: E402
from agent_usage_tracker.label import tasks  # noqa: E402
from label_helpers import ById, NOW, SECRET  # noqa: E402


class FixedLetters(backends.LetterBackend):
    """Logits per letter position, whatever the prompt says (pure position bias)."""
    model_id, model_rev = "fake/model", "rev1"

    def __init__(self, logits):
        self.logits, self.prompts = logits, []

    def letter_logits(self, prompt, letters):
        self.prompts.append(prompt)
        return self.logits[:len(letters)]


def _req(n=3) -> core.Request:
    t = tasks.TASKS["steer_intent"]
    r = t.request("steer", "u1", NOW, {"text": SECRET})
    r.candidates = r.candidates[:n]
    return r


def test_orders_are_averaged_and_normalized():
    b = FixedLetters([3.0, 0.0, 0.0])
    d = b.distribution(_req(3))
    assert abs(sum(d.values()) - 1) < 1e-9
    ids = [c["id"] for c in _req(3).candidates]
    # the first and last candidate each held letter A once: position bias cancels between them
    assert abs(d[ids[0]] - d[ids[2]]) < 1e-12 and d[ids[1]] < d[ids[0]]
    assert len(b.prompts) == 2 and b.prompts[0] != b.prompts[1]
    assert "A. framing:" in b.prompts[0] and "A. clarification:" in b.prompts[1]


def test_content_wins_over_position():
    d = ById("instruction").distribution(_req(9))
    assert max(d, key=d.get) == "instruction" and d["instruction"] > 0.9
    assert abs(sum(d.values()) - 1) < 1e-9


def test_temperature_and_tau():
    d = {"a": 0.9, "b": 0.08, "c": 0.02}
    hot = core.temper(d, 2.0)
    assert max(hot, key=hot.get) == "a" and hot["a"] < 0.9
    assert all(abs(core.temper(d, 1.0)[k] - v) < 1e-9 for k, v in d.items())
    # overconfident: always 0.99 on its answer, right 7 times in 10 -> T > 1
    dists = [{"a": 0.99, "b": 0.01}] * 10
    golds = ["a"] * 7 + ["b"] * 3
    t = core.fit_temperature(dists, golds)
    assert t > 1.5 and abs(core.temper(dists[0], t)["a"] - 0.7) < 0.02
    assert core.fit_temperature([], []) == 1.0
    items = [("a", 0.95, "a"), ("a", 0.9, "a"), ("a", 0.8, "a"), ("a", 0.7, "b"),
             ("a", 0.6, "a"), ("b", 0.9, "a"), ("c", 0.55, "c")]
    tau = core.fit_tau(items, precision=0.75)
    assert tau["a"] == 0.6          # 4 of 5 right at p >= 0.6
    assert tau["b"] is None         # never right
    assert tau["c"] == 0.55
    assert core.fit_tau([("c", 0.3, "c")])["c"] == core.TAU_FLOOR


def test_decide_below_tau():
    req = _req(3)
    raw = {"framing": 0.7, "instruction": 0.2, "clarification": 0.1}
    none = core.decide(req, raw, core.Choice(), "m", "r")
    assert none.chosen == "framing" and none.below_tau and none.calib_ver == "none"
    ch = core.Choice(temperature=1.0, tau={"framing": 0.6}, calib_ver="steer_intent@1/x")
    assert not core.decide(req, raw, ch, "m", "r").below_tau
    ch.tau["framing"] = 0.8
    assert core.decide(req, raw, ch, "m", "r").below_tau


def test_label_row_has_no_text_and_a_stable_id():
    req = _req(9)
    res = core.decide(req, ById().distribution(req), core.Choice(), "fake/byid", "rev2")
    ev = core.label_event(req, res)
    blob = json.dumps([ev.event_id, ev.attrs, ev.session_id, ev.turn_id])
    for word in SECRET.split():
        assert word not in blob
    assert set(ev.attrs) == {"task", "task_ver", "unit", "unit_id", "label", "p", "dist",
                             "below_tau", "model_id", "model_rev", "prompt_ver", "calib_ver",
                             "labelled_at"}
    assert datetime.fromisoformat(ev.attrs["labelled_at"]).tzinfo is not None
    assert core.label_event(req, res, "2026-11-04T02:13:00.000+00:00").event_id == ev.event_id
    assert ev.kind == "label" and ev.ts == req.ts and ev.attrs["label"] == "instruction"
    assert set(ev.attrs["dist"]) <= {c["id"] for c in req.candidates}
    again = core.label_event(req, core.decide(req, ById().distribution(req), core.Choice(),
                                              "fake/byid", "rev2"))
    assert again.event_id == ev.event_id
    other = core.label_event(req, core.decide(req, {"framing": 1.0}, core.Choice(), "m2", "r"))
    assert other.event_id != ev.event_id
    req.rev = "2026-11-02T10:00:00+00:00"           # a changed episode: a new row
    assert core.label_event(req, res).event_id != ev.event_id
    req.rev = None
    req.unit_id = "u2"
    assert core.label_event(req, res).event_id != ev.event_id


def test_choice_file_and_backend_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))   # no trained encoder here
    assert core.load_choice("steer_intent") == core.Choice()
    (tmp_path / "label.json").write_text(json.dumps({"tasks": {"steer_intent": {
        "backend": "mlx_letter", "model_id": "gemma-4-31b", "temperature": 1.4,
        "tau": {"stop": 0.7}, "calib_ver": "steer_intent@1/2026-11-20", "junk": 1}}}))
    c = core.load_choice("steer_intent")
    assert c.temperature == 1.4 and c.tau == {"stop": 0.7}
    b = backends.make_backend("steer_intent", c, "unused")
    assert isinstance(b, backends.MlxLetter) and b.model_id == backends.MODELS["gemma-4-31b"]
    with pytest.raises(backends.BackendUnavailable):
        backends.make_backend("x", core.Choice(model_id="pplx-decider-flash-v1.2"), "m")
    with pytest.raises(backends.BackendUnavailable):
        backends.Encoder("steer_intent").distribution(_req())


def _two_options(**kw) -> core.Request:
    base = dict(task="t", task_ver=2, unit="steer", unit_id="u", ts="2026-01-01T00:00:00+00:00",
                facts={"text": "go", "n": 3, "names": ["é"]}, question="Which?", decision="choice",
                candidates=[{"id": "b", "desc": "desc b"}, {"id": "a", "desc": "desc a"}],
                none_fits=None)
    return core.Request(**{**base, **kw})


def test_local_midnight_is_an_aware_start_of_that_day():
    m = core.local_midnight(date(2026, 3, 29))
    assert m.tzinfo is not None and (m.year, m.month, m.day, m.hour, m.minute) == (2026, 3, 29, 0, 0)


def test_label_paths_follow_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert core.data_dir() == tmp_path / "xdg" / "aut"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert core.data_dir() == tmp_path / ".local" / "share" / "aut"
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "conf" / "config.toml"))
    assert core.choice_path() == tmp_path / "conf" / "label.json"


def test_read_choices_tells_missing_from_unusable(tmp_path, monkeypatch):
    p = tmp_path / "label.json"
    monkeypatch.setattr(core, "choice_path", lambda: p)
    assert core.read_choices() == {}
    p.write_text('{"tasks": {"t": {"temperature": 2.0}}}')
    assert core.read_choices() == {"tasks": {"t": {"temperature": 2.0}}}
    for bad in ("[]", '{"tasks": []}', "{cut"):
        p.write_text(bad)
        with pytest.raises(ValueError):
            core.read_choices()


def test_load_choice_keeps_known_fields_and_warns_on_a_bad_entry(tmp_path, monkeypatch, capsys):
    p = tmp_path / "label.json"
    monkeypatch.setattr(core, "choice_path", lambda: p)
    p.write_text('{"tasks": {"t": {"temperature": 2.0, "junk": 1}, "bad": 5}}')
    assert core.load_choice("t") == core.Choice(temperature=2.0)
    assert core.load_choice("other") == core.Choice() and capsys.readouterr().err == ""
    assert core.load_choice("bad") == core.Choice()
    err = capsys.readouterr().err
    assert "its entry for bad is not a table" in err and "uncalibrated" in err


@pytest.mark.parametrize("entry", [{"temperature": "hot"}, {"temperature": 0}, {"tau": 0.5},
                                   {"tau": {"stop": "high"}}])
def test_load_choice_treats_a_bad_field_as_a_bad_entry(tmp_path, monkeypatch, capsys, entry):
    p = tmp_path / "label.json"
    monkeypatch.setattr(core, "choice_path", lambda: p)
    p.write_text(json.dumps({"tasks": {"t": entry}}))
    assert core.load_choice("t") == core.Choice()
    assert "uncalibrated" in capsys.readouterr().err


def test_render_shows_the_facts_the_options_in_order_and_the_letters():
    assert core.render(_two_options(), ["a", "b"]) == (
        'Which?\n\nFacts:\n- text: go\n- n: 3\n- names: ["é"]\n\n'
        "Options:\nA. a: desc a\nB. b: desc b\n\nAnswer with one letter (A, B) and nothing else.")


def test_softmax_sums_to_one_and_survives_huge_logits():
    assert core.softmax([1000.0, 1000.0]) == [0.5, 0.5]
    p = core.softmax([0.0, 1.0, 2.0])
    assert abs(sum(p) - 1) < 1e-12 and p[0] < p[1] < p[2]


def test_top_label_takes_the_first_of_a_tie():
    assert core.top_label({"x": 0.1, "y": 0.45, "z": 0.45}) == "y"


def test_letter_distribution_refuses_a_candidate_count_it_cannot_letter():
    many = [{"id": f"c{i}", "desc": "d"} for i in range(len(core.LETTERS) + 1)]
    for candidates in ([], many):
        with pytest.raises(ValueError, match="candidates"):
            core.letter_distribution(lambda p, ls: [0.0] * len(ls), _two_options(candidates=candidates))
    one = _two_options(candidates=[{"id": "only", "desc": "d"}])
    assert core.letter_distribution(lambda p, ls: [0.0] * len(ls), one) == {"only": 1.0}


def test_letter_distribution_asks_both_orders_with_their_letters():
    seen = []

    def logits(prompt, letters):
        seen.append((prompt.split("Options:\n")[1].split("\n")[0], letters))
        return [1.0, 0.0]
    d = core.letter_distribution(logits, _two_options())
    assert seen == [("A. b: desc b", ["A", "B"]), ("A. a: desc a", ["A", "B"])]
    assert abs(d["a"] - 0.5) < 1e-12 and abs(d["b"] - 0.5) < 1e-12     # pure position bias cancels


def test_temper_never_takes_the_log_of_zero():
    d = core.temper({"a": 1.0, "b": 0.0}, 1.0)
    assert d["a"] > 0.999 and 0 < d["b"] < 1e-9


def test_fit_temperature_reaches_its_bound_on_an_always_right_model():
    assert abs(core.fit_temperature([{"a": 0.6, "b": 0.4}] * 10, ["a"] * 10) - 0.05) < 1e-3


def test_fit_tau_cuts_only_between_distinct_probabilities():
    # the 0.8 pair is one cut: 2 of 3 right, under the bar, so 0.9 stands
    assert core.fit_tau([("a", 0.9, "a"), ("a", 0.8, "a"), ("a", 0.8, "b")]) == {"a": 0.9}
    assert core.fit_tau([("a", 0.8, "a"), ("a", 0.8, "b")]) == {"a": None}
    assert core.fit_tau([]) == {}


def test_decide_tempers_first_and_a_label_without_tau_never_passes():
    req = _two_options()
    raw = {"a": 0.9, "b": 0.1}
    res = core.decide(req, raw, core.Choice(temperature=2.0, tau={"a": 0.8}, calib_ver="c"), "m", "r")
    assert res.chosen == "a" and abs(res.prob - res.dist["a"]) < 1e-12 and 0.7 < res.prob < 0.8
    assert res.below_tau                        # 0.9 raw would pass 0.8; the calibrated p does not
    assert (res.model_id, res.model_rev, res.calib_ver) == ("m", "r", "c")
    assert res.prompt_ver == req.prompt_ver == f"t@2/p{core.PROMPT_VER}"
    assert core.decide(req, raw, core.Choice(tau={"b": 0.1}), "m", "r").below_tau
    assert core.decide(req, raw, core.Choice(tau={"a": None}), "m", "r").below_tau


def test_label_event_rounds_its_numbers_and_keeps_the_given_time():
    req = _two_options()
    res = core.decide(req, {"a": 2 / 3, "b": 1 / 3}, core.Choice(), "m", "r")
    ev = core.label_event(req, res, "2026-11-04T02:13:00.000+00:00")
    assert ev.attrs["labelled_at"] == "2026-11-04T02:13:00.000+00:00"
    assert ev.attrs["p"] == 0.6667 and ev.attrs["dist"] == {"a": 0.6667, "b": 0.3333}
    assert (ev.session_id, ev.turn_id, ev.agent) == (None, None, "label")
    other_calib = core.decide(req, {"a": 2 / 3, "b": 1 / 3}, core.Choice(calib_ver="c2"), "m", "r")
    assert core.label_event(req, other_calib).event_id != ev.event_id


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
