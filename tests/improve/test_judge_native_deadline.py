import json
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
    monkeypatch.setattr(judge, "_rubrics", lambda first=judge._rubrics()[:1]: first)
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


def test_failed_decision_keeps_the_unfinished_call_in_its_last_checkpoint(native_budget):
    store, audit, clock = native_budget
    checkpoints = []

    class Backend:
        model_id = "local"
        model_rev = "synthetic"

        def distribution(self, request):
            raise RuntimeError("fixture native failure")

    with pytest.raises(RuntimeError, match="fixture native failure"):
        judge.diagnose(store, audit, models=("local",), backend_for=lambda _: Backend(),
                       progress=checkpoints.append)
    unfinished = checkpoints[-1]["unfinished_call"]
    assert unfinished["kind"] == "decision" and unfinished["model"] == "local"
    assert unfinished["rubric"] == "avoidable_recovery"
    assert store.get("cases", unfinished["case_id"])
    assert not store.keys("votes")


def test_review_checkpoint_clears_only_after_its_attempt_is_durable(native_budget, monkeypatch):
    store, audit, clock = native_budget
    review_setup(monkeypatch)
    votes, reviews, checkpoints = [], [], []

    def review(*args):
        reviews.append(True)
        if len(reviews) == 1:
            raise AutError("fixture invalid citations")
        return accepted_review()

    def checkpoint(report):
        checkpoints.append((len(store.keys("review_attempts")), report))

    monkeypatch.setattr(judge, "_llm_review", review)
    report = judge.diagnose(store, audit, models=("local",), explain_model="review",
                            backend_for=backend_for(clock, votes, {"local": 0}, "fail"),
                            progress=checkpoint)
    admitted = [row["unfinished_call"] for _, row in checkpoints
                if row.get("unfinished_call", {}).get("kind") == "review"]
    assert [row["attempt"] for row in admitted] == [0, 1]
    assert all(row["model"] == "review" and row["model_rev"] == "synthetic" for row in admitted)
    assert any(count == 1 and "unfinished_call" not in row for count, row in checkpoints)
    assert report["complete"] and "unfinished_call" not in report


def loading_backend(clock, calls, load_s, infer_s, loads):
    class Backend:
        model_rev = "synthetic"

        def __init__(self, name):
            self.model_id = name

        def load(self):
            loads.append(self.model_id)
            clock[0] += load_s

        def distribution(self, request):
            calls.append(request.task)
            clock[0] += infer_s
            return {"pass": .99, "fail": .005, "unknown": .005}

    return Backend


def model_calls(store):
    rows = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    return [row for row in rows if row["kind"] == "model_call"]


def test_cold_model_with_a_load_step_is_refused_before_loading(native_budget):
    store, audit, clock = native_budget
    calls, loads = [], []
    result = judge.diagnose(store, audit, models=("cold",), budget_s=180, deadline=60,
                            backend_for=loading_backend(clock, calls, 30, 5, loads))
    assert result["reason"] == "budget" and result["calls"] == 0
    assert not calls and not loads
    assert result["stopped_by"]["model"] == "cold" and result["stopped_by"]["limit"] == "time"
    assert result["stopped_by"]["required_s"] == 90
    assert result["stopped_by"]["remaining_s"] == 60


def test_stopped_by_names_the_call_limit(native_budget):
    store, audit, clock = native_budget
    calls = []
    result = judge.diagnose(store, audit, models=("slow", "fast"), max_calls=1,
                            backend_for=backend_for(clock, calls, {"slow": 1, "fast": 1}))
    assert result["reason"] == "budget" and result["calls"] == 1
    assert result["stopped_by"]["limit"] == "max_calls"


def test_load_and_rate_history_admit_the_first_call_below_half_the_budget(native_budget):
    store, audit, clock = native_budget
    store.append("model_call", call="vote", model="local", model_rev="synthetic",
                 input_chars=2000, load_s=30, infer_s=1.0)
    calls, loads = [], []
    result = judge.diagnose(store, audit, models=("local",), budget_s=180, deadline=80,
                            backend_for=loading_backend(clock, calls, 30, 1, loads))
    assert loads == ["local"]
    assert result["calls"] >= 1 and calls
    assert result["first_call_at"]


def test_recorded_inference_time_excludes_the_load(native_budget):
    store, audit, clock = native_budget
    calls, loads = [], []

    def near_deadline(progress):
        if progress["finished"] == 1 and clock[0] < 169:
            clock[0] = 169

    result = judge.diagnose(store, audit, models=("local",), budget_s=180,
                            backend_for=loading_backend(clock, calls, 30, 5, loads),
                            progress=near_deadline)
    assert result["complete"] and result["calls"] == 2
    assert loads == ["local"]
    rows = model_calls(store)
    assert [row["load_s"] for row in rows] == [30, None, None]
    assert [row["infer_s"] for row in rows] == [None, 5, 5]
    assert all(row["call"] == "vote" and row["model"] == "local" for row in rows)


def interrupted_worker(store, audit, clock, monkeypatch, *, cut):
    """Run the scheduled worker with a backend whose load or inference hits the deadline."""
    from functools import partial

    from agent_usage_tracker.improve import schedule

    class Backend:
        model_rev = "synthetic"

        def __init__(self, name):
            self.model_id = name

        def load(self):
            clock[0] += 30
            if cut == "load":
                raise schedule._BudgetExpired

        def distribution(self, request):
            raise schedule._BudgetExpired

    monkeypatch.setattr(schedule, "diagnose", partial(judge.diagnose, backend_for=Backend))
    policy = store.put("cycle_policies", {"audit_id": audit, "budget_s": 180, "case_limit": 4,
        "max_calls": 4, "models": ["local"], "explain_model": None})
    report = schedule._worker(store, policy, "run")
    saved = json.loads((store.root / "worker_result.json").read_text())["report"]
    assert report["status"] == "deferred" and saved["status"] == "deferred"
    return saved["diagnosis"]


def test_a_load_cut_by_the_deadline_keeps_its_start_and_phase(native_budget, monkeypatch):
    store, audit, clock = native_budget
    diagnosis = interrupted_worker(store, audit, clock, monkeypatch, cut="load")
    assert diagnosis["calls"] == 0 and diagnosis["first_call_at"]
    assert diagnosis["unfinished_call"] == {"kind": "decision", "phase": "load",
                                            "model": "local", "model_rev": "synthetic"}
    assert not (store.root / "events.jsonl").exists() or not model_calls(store)


def test_an_inference_cut_by_the_deadline_keeps_the_journaled_load(native_budget, monkeypatch):
    store, audit, clock = native_budget
    diagnosis = interrupted_worker(store, audit, clock, monkeypatch, cut="inference")
    assert diagnosis["calls"] == 1 and diagnosis["first_call_at"]
    assert diagnosis["unfinished_call"]["phase"] == "inference"
    assert [(row["load_s"], row["infer_s"]) for row in model_calls(store)] == [
        (30, None), (None, None)]


def test_admission_rechecks_after_the_load_consumes_time(native_budget):
    store, audit, clock = native_budget
    calls, loads = [], []
    result = judge.diagnose(store, audit, models=("cold",), budget_s=180,
                            backend_for=loading_backend(clock, calls, 120, 5, loads))
    assert loads == ["cold"]
    assert result["reason"] == "budget" and result["calls"] == 0 and not calls
    assert model_calls(store)[0]["load_s"] == 120
    assert model_calls(store)[0]["infer_s"] is None


def test_a_large_case_does_not_starve_smaller_cases_after_it(native_budget, monkeypatch):
    store, audit, clock = native_budget
    monkeypatch.setattr(judge, "_rubrics", lambda first=judge._rubrics()[:1]: first)
    big = store.put("sessions", {"agent": "codex", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "x" * 25000},
            {"id": "c", "kind": "call", "call_id": "c", "text": "check"},
            {"id": "r", "kind": "result", "call_id": "c", "status": "error", "text": "failed"},
            {"id": "a", "kind": "reply", "text": "It failed"}]})
    small = store.put("sessions", {"agent": "codex", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "y" * 2500},
            {"id": "a", "kind": "reply", "text": "Checked"}]})
    audit = store.put("audits", {"session_ids": [big, small]})
    sizes = [len(json.dumps(row, ensure_ascii=False)) for row in judge.cases(store, audit)]
    assert sizes[0] > 25000 and 2500 < sizes[1] < 4000
    calls = []

    class Backend:
        model_id = "local"
        model_rev = "synthetic"

        def distribution(self, request):
            calls.append(request.unit_id)
            size = len(json.dumps(request.facts["untrusted_historical_evidence"],
                                  ensure_ascii=False))
            clock[0] += 12.5 * size / sizes[0]
            return {"pass": .99, "fail": .005, "unknown": .005}

    def near_deadline(progress):
        if progress["finished"] == 1 and clock[0] < 160:
            clock[0] = 160

    result = judge.diagnose(store, audit, models=("local",), budget_s=180,
                            backend_for=lambda _: Backend(), progress=near_deadline)
    assert result["complete"] and result["calls"] == 2


def test_a_refused_large_case_stays_pending_while_a_smaller_case_is_judged(
        native_budget, monkeypatch):
    store, audit, clock = native_budget
    monkeypatch.setattr(judge, "_rubrics", lambda first=judge._rubrics()[:1]: first)
    big = store.put("sessions", {"agent": "codex", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "x" * 25000},
            {"id": "a", "kind": "reply", "text": "Checked"}]})
    small = store.put("sessions", {"agent": "codex", "origin": "interactive",
        "coverage": {"complete": True}, "events": [
            {"id": "p", "kind": "prompt", "text": "y" * 2500},
            {"id": "a", "kind": "reply", "text": "Checked"}]})
    audit = store.put("audits", {"session_ids": [big, small]})
    # Seeded history: the big case needs about 250 s, the small one about 30 s.
    store.append("model_call", call="vote", model="local", model_rev="synthetic",
                 input_chars=2000, infer_s=10.0)
    calls = []

    class Backend:
        model_id = "local"
        model_rev = "synthetic"

        def distribution(self, request):
            calls.append(request.unit_id)
            clock[0] += 1
            return {"pass": .99, "fail": .005, "unknown": .005}

    first = judge.diagnose(store, audit, models=("local",), budget_s=180,
                           backend_for=lambda _: Backend())
    assert first["calls"] == 1 and first["finished"] == 1
    assert first["reason"] == "budget" and not first["complete"]
    assert first["stopped_by"]["limit"] == "time" and first["stopped_by"]["next_chars"] > 25000
    clock[0] = 0
    later = judge.diagnose(store, audit, models=("local",), budget_s=600,
                           backend_for=lambda _: Backend())
    assert later["complete"] and later["calls"] == 1 and len(calls) == 2


def test_memory_guard_returns_the_free_percentage(monkeypatch):
    from agent_usage_tracker.label import local

    monkeypatch.setattr(local.sys, "platform", "darwin")
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="System-wide memory free percentage: 42%\n"))
    assert local.memory_guard() == 42


def test_explainer_load_is_timed_and_rechecked_like_a_vote_model(native_budget, monkeypatch):
    store, audit, clock = native_budget
    monkeypatch.setattr(judge, "_rubrics", lambda first=judge._rubrics()[:1]: first)
    loads, reviews, votes = [], [], []

    def load():
        loads.append(True)
        clock[0] += 120

    monkeypatch.setattr(judge, "MlxLetter", lambda _: SimpleNamespace(
        model_rev="synthetic", prepare=lambda: None, close=lambda: None, load=load))
    monkeypatch.setattr(judge, "_llm_review", lambda *a: reviews.append(True))
    result = judge.diagnose(store, audit, models=("local",), budget_s=180, explain_model="review",
                            backend_for=backend_for(clock, votes, {"local": 0}, "fail"))
    assert loads == [True] and not reviews
    assert result["reason"] == "budget" and result["stopped_by"]["kind"] == "review"
    review_rows = [row for row in model_calls(store) if row["call"] == "review"]
    assert [(row["load_s"], row["infer_s"]) for row in review_rows] == [(120, None)]
