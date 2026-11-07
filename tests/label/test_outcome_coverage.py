"""The unconnected draft planner cannot write labels or advance live collection progress."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent_usage_tracker.label import outcome_coverage as coverage, tasks
from agent_usage_tracker.label.evalset import scores_path

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def request(uid="e", age=14, rev="r1"):
    return tasks.TASKS["soft_outcome"].request("episode", uid, NOW,
        {"days_idle": age, "last_steers": ["question"], "last_agent_reply": "answer"}, rev=rev)


def test_maturity_crossing_changes_key_but_later_idle_age_does_not():
    assert coverage.coverage_key(request(age=13), "p")[1] is False
    assert coverage.coverage_key(request(age=14), "p")[1] is True
    assert coverage.coverage_key(request(age=13), "p")[0] != coverage.coverage_key(request(), "p")[0]
    assert coverage.coverage_key(request(), "p") == coverage.coverage_key(request(age=90), "p")


def test_changed_revision_policy_or_text_requires_new_coverage():
    base = request()
    key = coverage.coverage_key(base, "p")[0]
    assert coverage.coverage_key(request(rev="r2"), "p")[0] != key
    assert coverage.coverage_key(base, "other-policy")[0] != key
    changed = replace(base, facts={**base.facts, "last_agent_reply": "new answer"})
    assert coverage.coverage_key(changed, "p")[0] != key


def test_duplicate_discovery_delayed_runs_and_acknowledged_resume():
    old, recent = request("old", age=100), request("new", age=1)
    result = coverage.pending_outcomes([old, recent, old], set(), "p")
    assert [r.unit_id for k, r in result] == ["new", "old"]
    # A failed delivery acknowledges nothing; an unknown delivered label acknowledges its key.
    assert coverage.pending_outcomes([old, recent], set(), "p") == result
    done = {result[0][0]}
    assert [r.unit_id for k, r in coverage.pending_outcomes([old, recent], done, "p")] == ["old"]
    assert coverage.pending_outcomes([request("old", age=101)], {result[1][0]}, "p") == []


def test_each_phase_makes_progress_inside_a_two_request_budget():
    requests = [request(f"m{i}", 30) for i in range(3)] + [request(f"r{i}", 1) for i in range(3)]
    done = set()
    for _ in range(3):
        batch = coverage.pending_outcomes(requests, done, "p")[:2]
        assert [coverage.coverage_key(r, "p")[1] for k, r in batch] == [False, True]
        done.update(k for k, r in batch)
    assert coverage.pending_outcomes(requests, done, "p") == []


def test_conflicting_snapshot_is_rejected():
    with pytest.raises(ValueError, match="one current snapshot"):
        coverage.pending_outcomes([request(), request(rev="changed")], set(), "p")


def test_one_request_resumes_persist_next_phase_even_when_recent_work_keeps_arriving():
    done, next_mature, delivered = set(), False, []
    old = request("mature", age=90)
    for day in range(4):
        recent = request(f"recent-{day}", age=1)
        batch = coverage.pending_outcomes([old, recent], done, "p", start_mature=next_mature)[:1]
        for key, item in batch:
            # Only an acknowledged delivery changes either the key set or phase.
            done.add(key)
            next_mature = not coverage.coverage_key(item, "p")[1]
            delivered.append(item.unit_id)
    assert delivered[:2] == ["recent-0", "mature"]


@pytest.mark.parametrize("age", [-1, None, True, 1.5])
def test_invalid_age_is_rejected(age):
    with pytest.raises(ValueError):
        coverage.coverage_key(request(age=age), "p")


def test_writing_is_explicit_versioned_draft_with_separate_cache_identity():
    old = tasks.TASKS["work_category"]
    draft = tasks.writing_task()
    assert old.ver == 2 and "writing" not in dict(old.candidates)
    assert draft.ver == 3 and len(draft.candidates) == 10
    assert [label for label, desc in draft.candidates][4:7] == ["review", "writing", "learning"]
    before = old.request("episode_day", "e:day", NOW, {"first_steer": "author docs"})
    after = draft.request("episode_day", "e:day", NOW, before.facts)
    assert before.unit_id == after.unit_id and before.task == after.task
    assert before.prompt_ver != after.prompt_ver
    assert scores_path(before.task, "model", "rev", before.prompt_ver) != scores_path(
        after.task, "model", "rev", after.prompt_ver)


def test_bounded_outcome_contract_is_unregistered_and_has_separate_cache_identity():
    old, draft = tasks.TASKS["soft_outcome"], tasks.bounded_outcome_task()
    assert old.ver == 2 and draft.ver == 3
    assert old.candidates == draft.candidates
    before = old.request("episode", "stable-episode", NOW, {"days_idle": 40}, rev="same-rev")
    after = draft.request("episode", "stable-episode", NOW, before.facts, rev=before.rev)
    assert before.unit_id == after.unit_id and before.task == after.task
    assert before.prompt_ver != after.prompt_ver
    assert scores_path(before.task, "model", "rev", before.prompt_ver) != scores_path(
        after.task, "model", "rev", after.prompt_ver)
    assert coverage.coverage_key(before, "policy") != coverage.coverage_key(after, "policy")


def test_outcome_contract_three_refuses_legacy_source(tmp_path):
    from agent_usage_tracker.config import AutError
    from label_helpers import FakeSource, _cfg
    with pytest.raises(AutError, match="opted-in"):
        tasks._outcome_units(tasks.bounded_outcome_task(), FakeSource(_cfg(tmp_path)), NOW, NOW)
