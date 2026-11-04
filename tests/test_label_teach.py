"""Hermetic tests for label/teach.py: `aut label teach`, the teacher (silver) labels. No claude.

Run: `python tests/test_label_teach.py`  (or `pytest`).
Locks the rules: Sonnet sees only units whose text comes from Claude Code; window units never
reach it; the headless call saves no session, has no tools and runs Sonnet; spend is capped; a
rerun only adds what is missing; silver rows hold ids, teachers and numbers, never text.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.label import evalset  # noqa: E402
from agent_usage_tracker.label import tasks  # noqa: E402
from agent_usage_tracker.label import teach  # noqa: E402
from label_helpers import (BASE, FakeSource, Oracle, TASK, TODAY, WORD, _cfg, _env,  # noqa: E402
                           _src, _win)


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
        c = teach.run_teach(cfg, TASK, 3, budget_usd=2.0, src=src, today=TODAY, local=Oracle(),
                            ask=ask)
        assert c["units"] == 3 and c["local"] == 3 and c["sonnet"] == 2
        assert c["sonnet_skipped"] == 1 and c["spent_usd_milli"] == 2000
        rows = evalset.read_jsonl(evalset.eval_path(TASK, "silver"))
        assert {r["teacher"] for r in rows} == {"fake/oracle", teach.SONNET}
        assert all(set(r) == {"unit_id", "teacher", "dist", "date", "task_ver"} for r in rows)
        assert WORD not in evalset.eval_path(TASK, "silver").read_text()
        assert WORD in asked[0]                         # Sonnet saw the (Claude Code) text
        # a rerun adds only what is missing: Sonnet for the skipped unit, then the fourth unit
        asked.clear()
        c = teach.run_teach(cfg, TASK, 10, budget_usd=5.0, src=src, today=TODAY, local=Oracle(),
                            ask=ask)
        assert c["units"] == 2 and c["local"] == 1 and c["sonnet"] == 2
        per = Counter((r["unit_id"], r["teacher"]) for r in
                      evalset.read_jsonl(evalset.eval_path(TASK, "silver")))
        assert len(per) == 8 and set(per.values()) == {1}
        # failures and unusable answers are counted, and claude gives up after MAX_FAILS
        evalset.eval_path(TASK, "silver").unlink()

        def broken(prompt):
            raise RuntimeError("down")
        c = teach.run_teach(cfg, TASK, 4, src=src, today=TODAY, local=Oracle(), ask=broken)
        assert c["sonnet_failed"] == teach.MAX_FAILS and c["sonnet_skipped"] == 1
        evalset.eval_path(TASK, "silver").unlink()
        c = teach.run_teach(cfg, TASK, 1, src=src, today=TODAY, local=Oracle(),
                            ask=lambda p: ("no", 0.0))
        assert c["sonnet_unusable"] == 1 and c["local"] == 1


def test_teach_never_sends_window_units(tmp_path):
    t0 = BASE - timedelta(days=2)
    cfg = _cfg(tmp_path)
    src = FakeSource(cfg, wins=[_win(t0, 6, title="Private bank - statement")])
    asked = []
    with _env(tmp_path):
        c = teach.run_teach(cfg, "aw_title_relevance", 5, src=src, today=TODAY, local=Oracle("relevant"),
                            ask=lambda p: asked.append(p) or ("{}", 0.0))
        assert c["local"] == 1 and c["sonnet"] == 0 and asked == []
        assert "bank" not in evalset.eval_path("aw_title_relevance", "silver").read_text()
    assert {"aw_block", "aw_title"}.isdisjoint(teach.CLAUDE_UNITS)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
