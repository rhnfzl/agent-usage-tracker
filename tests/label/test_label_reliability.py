"""Changed evidence and unfinished runs must remain eligible for another attempt."""

from collections import defaultdict
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_usage_tracker import cli
from agent_usage_tracker.label import backends, bakeoff, core, evalset, run
from label_helpers import _cfg


@pytest.mark.parametrize("flag", ["cut", "unavailable"])
def test_unfinished_label_run_has_nonzero_exit(tmp_path, monkeypatch, flag):
    monkeypatch.setattr(cli, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(run, "run_label", lambda *a, **kw: {
        "steer_intent": defaultdict(int, {flag: 1})})
    monkeypatch.setattr(run, "print_report", lambda *a, **kw: None)
    assert cli.main(["label", "run", "--task", "steer_intent"]) == 1


def test_scores_follow_evidence_and_revision_without_storing_text(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    req = core.Request("t", 1, "episode", "u", "2026-11-01T00:00:00Z",
                       {"answer": "private-before"}, "Which?", "choice",
                       [{"id": "a", "desc": "first"}, {"id": "b", "desc": "second"}],
                       None, "r1")
    calls = []

    def score(r):
        calls.append(r.rev)
        return {"a": .8, "b": .2} if r.facts["answer"] == "private-before" else {"a": .1, "b": .9}

    backend = SimpleNamespace(model_id="fake", model_rev="r", distribution=score)
    assert bakeoff._score(backend, [req])[0]["u"] == {"a": .8, "b": .2}
    assert bakeoff._score(backend, [req])[0]["u"] == {"a": .8, "b": .2}
    changed = replace(req, facts={"answer": "private-after"})
    assert bakeoff._score(backend, [changed])[0]["u"] == {"a": .1, "b": .9}
    bakeoff._score(backend, [replace(changed, rev="r2")])
    assert calls == ["r1", "r1", "r2"]
    for path in (tmp_path / "aut" / "eval" / "scores").glob("*"):
        assert "private-before" not in path.read_text()
        assert "private-after" not in path.read_text()


def test_unfingerprinted_score_is_recomputed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    req = core.Request("t", 1, "steer", "u", "", {}, "Which?", "boolean", [], None)
    path = evalset.scores_path("t", "fake", "r", req.prompt_ver)
    evalset.append_jsonl(path, {"unit_id": "u", "dist": {"a": 1.0}})
    backend = SimpleNamespace(model_id="fake", model_rev="r", distribution=lambda r: {"b": 1.0})
    assert bakeoff._score(backend, [req])[0]["u"] == {"b": 1.0}


def test_optional_memory_probe_failure_does_not_abort_evaluation(monkeypatch):
    backend = backends.LlamaServer("fake", "rev", "file")
    backend.size_mb = 2.0
    backend._proc = SimpleNamespace(pid=1)

    def denied(*args, **kwargs):
        raise PermissionError("process inspection unavailable")

    monkeypatch.setattr(backends.subprocess, "run", denied)
    assert backend.peak_memory_mb() == 2.0
