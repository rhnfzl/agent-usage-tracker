"""Hermetic tests for label/teach.py: `aut label teach`, the teacher (silver) labels. No claude.

Locks the rules: Sonnet sees only units whose text comes from Claude Code; window units never
reach it; the headless call saves no session, has no tools and runs Sonnet; spend is capped; a
rerun only adds what is missing; silver rows hold ids, teachers and numbers, never text.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from datetime import timedelta

import pytest

from agent_usage_tracker.label import evalset
from agent_usage_tracker.label import tasks
from agent_usage_tracker.label import teach
from label_helpers import (BASE, FakeSource, Oracle, TASK, WORD, _cfg, _env,
                           _src, _win)
from agent_usage_tracker.config import AutError
from agent_usage_tracker.label import core


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


def _teachers(monkeypatch, local, ask):
    monkeypatch.setattr(teach, "make_backend", lambda *a: local)
    monkeypatch.setattr(teach, "ask_claude", ask)


def test_teach_sonnet_only_for_claude_units_resumable_and_capped(tmp_path, monkeypatch):
    golds = ["framing", "stop", "correction", "review"]
    cfg, src = _src(tmp_path, golds)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return '{"stop": 0.9, "framing": 0.1}', 1.0
    _teachers(monkeypatch, Oracle(), ask)
    with _env(tmp_path):
        c = teach.run_teach(cfg, TASK, 3, budget_usd=2.0, src=src, now=BASE)
        assert c["units"] == 3 and c["local"] == 3 and c["sonnet"] == 2
        assert c["sonnet_skipped"] == 1 and c["spent_usd_milli"] == 2000
        rows = evalset.read_jsonl(evalset.eval_path(TASK, "silver"))
        assert {r["teacher"] for r in rows} == {"fake/oracle", teach.SONNET}
        assert all(set(r) == {"unit_id", "teacher", "dist", "date", "task_ver"} for r in rows)
        assert WORD not in evalset.eval_path(TASK, "silver").read_text()
        assert WORD in asked[0]                         # Sonnet saw the (Claude Code) text
        # a rerun adds only what is missing: Sonnet for the skipped unit, then the fourth unit
        asked.clear()
        c = teach.run_teach(cfg, TASK, 10, budget_usd=5.0, src=src, now=BASE)
        assert c["units"] == 2 and c["local"] == 1 and c["sonnet"] == 2
        per = Counter((r["unit_id"], r["teacher"]) for r in
                      evalset.read_jsonl(evalset.eval_path(TASK, "silver")))
        assert len(per) == 8 and set(per.values()) == {1}
        # failures and unusable answers are counted, and claude gives up after MAX_FAILS
        evalset.eval_path(TASK, "silver").unlink()

        def broken(prompt):
            raise RuntimeError("down")
        _teachers(monkeypatch, Oracle(), broken)
        c = teach.run_teach(cfg, TASK, 4, src=src, now=BASE)
        assert c["sonnet_failed"] == teach.MAX_FAILS and c["sonnet_skipped"] == 1
        evalset.eval_path(TASK, "silver").unlink()
        _teachers(monkeypatch, Oracle(), lambda p: ("no", 0.0))
        c = teach.run_teach(cfg, TASK, 1, src=src, now=BASE)
        assert c["sonnet_unusable"] == 1 and c["local"] == 1


def test_teach_never_sends_window_units(tmp_path, monkeypatch):
    t0 = BASE - timedelta(days=2)
    cfg = _cfg(tmp_path)
    src = FakeSource(cfg, wins=[_win(t0, 6, title="Private bank - statement")])
    asked = []
    _teachers(monkeypatch, Oracle("relevant"), lambda p: asked.append(p) or ("{}", 0.0))
    with _env(tmp_path):
        c = teach.run_teach(cfg, "aw_title_relevance", 5, src=src, now=BASE)
        assert c["local"] == 1 and c["sonnet"] == 0 and asked == []
        assert "bank" not in evalset.eval_path("aw_title_relevance", "silver").read_text()
    assert {"aw_block", "aw_title"}.isdisjoint(teach.CLAUDE_UNITS)


def _req(**kw) -> core.Request:
    base = dict(task="t", task_ver=1, unit="steer", unit_id="u", ts="2026-01-01T00:00:00+00:00",
                facts={"text": "tidy it"}, question="Which?", decision="choice",
                candidates=[{"id": "a", "desc": "desc a"}, {"id": "b", "desc": "desc b"}],
                none_fits=None)
    return core.Request(**{**base, **kw})


def test_sonnet_prompt_keeps_the_facts_and_asks_for_every_option_by_name():
    p = teach.sonnet_prompt(_req())
    assert p.startswith("Which?\n\nFacts:\n- text: tidy it\n") and "A. a: desc a\nB. b: desc b" in p
    assert "one letter" not in p and "option name (a, b)" in p


def test_parse_dist_normalizes_and_tolerates_what_a_model_adds():
    ids = ["a", "b"]
    assert teach.parse_dist('Here:\n{"a": 1,\n "b": 3}\nthanks', ids) == {"a": 0.25, "b": 0.75}
    assert teach.parse_dist('{"a": 1, "b": null, "z": 9}', ids) == {"a": 1.0, "b": 0.0}
    assert teach.parse_dist('{"a": -5, "b": 2}', ids) == {"a": 0.0, "b": 1.0}
    assert teach.parse_dist(None, ids) is None
    assert teach.parse_dist('{"a": [1]}', ids) is None
    assert teach.parse_dist("{cut", ids) is None


class _Claude:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode = stdout, returncode


def _fake_claude(monkeypatch, stdout, returncode=0):
    monkeypatch.setattr(teach.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(teach.subprocess, "run", lambda argv, **kw: _Claude(stdout, returncode))


def test_ask_claude_without_the_cli_is_an_operator_error(monkeypatch):
    monkeypatch.setattr(teach.shutil, "which", lambda name: None)
    with pytest.raises(AutError, match="not on PATH"):
        teach.ask_claude("p")


def test_ask_claude_reports_each_bad_answer_without_its_output(monkeypatch):
    _fake_claude(monkeypatch, json.dumps({"is_error": True, "result": "SECRET"}))
    with pytest.raises(RuntimeError) as exc:
        teach.ask_claude("p")
    assert "SECRET" not in str(exc.value)
    _fake_claude(monkeypatch, "[1]")
    with pytest.raises(RuntimeError):
        teach.ask_claude("p")
    _fake_claude(monkeypatch, "not json")
    with pytest.raises(ValueError):
        teach.ask_claude("p")
    _fake_claude(monkeypatch, "{}")
    assert teach.ask_claude("p") == ("", 0.0)     # no result and no cost is an empty free answer


def test_sonnet_dist_stops_at_the_budget_and_after_consecutive_failures(monkeypatch):
    c = Counter()
    over = teach._SonnetRun(budget_usd=1.0, spent=1.0)
    monkeypatch.setattr(teach, "ask_claude", lambda p: ('{"a": 1}', 0.0))
    assert teach._sonnet_dist(_req(), over, c) is None
    assert c == {"sonnet_skipped": 1}

    def fails(prompt):
        raise subprocess.TimeoutExpired("claude", 1)
    monkeypatch.setattr(teach, "ask_claude", fails)
    run = teach._SonnetRun(budget_usd=1.0)
    c = Counter()
    for _ in range(teach.MAX_FAILS + 1):
        assert teach._sonnet_dist(_req(), run, c) is None
    assert c == {"sonnet_failed": teach.MAX_FAILS, "sonnet_skipped": 1}


def test_sonnet_dist_counts_spend_and_a_success_clears_the_failures(monkeypatch):
    run = teach._SonnetRun(budget_usd=1.0, fails=teach.MAX_FAILS - 1)
    c = Counter()
    monkeypatch.setattr(teach, "ask_claude", lambda p: ('{"a": 1}', 0.25))
    assert teach._sonnet_dist(_req(), run, c) == {"a": 1.0, "b": 0.0}
    assert (run.fails, run.spent) == (0, 0.25) and not c
    monkeypatch.setattr(teach, "ask_claude", lambda p: ("no json", 0.25))
    assert teach._sonnet_dist(_req(), run, c) is None
    assert run.spent == 0.5 and c == {"sonnet_unusable": 1}     # an unusable answer still cost


def test_print_teach_shows_the_counts_and_the_spend(tmp_path, capsys):
    with _env(tmp_path):
        teach.print_teach(TASK, Counter(units=3, local=2, sonnet=1, sonnet_failed=4,
                                        spent_usd_milli=250), 5.0)
        out = capsys.readouterr().out
        assert "3 units, local 2, sonnet 1 (unusable 0, failed 4, skipped 0)" in out
        assert "spent $0.25 of $5.00" in out and str(evalset.eval_path(TASK, "silver")) in out


def test_sonnet_only_loads_no_local_model_and_skips_what_sonnet_may_not_read(tmp_path, monkeypatch):
    cfg, src = _src(tmp_path, ["framing", "stop"])
    src.wins = [_win(BASE - timedelta(days=2), 6, title="Private bank - statement")]
    monkeypatch.setattr(teach, "make_backend", lambda *a: pytest.fail("loaded a local model"))
    monkeypatch.setattr(teach, "ask_claude", lambda p: ('{"stop": 0.9, "framing": 0.1}', 0.01))
    with _env(tmp_path):
        c = teach.run_teach(cfg, TASK, 10, src=src, now=BASE, sonnet_only=True)
        assert c["sonnet"] == 2 and c["local"] == 0
