"""A choice-description change is a real evaluator change, not a cached old decision."""

from copy import deepcopy

from agent_usage_tracker.improve import judge
from agent_usage_tracker.improve.store import Store


def test_description_map_order_does_not_change_model_candidate_order(monkeypatch):
    before = judge._request({"case_id": "source"}, "completion_supported")
    reordered = {rubric: dict(reversed(list(labels.items())))
                 for rubric, labels in judge.CHOICE_DESCRIPTIONS.items()}
    monkeypatch.setattr(judge, "CHOICE_DESCRIPTIONS", reordered)
    after = judge._request({"case_id": "source"}, "completion_supported")
    assert before.candidates == after.candidates


def test_choice_descriptions_reach_models_and_invalidate_saved_votes(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config/config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    descriptions = {rubric: {label: f"{rubric} meaning of {label}"
                            for label in ("pass", "fail", "unknown")} for rubric in judge.RUBRICS}
    monkeypatch.setattr(judge, "CHOICE_DESCRIPTIONS", descriptions, raising=False)
    requests = []

    class Backend:
        model_id = "test"
        model_rev = "fixed"
        protocol = "fixed"

        def distribution(self, request):
            requests.append(request)
            return {"pass": .99, "fail": .005, "unknown": .005}

    store = Store(tmp_path / "private")
    snapshot = store.put("sessions", {"agent": "codex", "session_id": "source",
        "origin": "interactive", "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "Check it", "call_id": ""},
            {"id": "r", "kind": "reply", "text": "Checked", "call_id": ""}]})
    audit = store.put("audits", {"session_ids": [snapshot]})
    first = judge.diagnose(store, audit, models=("test",), backend_for=lambda _: Backend())
    assert first["calls"] == 2
    again = judge.diagnose(store, audit, models=("test",), backend_for=lambda _: Backend())
    assert again["calls"] == 0
    changed = deepcopy(descriptions)
    changed["completion_supported"]["unknown"] = "A changed evaluator criterion"
    monkeypatch.setattr(judge, "CHOICE_DESCRIPTIONS", changed)
    updated = judge.diagnose(store, audit, models=("test",), backend_for=lambda _: Backend())
    assert updated["calls"] == 2
    assert first["job_id"] != updated["job_id"]
    policy = store.get("diagnoses", updated["job_id"])
    assert policy["choice_descriptions"] == changed
    assert {row["id"]: row["desc"] for row in requests[-1].candidates} \
        == changed["completion_supported"]
