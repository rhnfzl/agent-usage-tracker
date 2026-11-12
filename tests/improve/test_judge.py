import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve.judge import aggregate, cases, validate_review
from agent_usage_tracker.improve.store import Store


def test_model_agreement_is_provisional_and_disagreement_abstains():
    agreed = aggregate([{"pass": .01, "fail": .98, "unknown": .01}] * 2)
    assert agreed["verdict"] == "fail"
    assert agreed["authority"] == "model_estimate"
    assert agreed["human_verified"] is False
    assert aggregate([{"pass": .98, "fail": .01, "unknown": .01},
                      {"pass": .01, "fail": .98, "unknown": .01}])["verdict"] == "unknown"
    assert aggregate([{"pass": .4, "fail": .5, "unknown": .1}])["verdict"] == "unknown"


@pytest.mark.parametrize("value", [
    {"pass": float("nan"), "fail": .5, "unknown": .5},
    {"pass": -1, "fail": 2, "unknown": 0},
    {"pass": 1},
])
def test_invalid_model_distribution_is_rejected(value):
    with pytest.raises(AutError, match="distribution"):
        aggregate([value])


def test_llm_cannot_invent_evidence_or_patch_commands():
    case = {"events": [{"id": "real", "kind": "result"}]}
    valid = {"verdict": "fail", "evidence_ids": ["real"],
             "cause": "tool_contract", "rationale": "The result contradicts the claim."}
    assert validate_review(case, valid)["evidence_ids"] == ["real"]
    with pytest.raises(AutError, match="evidence"):
        validate_review(case, {**valid, "evidence_ids": ["invented"]})
    with pytest.raises(AutError, match="fields"):
        validate_review(case, {**valid, "command": "rm -rf /"})


def test_completion_failure_must_cite_the_claim_and_contradicting_result():
    case = {"events": [{"id": "result", "kind": "result"},
                       {"id": "claim", "kind": "reply"}]}
    value = {"verdict": "fail", "evidence_ids": ["result"], "cause": "model",
             "rationale": "An implicit goal was not fulfilled."}
    with pytest.raises(AutError, match="claim"):
        validate_review(case, value, rubric="completion_supported")
    accepted = validate_review(case, {**value, "evidence_ids": ["result", "claim"]},
                               rubric="completion_supported")
    assert accepted["verdict"] == "fail"


def test_cases_keep_matched_failure_context_and_a_clean_control(tmp_path):
    store = Store(tmp_path / "private")
    snapshot = {"agent": "codex", "session_id": "s", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "Run the checks", "call_id": ""},
            {"id": "c", "kind": "call", "text": "test", "call_id": "x"},
            {"id": "r", "kind": "result", "text": "failed", "call_id": "x", "status": "error"},
            {"id": "f", "kind": "reply", "text": "The check failed", "call_id": ""}]}
    sid = store.put("sessions", snapshot)
    audit = store.put("audits", {"session_ids": [sid]})
    rows = cases(store, audit, limit=10)
    assert len(rows) == 1
    assert rows[0]["trigger"] == "error_result"
    assert {event["id"] for event in rows[0]["events"]} == {"p", "c", "r", "f"}
    assert rows[0]["session_snapshot"] == sid


def test_diagnosis_resumes_votes_and_reuses_unchanged_cases_across_audits(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import judge

    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config/config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    calls = []

    class Backend:
        model_rev = "one"
        protocol = "first"

        def __init__(self, name):
            self.model_id = name

        def distribution(self, request):
            calls.append(request.task)
            return {"pass": .99, "fail": .005, "unknown": .005}

    store = Store(tmp_path / "private")
    sid = store.put("sessions", {"agent": "codex", "session_id": "s", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "Check it", "call_id": ""},
            {"id": "a", "kind": "reply", "text": "Checked", "call_id": ""}]})
    first = store.put("audits", {"session_ids": [sid], "coverage": {"seen": 1}})
    result = judge.diagnose(store, first, models=("one", "two"), max_calls=1,
                            backend_for=lambda name: Backend(name))
    assert not result["complete"]
    assert len(calls) == 1
    result = judge.diagnose(store, first, models=("one", "two"), max_calls=10,
                            backend_for=lambda name: Backend(name))
    assert result["complete"]
    assert len(calls) == 4
    second = store.put("audits", {"session_ids": [sid], "coverage": {"seen": 2}})
    again = judge.diagnose(store, second, models=("one", "two"), max_calls=10,
                           backend_for=lambda name: Backend(name))
    assert again["calls"] == 0
    assert len(calls) == 4
    Backend.protocol = "second"
    updated = judge.diagnose(store, second, models=("one", "two"), max_calls=10,
                             backend_for=lambda name: Backend(name))
    assert updated["calls"] == 4
    assert len(calls) == 8


def test_incomplete_cases_abstain_before_model_inference(tmp_path, monkeypatch):
    from agent_usage_tracker.improve import judge

    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config/config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)

    class Backend:
        model_id = "test"
        model_rev = "one"

        def distribution(self, request):
            raise AssertionError("Incomplete evidence must not consume inference")

    store = Store(tmp_path / "private")
    sid = store.put("sessions", {"agent": "codex", "session_id": "s", "origin": "interactive",
        "coverage": {"complete": False}, "events": [
            {"id": "p", "kind": "prompt", "text": "Check it", "call_id": ""},
            {"id": "a", "kind": "reply", "text": "Checked", "call_id": ""}]})
    audit = store.put("audits", {"session_ids": [sid]})
    result = judge.diagnose(store, audit, models=("one",), backend_for=lambda name: Backend())
    assert result["complete"]
    assert result["calls"] == 0
    assert result["verdicts"]["unknown"] == 2


def test_complete_attempt_is_not_invalidated_by_unrelated_clipped_history(tmp_path):
    store = Store(tmp_path / "private")
    events = [
        {"id": "old", "kind": "reply", "text": "clipped earlier turn", "truncated": True},
        {"id": "old-end", "kind": "boundary", "text": "turn_complete"},
        {"id": "p", "kind": "prompt", "text": "Run the check"},
        {"id": "c", "kind": "call", "call_id": "c", "text": "check"},
        {"id": "r", "kind": "result", "call_id": "c", "status": "success", "text": "passed"},
        {"id": "a", "kind": "reply", "text": "The check passed"},
        {"id": "end", "kind": "boundary", "text": "turn_complete"}]
    sid = store.put("sessions", {"version": 4, "agent": "codex", "session_id": "s",
        "origin": "interactive", "coverage": {"complete": False, "truncated_events": 1},
        "events": events})
    audit = store.put("audits", {"session_ids": [sid]})
    [case] = cases(store, audit)
    assert case["coverage"]["complete"] is True
    assert case["coverage"]["session_complete"] is False
    assert {event["id"] for event in case["events"]} == {"p", "c", "r", "a", "end"}
    events[4]["truncated"] = True
    clipped = store.put("sessions", {"version": 4, "agent": "codex", "session_id": "s",
        "origin": "interactive", "coverage": {"complete": False, "truncated_events": 2},
        "events": events})
    other = store.put("audits", {"session_ids": [clipped]})
    assert cases(store, other)[0]["coverage"]["complete"] is False


def test_delayed_result_cannot_rejoin_a_call_across_an_aborted_boundary(tmp_path):
    store = Store(tmp_path / "private")
    sid = store.put("sessions", {"version": 4, "agent": "codex", "session_id": "s",
        "origin": "interactive", "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "Check it"},
            {"id": "c", "kind": "call", "call_id": "x", "text": "check"},
            {"id": "abort", "kind": "boundary", "text": "turn_aborted"},
            {"id": "r", "kind": "result", "call_id": "x", "status": "error", "text": "failed"},
            {"id": "reply", "kind": "reply", "text": "Finished"},
            {"id": "end", "kind": "boundary", "text": "turn_complete"}]})
    audit = store.put("audits", {"session_ids": [sid]})
    [case] = cases(store, audit)
    assert not case["coverage"]["complete"]
    assert "c" not in {event["id"] for event in case["events"]}


def test_failure_session_also_supplies_a_clean_complete_control(tmp_path):
    store = Store(tmp_path / "private")
    sid = store.put("sessions", {"version": 4, "agent": "codex", "session_id": "s",
        "origin": "interactive", "coverage": {"complete": False}, "events": [
            {"id": "p1", "kind": "prompt", "text": "Earlier attempt"},
            {"id": "r1", "kind": "result", "call_id": "missing", "status": "error"},
            {"id": "b1", "kind": "boundary", "text": "turn_complete"},
            {"id": "p2", "kind": "prompt", "text": "Explain it"},
            {"id": "a2", "kind": "reply", "text": "Explanation"},
            {"id": "b2", "kind": "boundary", "text": "turn_complete"}]})
    audit = store.put("audits", {"session_ids": [sid]})
    rows = cases(store, audit, limit=2)
    assert len(rows) == 2
    control = next(row for row in rows if row["trigger"] == "control")
    assert control["coverage"]["complete"]
    assert {event["id"] for event in control["events"]} == {"p2", "a2", "b2"}
