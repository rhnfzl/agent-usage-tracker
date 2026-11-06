"""Local estimates reuse saved recipes without becoming calibrated or frozen Sonnet labels."""

import json
import time
from datetime import datetime, timezone

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.label import core, local, run, tasks
from label_helpers import NOW, FakeSource, _cfg, _claude_log, _env, _steer
from test_label import sink  # noqa: F401


def recipe(method="majority"):
    return {
        "prompt_ver": f"steer_intent@2/p{core.PROMPT_VER}",
        "minimum_score": 0.5,
        "source_eval_sha256": "a" * 64,
        "recipe": {
            "id": "saved-test",
            "method": method,
            "members": [
                {"model_id": name, "backend": backend, "model_rev": rev, "temperature": 1.0}
                for name, backend, rev in (
                    ("0x36/elephant-1-9B-MLX-4bit", "mlx_elephant", "r1"),
                    ("mlx-community/clef-flash-4bit", "mlx_clef", "r2"),
                    ("perplexity-ai/pplx-decider-flash-v1.2", "mlx_pplx", "r3"),
                )
            ],
        },
    }


def request(uid="u"):
    return tasks.TASKS["steer_intent"].request(
        "steer", uid, datetime(2026, 11, 5, tzinfo=timezone.utc), {"text": "private work"}
    )


def backend(tmp_path, monkeypatch, answers):
    closed = []
    monkeypatch.setattr(local, "memory_guard", lambda: None)

    class Member:
        def __init__(self, row):
            self.model_id, self.model_rev = row["model_id"], row["model_rev"]

        def distribution(self, req):
            answer = answers[self.model_rev]
            return {c["id"]: float(c["id"] == answer) for c in req.candidates}

        def close(self):
            closed.append(self.model_rev)

    return local.LocalBackend("steer_intent", recipe(), "b" * 64, _cfg(tmp_path), Member), closed


def test_saved_majority_scores_a_batch_with_one_member_loaded_at_a_time(tmp_path, monkeypatch):
    b, closed = backend(
        tmp_path, monkeypatch, {"r1": "instruction", "r2": "instruction", "r3": "stop"}
    )
    reqs = [request("u1"), request("u2")]
    b.prepare_requests(reqs, time.monotonic() + 60)
    assert b.distribution(reqs[0])["instruction"] == pytest.approx(2 / 3)
    assert closed == ["r1", "r2", "r3"]
    assert b.distribution(reqs[1])["instruction"] == pytest.approx(2 / 3)
    assert closed == ["r1", "r2", "r3"]


def test_disagreement_stays_unknown_and_never_enters_the_frozen_namespace(tmp_path, monkeypatch):
    b, _closed = backend(
        tmp_path, monkeypatch, {"r1": "instruction", "r2": "correction", "r3": "stop"}
    )
    req = request()
    b.prepare_requests([req], time.monotonic() + 60)
    response = core.decide(req, b.distribution(req), core.Choice(), b.model_id, b.model_rev)
    event = local.decorate(core.label_event(req, response), req, b, response.prob)
    assert event.attrs["task"] == "working.local.steer_intent"
    assert event.attrs["label"] == "unknown" and event.attrs["below_tau"] is True
    assert event.attrs["population_weight"] == 1
    assert event.attrs["label_policy"] == "local-decision-v1"
    assert event.attrs["human_verified"] is False and event.attrs["provisional"] is True
    assert "private work" not in json.dumps(event.attrs)


def test_stack_reuses_saved_coefficients_without_fitting():
    result = local.combine(
        "stack", [{"a": 0.9, "b": 0.1}, {"a": 0.1, "b": 0.9}], {"a": [1.0, 0.0], "b": {}}
    )
    assert result["a"] == pytest.approx(0.9)


def test_uncertainty_uses_unrounded_native_score(tmp_path, monkeypatch):
    b, _closed = backend(
        tmp_path, monkeypatch, {"r1": "instruction", "r2": "instruction", "r3": "stop"}
    )
    req = request()
    raw = {c["id"]: (1 - 0.49999) / 8 for c in req.candidates}
    raw["instruction"] = 0.49999
    response = core.decide(req, raw, core.Choice(), b.model_id, b.model_rev)
    event = core.label_event(req, response)
    assert event.attrs["p"] == 0.5
    event = local.decorate(event, req, b, response.prob)
    assert event.attrs["label"] == "unknown" and event.attrs["below_tau"] is True


def test_remote_models_and_changed_prompts_are_rejected(tmp_path):
    path = tmp_path / "policy.json"
    row = recipe()
    row["recipe"]["members"][0]["backend"] = "vllm_letter"
    path.write_text(json.dumps({"version": local.POLICY, "tasks": {"steer_intent": row}}))
    with pytest.raises(AutError):
        local.load_policy(path)
    row = recipe()
    row["prompt_ver"] = "steer_intent@1/p2"
    path.write_text(json.dumps({"version": local.POLICY, "tasks": {"steer_intent": row}}))
    with pytest.raises(AutError):
        local.load_policy(path)


def test_run_sends_only_local_estimates_and_keeps_calibrated_watermark(tmp_path, monkeypatch, sink):  # noqa: F811
    cfg = _cfg(tmp_path, label_tasks=("steer_intent",))
    _claude_log(tmp_path, "s1", [("u1", "continue the work")])
    source = FakeSource(cfg, [_steer("u1", "s1", NOW)])
    b, _closed = backend(
        tmp_path, monkeypatch, {"r1": "instruction", "r2": "instruction", "r3": "stop"}
    )
    monkeypatch.setattr(
        run, "load_policy", lambda: ({"tasks": {"steer_intent": recipe()}}, "b" * 64)
    )
    with _env(tmp_path):
        original = "2026-10-01T00:00:00+00:00"
        (tmp_path / "state.json").write_text(
            json.dumps({"collectors": {"label": {"steer_intent": original}}})
        )
        report = run.run_label(
            cfg, src=source, backend_for=lambda _t, _c: b, local_estimates=True, now=NOW
        )
        state = json.loads((tmp_path / "state.json").read_text())["collectors"]
    assert report["steer_intent"]["rows"] == 1
    assert state["label"]["steer_intent"] == original
    assert "steer_intent" in state["label_local"]
    assert [e.attrs["task"] for batch in sink.rows for e in batch] == ["working.local.steer_intent"]
