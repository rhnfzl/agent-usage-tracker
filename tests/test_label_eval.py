"""Hermetic tests for the label eval set: `aut label teach`, `review`, `eval`, `choose` and
`train-encoder`. No model, no claude, no database: every backend and the Sonnet call are fakes.

Run: `python tests/test_label_eval.py`  (or `pytest`).
Locks the rules:
  - Sonnet sees only units whose text comes from Claude Code; window units never reach it; the
    headless call saves no session, has no tools and runs Sonnet; spend is capped; a rerun only
    adds what is missing; silver rows hold ids, teachers and numbers, never text;
  - the gold page lives on 127.0.0.1 under a token, hides the teachers' answers until the item is
    answered, writes gold rows without text, and skip / undo / done behave;
  - the bake-off fits on the calibration days and reports on the held-out ones; its record has the
    fields agents.eval_latest reads, and no text; choose writes the smallest passing backend;
  - the encoder trains on gold + silver, never on a held-out gold unit, and its file holds no word.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_label import FakeSource, _cfg, _claude_log, _env, _steer, _win  # noqa: E402

from agent_usage_tracker import cli  # noqa: E402
from agent_usage_tracker.label import (backends, bakeoff, core, evalset, gold, tasks,  # noqa: E402
                                       teach)

TASK = "steer_intent"
LABELS = [c for c, _d in tasks.TASKS[TASK].candidates]
WORD = "zanzibarquill"
BASE = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


class Oracle(backends.LetterBackend):
    """Answers the label word written in the steer text (perfect), or always `wrong`."""
    model_id, model_rev = "fake/oracle", "r1"

    def __init__(self, wrong=None, size_mb=None):
        self.wrong, self.size_mb, self.calls = wrong, size_mb, 0
        if wrong:
            self.model_id = f"fake/always-{wrong}"

    def letter_logits(self, prompt, letters):
        self.calls += 1
        shown = dict(re.findall(r"^([A-J])\. (\w+):", prompt, re.M))
        want = self.wrong or re.search(r"^- text: (\w+)", prompt, re.M).group(1)
        return [5.0 if shown[x] == want else 0.0 for x in letters]


def _steers(home: Path, golds: list[str], days: int = 20):
    """One Claude Code steer per item, spread over `days` past days; text = its gold label, then
    a word no stored row may hold."""
    recs = [(f"u{i}", f"{g} {WORD}") for i, g in enumerate(golds)]
    _claude_log(home, "s1", recs)
    return [_steer(u, "s1", BASE - timedelta(days=1 + i % days, minutes=i))
            for i, (u, _t) in enumerate(recs)]


def _src(tmp: Path, golds: list[str], wins=()):
    cfg = _cfg(tmp)
    return cfg, FakeSource(cfg, _steers(tmp, golds), wins=wins)


def test_parse_dist_and_prompt():
    ids = ["a", "b", "c"]
    assert teach.parse_dist('Sure: {"a": 3, "b": 1}', ids) == {"a": 0.75, "b": 0.25, "c": 0.0}
    for junk in ("", "no json", '{"a": "x"}', '{"a": 0, "b": 0}', "[1, 2]", '{"a": -1}'):
        assert teach.parse_dist(junk, ids) is None, junk
    req = tasks.TASKS[TASK].request("steer", "u", BASE, {"text": "go"})
    p = teach.sonnet_prompt(req)
    assert "Answer with one letter" not in p and "framing" in p and p.rstrip().endswith("1.")


def test_claude_call_is_headless_sessionless_and_sonnet(monkeypatch):
    seen = {}

    class Done:
        returncode, stdout = 0, json.dumps({"result": '{"stop": 1}', "total_cost_usd": 0.02})

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return Done()
    monkeypatch.setattr(teach.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(teach.subprocess, "run", fake_run)
    assert teach.ask_claude("the prompt") == ('{"stop": 1}', 0.02)
    a = seen["argv"]
    assert a[a.index("--model") + 1] == "claude-sonnet-5-5" and "-p" in a
    assert "--no-session-persistence" in a and a[a.index("--tools") + 1] == ""
    assert seen["input"] == "the prompt" and "the prompt" not in a
    assert os.path.abspath(seen["cwd"]) != os.getcwd()        # an empty temp dir, now gone
    Done.returncode = 1
    with pytest.raises(RuntimeError) as exc:
        teach.ask_claude("the prompt")
    assert "prompt" not in str(exc.value)


def test_teach_sonnet_only_for_claude_units_resumable_and_capped(tmp_path):
    golds = ["framing", "stop", "correction", "review"]
    cfg, src = _src(tmp_path, golds)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return '{"stop": 0.9, "framing": 0.1}', 1.0
    with _env(tmp_path):
        c = teach.run_teach(cfg, TASK, 3, budget_usd=2.0, src=src, local=Oracle(), ask=ask)
        assert c["units"] == 3 and c["local"] == 3 and c["sonnet"] == 2
        assert c["sonnet_skipped"] == 1 and c["spent_usd_milli"] == 2000
        rows = evalset.read_jsonl(evalset.path(TASK, "silver"))
        assert {r["teacher"] for r in rows} == {"fake/oracle", teach.SONNET}
        assert all(set(r) == {"unit_id", "teacher", "dist", "date", "task_ver"} for r in rows)
        assert WORD not in evalset.path(TASK, "silver").read_text()
        assert WORD in asked[0]                         # Sonnet saw the (Claude Code) text
        # a rerun adds only what is missing: Sonnet for the skipped unit, then the fourth unit
        asked.clear()
        c = teach.run_teach(cfg, TASK, 10, budget_usd=5.0, src=src, local=Oracle(), ask=ask)
        assert c["units"] == 2 and c["local"] == 1 and c["sonnet"] == 2
        per = Counter((r["unit_id"], r["teacher"]) for r in
                      evalset.read_jsonl(evalset.path(TASK, "silver")))
        assert len(per) == 8 and set(per.values()) == {1}
        # failures and unusable answers are counted, and claude gives up after MAX_FAILS
        evalset.path(TASK, "silver").unlink()

        def broken(prompt):
            raise RuntimeError("down")
        c = teach.run_teach(cfg, TASK, 4, src=src, local=Oracle(), ask=broken)
        assert c["sonnet_failed"] == teach.MAX_FAILS and c["sonnet_skipped"] == 1
        evalset.path(TASK, "silver").unlink()
        c = teach.run_teach(cfg, TASK, 1, src=src, local=Oracle(), ask=lambda p: ("no", 0.0))
        assert c["sonnet_unusable"] == 1 and c["local"] == 1


def test_teach_never_sends_window_units(tmp_path):
    t0 = BASE - timedelta(days=2)
    cfg = _cfg(tmp_path)
    src = FakeSource(cfg, wins=[_win(t0, 6, title="Private bank - statement")])
    asked = []
    with _env(tmp_path):
        c = teach.run_teach(cfg, "aw_title_relevance", 5, src=src, local=Oracle("relevant"),
                            ask=lambda p: asked.append(p) or ("{}", 0.0))
        assert c["local"] == 1 and c["sonnet"] == 0 and asked == []
        assert "bank" not in evalset.path("aw_title_relevance", "silver").read_text()
    assert {"aw_block", "aw_title"}.isdisjoint(teach.CLAUDE_UNITS)


def test_stratified_and_split():
    items = ["a"] * 20 + ["b"] * 2 + ["c"]
    order = evalset.stratified(items, lambda x: x, "seed")
    assert set(order[:3]) == {"a", "b", "c"} and sorted(order) == sorted(items)
    assert order == evalset.stratified(items, lambda x: x, "seed")
    r = tasks.TASKS[TASK].request("steer", "u", BASE, {})
    later = tasks.TASKS[TASK].request("steer", "v", BASE + timedelta(minutes=1), {})
    assert evalset.held_out(r) == evalset.held_out(later)        # same day, same side
    sides = {evalset.held_out(tasks.TASKS[TASK].request("steer", "u", BASE - timedelta(days=d),
                                                         {})) for d in range(20)}
    assert sides == {True, False}
    # a title's ts moves with the window read: its side follows the unit_id, not the day
    t = tasks.TASKS["aw_title_relevance"]
    assert {evalset.held_out(t.request("aw_title", "k1", BASE - timedelta(days=d), {}))
            for d in range(20)} == {evalset.held_out(t.request("aw_title", "k1", BASE, {}))}
    assert evalset.stratum(None) == "unlabelled"
    assert evalset.stratum({"x": {"a": 0.6, "b": 0.4}, "y": {"a": 0.9, "b": 0.1}}) == "a"
    assert evalset.stratum({"x": {"a": 0.6, "b": 0.4}, "y": {"a": 0.1, "b": 0.9}}) == "disagree"


def _serve(reqs, **kw):
    box, ready = {}, threading.Event()

    def on_ready(url):
        box["url"] = url
        ready.set()
    t = threading.Thread(target=lambda: box.update(res=gold.serve(
        TASK, reqs, open_browser=False, on_ready=on_ready, **kw)))
    t.start()
    assert ready.wait(10)
    return t, box


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _post(url, **form):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, data=urllib.parse.urlencode(form).encode()),
                         timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def test_gold_page_hides_silver_until_answered_and_writes_no_text(tmp_path):
    cfg, src = _src(tmp_path, ["framing", "stop", "review"])
    with _env(tmp_path):
        reqs = evalset.window_units(cfg, TASK, src=src)
        for u in reqs:
            evalset.append_jsonl(evalset.path(TASK, "silver"), {
                "unit_id": u, "teacher": "fake/teacher", "task_ver": 1, "date": "d",
                "dist": {"acceptance": 0.8, "stop": 0.2}})
        t, box = _serve(reqs)
        url = box["url"]
        assert url.startswith("http://127.0.0.1:")
        base = url.rsplit("/", 1)[0]
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/other", timeout=5)
        assert _post(base + "/other", a="stop")[0] == 404
        page = _get(url)
        assert "acceptance (80%)" not in page and 'data-key="1"' in page
        assert 'data-key="s"' in page and 'data-key="u"' in page
        shown = re.search(r'name=u value="(\w+)"', page).group(1)
        text = re.search(r"<dt>text</dt><dd><pre>(\w+) (\w+)</pre>", page).group(1)
        assert text in LABELS and WORD in page and "0 / 30" in page
        assert _post(url, a="stop", u="not-the-shown-unit")[0] == 303      # stale: ignored
        assert _post(url, a="stop", u=shown)[0] == 303
        page = _get(url)
        assert "you said <b>stop</b>" in page and "acceptance (80%)" in page
        nxt = re.search(r'name=u value="(\w+)"', page).group(1)
        assert _post(url, a=":skip", u=nxt)[0] == 303
        assert _post(url, a=":undo")[0] == 303                     # undo the skip
        assert re.search(r'name=u value="(\w+)"', _get(url)).group(1) == nxt
        assert _post(url, a="review", u=nxt)[0] == 303
        assert _post(url, a=":undo")[0] == 303                     # undo the answer
        assert _post(url, a=":quit")[0] == 200
        t.join(10)
        assert not t.is_alive() and box["res"] == Counter(answered=1, skipped=0)
        rows = evalset.read_jsonl(evalset.path(TASK, "gold"))
        assert rows == [{"unit_id": shown, "gold": "stop", "labeller": "human",
                         "date": rows[0]["date"], "task_ver": 1}]
        assert WORD not in evalset.path(TASK, "gold").read_text()
        # the next session starts with what is left
        t, box = _serve(reqs)
        assert re.search(r'name=u value="(\w+)"', _get(box["url"])).group(1) != shown
        assert "1 / 30" in _get(box["url"])
        _post(box["url"], a=":quit")
        t.join(10)


def _gold(tmp: Path, golds: list[str]):
    cfg, src = _src(tmp, golds)
    reqs = evalset.window_units(cfg, TASK, src=src)
    for u, r in reqs.items():
        evalset.append_jsonl(evalset.path(TASK, "gold"), {
            "unit_id": u, "gold": r.state["text"].split()[0], "labeller": "human", "date": "d",
            "task_ver": 1})
    return cfg, src, reqs


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
        recs = bakeoff.run_eval(cfg, TASK, src=src,
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
        assert WORD not in evalset.path(TASK, "eval").read_text()
        assert len(evalset.read_jsonl(evalset.path(TASK, "eval"))) == 2
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
        evalset.path(TASK, "eval").unlink()
        bakeoff.run_eval(cfg, TASK, src=src, backends=[Oracle("stop")])
        with pytest.raises(SystemExit):
            bakeoff.choose(cfg, TASK)
        assert core.load_choice(TASK).model_rev == "r1"
        evalset.path(TASK, "gold").unlink()
        with pytest.raises(SystemExit):
            bakeoff.run_eval(cfg, TASK, src=src, backends=[Oracle()])


def test_share_error_is_reweighted_to_natural_frequency(tmp_path):
    with _env(tmp_path):
        # silver says 9 of 10 units are `stop`, gold oversampled the rare label 1:1
        for i in range(10):
            evalset.append_jsonl(evalset.path(TASK, "silver"), {
                "unit_id": f"p{i}", "teacher": "t", "task_ver": 1, "date": "d",
                "dist": {"stop": 0.9, "framing": 0.1} if i < 9 else {"framing": 0.9,
                                                                     "stop": 0.1}})
        for u, d in (("g1", "stop"), ("g2", "framing")):
            evalset.append_jsonl(evalset.path(TASK, "silver"), {
                "unit_id": u, "teacher": "t", "task_ver": 1, "date": "d",
                "dist": {d: 0.9}})
        w = bakeoff.weights(TASK, ["g1", "g2", "g3"])
        # population stop 10 : framing 2; gold 1 : 1 (g3 has no silver: weight 1)
        assert abs(w["g1"] / w["g2"] - 5) < 1e-9 and w["g3"] == 1.0


def test_train_encoder_and_backend(tmp_path):
    golds = (["stop", "framing", "correction"] * 24)
    with _env(tmp_path):
        cfg, src, reqs = _gold(tmp_path, golds)
        with pytest.raises(SystemExit):
            backends.Encoder(TASK).distribution(next(iter(reqs.values())))
        r = bakeoff.train_encoder(cfg, TASK, src=src)
        # the held-out split follows today's date, and each steer also carries its neighbour's
        # text, so a small fit is not always perfect: it must clearly beat chance (1/3); over
        # 14 dates checked it scored 0.89 to 1.0
        assert r["held_out"] > 0 and r["accuracy"] >= 0.8 and r["macro_f1"] >= 0.8
        held = {u for u, q in reqs.items() if evalset.held_out(q)}
        fit = {u for u, q in reqs.items() if u not in held and evalset.encoder_fit(q)}
        assert 0 < len(fit) < len(reqs) - len(held) and r["trained_on"] == len(fit)
        # eval calibrates the encoder only on the gold it never trained on
        rec = bakeoff.evaluate(TASK, backends.Encoder(TASK), [
            (q, q.state["text"].split()[0], u in held, 1.0) for u, q in reqs.items()], 0.9, "d")
        assert rec["n_cal"] == len(reqs) - len(held) - len(fit) and rec["n_held"] == len(held)
        raw = backends.encoder_path(TASK).read_text()
        assert WORD not in raw and json.loads(raw)["labels"] == LABELS
        assert all(k.isdigit() for k in json.loads(raw)["w"])     # weights keyed by bucket ids
        enc = backends.make_backend(TASK, core.Choice(backend="encoder"), "unused")
        req = next(iter(reqs.values()))
        d = enc.distribution(req)
        assert max(d, key=d.get) == req.state["text"].split()[0] and abs(sum(d.values()) - 1) < 1e-9
        assert enc.model_rev and enc.size_mb is not None
        with pytest.raises(SystemExit):                            # retrained since chosen
            backends.make_backend(TASK, core.Choice(backend="encoder", model_rev="0" * 12), "x")
        req.task_ver = 2
        with pytest.raises(SystemExit):
            enc.distribution(req)
        assert any(isinstance(b, backends.Encoder) for b in
                   bakeoff.default_backends(_cfg(tmp_path), TASK))


def test_cli_wiring(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(teach, "run_teach", lambda cfg, t, n, b, d: seen.append(
        ("teach", t, n, b, d)) or Counter())
    monkeypatch.setattr(gold, "run_review", lambda cfg, t, d: seen.append(("review", t, d)))
    monkeypatch.setattr(bakeoff, "run_eval", lambda cfg, t, d: seen.append(("eval", t, d)) or [])
    monkeypatch.setattr(bakeoff, "choose", lambda cfg, t: seen.append(("choose", t)) or {
        "backend": "encoder", "model_id": "m", "temperature": 1.0, "tau": {}})
    monkeypatch.setattr(bakeoff, "train_encoder", lambda cfg, t, d: seen.append(("train", t))
                        or {})
    with _env(tmp_path):
        assert cli.main(["label", "teach", "--task", TASK, "--n", "5"]) == 0
        assert cli.main(["label", "review"]) == 0
        assert cli.main(["label", "eval", "--task", TASK, "--days", "30"]) == 0
        assert cli.main(["label", "choose", "--task", TASK]) == 0
        assert cli.main(["label", "train-encoder", "--task", TASK]) == 0
    assert seen == [("teach", TASK, 5, 5.0, evalset.DAYS), ("review", None, evalset.DAYS),
                    ("eval", TASK, 30), ("choose", TASK), ("train", TASK)]
    with pytest.raises(SystemExit):
        cli.main(["label", "teach", "--task", "daily_note", "--n", "1"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


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
    with pytest.raises(SystemExit, match="unusable"):
        bakeoff.choose(_cfg(tmp_path), TASK)
    assert p.read_text() == "{not json", "another task's choice must survive"


def test_unreadable_jsonl_is_not_an_empty_one(tmp_path):
    assert evalset.read_jsonl(tmp_path / "gone.jsonl") == []
    with pytest.raises(SystemExit, match="cannot read"):
        evalset.read_jsonl(tmp_path)        # a directory: OSError that is not "missing"
