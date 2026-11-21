"""A choice-description change is a real evaluator change, not a cached old decision."""

from dataclasses import replace

from agent_usage_tracker.improve import judge
from agent_usage_tracker.improve.store import Store


def test_description_map_order_does_not_change_model_candidate_order(core_rubrics):
    completion = core_rubrics[1]
    before = judge._request({"case_id": "source"}, completion)
    reordered = replace(completion, labels=dict(reversed(list(completion.labels.items()))))
    after = judge._request({"case_id": "source"}, reordered)
    assert before.candidates == after.candidates


def test_choice_descriptions_reach_models_and_invalidate_only_their_rubric_votes(
        tmp_path, monkeypatch, core_rubrics):
    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config/config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    rubrics = [replace(item, labels={label: f"{item.id} meaning of {label}"
                                     for label in ("pass", "fail", "unknown")})
               for item in core_rubrics]
    monkeypatch.setattr(judge, "_rubrics", lambda: rubrics)
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
    changed = {**rubrics[1].labels, "unknown": "A changed evaluator criterion"}
    rubrics[1] = replace(rubrics[1], labels=changed)
    updated = judge.diagnose(store, audit, models=("test",), backend_for=lambda _: Backend())
    # Only the edited rubric votes again; the other keeps its vote and judgment.
    assert updated["calls"] == 1
    assert requests[-1].task == "improve_completion_supported"
    assert first["job_id"] != updated["job_id"]
    old, new = (store.get("diagnoses", report["job_id"])["rubrics"] for report in (first, updated))
    assert old["avoidable_recovery"] == new["avoidable_recovery"]
    assert old["completion_supported"] != new["completion_supported"]
    assert store.get("rubric_snapshots", new["completion_supported"])["labels"] == changed
    assert {row["id"]: row["desc"] for row in requests[-1].candidates} == changed
    kept = set(first["judgment_ids"]) & set(updated["judgment_ids"])
    assert [store.get("judgments", key)["rubric"] for key in kept] == ["avoidable_recovery"]
