from types import SimpleNamespace

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.improve import judge
from agent_usage_tracker.improve.store import Store


@pytest.fixture
def native_budget(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    sid = store.put("sessions", {"agent": "codex", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "prompt", "kind": "prompt", "text": "Check the result"},
            {"id": "reply", "kind": "reply", "text": "Checked"}]})
    audit = store.put("audits", {"session_ids": [sid]})
    clock = [0.0]
    monkeypatch.setattr(judge.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(judge, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(judge, "memory_guard", lambda: None)
    return store, audit, clock


def backend_for(clock, calls, durations, verdict="pass"):
    class Backend:
        model_rev = "synthetic"

        def __init__(self, name):
            self.model_id = name

        def distribution(self, request):
            calls.append((self.model_id, request.task))
            clock[0] += durations[self.model_id]
            return {label: .99 if label == verdict else .005 for label in judge.LABELS}

    return Backend


@pytest.mark.parametrize("models", [("slow",), ("slow", "fast")])
@pytest.mark.parametrize("remaining", [4.0, 30.0])
def test_native_call_needs_observed_duration_and_reserve_before_starting(
        native_budget, models, remaining):
    store, audit, clock = native_budget
    calls = []
    backend = backend_for(clock, calls, {"slow": 20, "fast": 1})

    def near_deadline(progress):
        if progress["finished"] == 1:
            clock[0] = 180 - remaining

    first = judge.diagnose(store, audit, models=models, budget_s=180, max_calls=24,
                           backend_for=backend, progress=near_deadline)
    assert first["reason"] == "budget" and not first["complete"]
    assert first["finished"] == 1
    assert first["calls"] == len(models)
    assert len(calls) == len(models)
    retained = first["judgment_ids"]
    clock[0] = 0
    resumed = judge.diagnose(store, audit, models=models, budget_s=180, max_calls=24,
                             backend_for=backend)
    assert resumed["complete"] and resumed["calls"] == len(models)
    assert resumed["job_id"] == first["job_id"]
    assert set(retained) <= set(resumed["judgment_ids"])
    assert len(calls) == 2 * len(models)
    assert len(store.keys("judgments")) == 2


def test_unknown_cold_model_needs_half_the_original_budget(native_budget):
    store, audit, clock = native_budget
    calls = []
    result = judge.diagnose(store, audit, models=("cold",), budget_s=180, max_calls=24,
                            deadline=60, backend_for=backend_for(clock, calls, {"cold": 20}))
    assert result["reason"] == "budget" and not result["complete"]
    assert result["calls"] == 0 and not calls
    assert not store.keys("votes")


def test_cached_votes_and_judgments_need_no_native_call_window(native_budget):
    store, audit, clock = native_budget
    calls = []
    backend = backend_for(clock, calls, {"local": 0})

    def interrupted(progress):
        if progress["calls"] == 2 and len(store.keys("votes")) == 2:
            raise RuntimeError("interrupted after vote persistence")

    with pytest.raises(RuntimeError, match="after vote persistence"):
        judge.diagnose(store, audit, models=("local",), budget_s=180,
                       backend_for=backend, progress=interrupted)
    assert len(store.keys("judgments")) == 1
    result = judge.diagnose(store, audit, models=("local",), budget_s=180,
                            deadline=5, backend_for=backend)
    assert result["complete"] and result["calls"] == 0
    assert len(calls) == 2


def review_setup(monkeypatch):
    monkeypatch.setattr(judge, "RUBRICS", {"avoidable_recovery": ("Question", "Definitions")})
    monkeypatch.setattr(judge, "MlxLetter", lambda _: SimpleNamespace(
        model_rev="synthetic", prepare=lambda: None, close=lambda: None))


def accepted_review():
    return {"verdict": "fail", "evidence_ids": ["prompt", "reply"], "cause": "model",
            "rationale": "Repeated known failure"}


def test_cold_explanatory_review_has_its_own_admission_window(native_budget, monkeypatch):
    store, audit, clock = native_budget
    review_setup(monkeypatch)
    votes, reviews = [], []

    def review(*args):
        reviews.append(True)
        clock[0] += 1
        return accepted_review()

    monkeypatch.setattr(judge, "_llm_review", review)

    def after_vote(progress):
        if store.keys("votes"):
            clock[0] = 120

    kwargs = {"models": ("local",), "budget_s": 180, "max_calls": 24,
              "explain_model": "local",
              "backend_for": backend_for(clock, votes, {"local": 1}, "fail")}
    first = judge.diagnose(store, audit, progress=after_vote, **kwargs)
    assert first["reason"] == "budget" and first["calls"] == 1
    assert not first["complete"] and not reviews
    vote_ids = store.keys("votes")
    clock[0] = 0
    resumed = judge.diagnose(store, audit, **kwargs)
    assert resumed["complete"] and resumed["calls"] == 1
    assert len(votes) == len(reviews) == 1
    assert store.keys("votes") == vote_ids
    assert resumed["job_id"] == first["job_id"]


@pytest.mark.parametrize("remaining", [5, 45])
def test_schema_repair_reserves_time_using_the_rejected_review_duration(
        native_budget, monkeypatch, remaining):
    store, audit, clock = native_budget
    review_setup(monkeypatch)
    votes, reviews = [], []

    def review(*args):
        reviews.append(True)
        clock[0] += 20
        if len(reviews) == 1:
            raise AutError("invalid citations\nrejected local response")
        return accepted_review()

    monkeypatch.setattr(judge, "_llm_review", review)
    adjusted = False

    def after_rejection(progress):
        nonlocal adjusted
        if not adjusted and len(store.keys("review_attempts")) == 1:
            clock[0] = 180 - remaining
            adjusted = True

    kwargs = {"models": ("local",), "budget_s": 180, "max_calls": 24,
              "explain_model": "review",
              "backend_for": backend_for(clock, votes, {"local": 1}, "fail")}
    first = judge.diagnose(store, audit, progress=after_rejection, **kwargs)
    if remaining == 5:
        assert not first["complete"] and first["reason"] == "budget"
        assert first["calls"] == 2 and len(reviews) == 1
        assert len(store.keys("review_attempts")) == 1
        clock[0] = 0
        result = judge.diagnose(store, audit, **kwargs)
        assert result["calls"] == 1
    else:
        assert first["complete"] and first["calls"] == 3
        result = first
    assert result["complete"] and len(votes) == 1 and len(reviews) == 2
    assert len(store.keys("review_attempts")) == 2
    judgment = store.get("judgments", result["judgment_ids"][0])
    assert judgment["review_errors"] == ["invalid citations\nrejected local response"]


def test_admission_rechecks_after_memory_guard_consumes_time(native_budget, monkeypatch):
    store, audit, clock = native_budget
    calls = []
    monkeypatch.setattr(judge, "memory_guard", lambda: clock.__setitem__(0, 100))
    result = judge.diagnose(store, audit, models=("cold",), budget_s=180,
                            backend_for=backend_for(clock, calls, {"cold": 20}))
    assert not result["complete"] and result["reason"] == "budget"
    assert result["calls"] == 0 and not calls


def test_faster_later_call_does_not_replace_the_model_high_water(native_budget):
    store, audit, clock = native_budget
    manifest = store.get("audits", audit)
    session = store.get("sessions", manifest["session_ids"][0])
    session["events"][0]["text"] = "Check a second result"
    second = store.put("sessions", session)
    audit = store.put("audits", {"session_ids": [*manifest["session_ids"], second]})
    calls = []

    class Backend:
        model_id = "local"
        model_rev = "synthetic"

        def distribution(self, request):
            calls.append(request.task)
            clock[0] += 20 if len(calls) == 1 else 1
            return {"pass": .99, "fail": .005, "unknown": .005}

    def after_two(progress):
        if progress["finished"] == 2:
            clock[0] = 170

    result = judge.diagnose(store, audit, models=("local",), budget_s=180,
                            backend_for=lambda _: Backend(), progress=after_two)
    assert result["reason"] == "budget" and not result["complete"]
    assert result["finished"] == 2 and result["calls"] == len(calls) == 2


def test_cached_explanatory_review_needs_no_native_call_window(native_budget, monkeypatch):
    store, audit, clock = native_budget
    review_setup(monkeypatch)
    calls, reviews = [], []

    def review(*args):
        reviews.append(True)
        clock[0] += 1
        return accepted_review()

    monkeypatch.setattr(judge, "_llm_review", review)
    original = store.put

    def interrupted(kind, value):
        if kind == "judgments":
            raise RuntimeError("interrupted judgment persistence")
        return original(kind, value)

    kwargs = {"models": ("local",), "budget_s": 180, "explain_model": "review",
              "backend_for": backend_for(clock, calls, {"local": 1}, "fail")}
    with monkeypatch.context() as patcher:
        patcher.setattr(store, "put", interrupted)
        with pytest.raises(RuntimeError, match="judgment persistence"):
            judge.diagnose(store, audit, **kwargs)
    clock[0] = 0
    result = judge.diagnose(store, audit, deadline=5, **kwargs)
    assert result["complete"] and result["calls"] == 0
    assert len(calls) == len(reviews) == 1


def test_zero_elapsed_review_does_not_admit_repair_at_the_deadline(native_budget, monkeypatch):
    store, audit, clock = native_budget
    review_setup(monkeypatch)
    calls, reviews = [], []

    def review(*args):
        reviews.append(True)
        if len(reviews) == 1:
            raise AutError("invalid citations")
        return accepted_review()

    def at_deadline(progress):
        if store.keys("review_attempts"):
            clock[0] = 180

    monkeypatch.setattr(judge, "_llm_review", review)
    result = judge.diagnose(store, audit, models=("local",), budget_s=180,
                            explain_model="review", progress=at_deadline,
                            backend_for=backend_for(clock, calls, {"local": 0}, "fail"))
    assert not result["complete"] and result["reason"] == "budget"
    assert result["calls"] == 2 and len(reviews) == 1
