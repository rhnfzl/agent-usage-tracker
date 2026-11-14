"""Control sampling prefers captured evidence without changing completeness rules."""

import pytest

from agent_usage_tracker.improve import judge
from agent_usage_tracker.improve.store import Store


def turn(name, *, clipped=False, calls=0):
    def event(key, kind, text, call="", **extra):
        return {"id": key, "kind": kind, "text": text, "call_id": call,
                "status": "unknown", "truncated": False, **extra}

    rows = [event(name, "prompt", name)]
    for i in range(calls):
        key = f"{name}-{i}"
        rows.extend([event(key, "call", "check", key),
                     event(key + "-result", "result", "passed", key, status="success")])
    return rows + [event(name + "-reply", "reply", "Observed result", truncated=clipped),
                   event(name + "-end", "boundary", "turn_complete")]


def sample(tmp_path, events, **coverage):
    store = Store(tmp_path / "private")
    snapshot = store.put("sessions", {"version": 7, "agent": "codex", "session_id": "source",
        "origin": "interactive", "coverage": {"complete": False, **coverage}, "events": events})
    audit = store.put("audits", {"session_ids": [snapshot]})
    return judge.cases(store, audit)


@pytest.mark.parametrize("later", [turn("clipped", clipped=True), turn("too-large", calls=20)])
def test_control_selection_prefers_a_complete_attempt_that_fits_its_budget(tmp_path, later):
    rows = sample(tmp_path, turn("usable") + later)
    assert len(rows) == 1
    assert rows[0]["events"][0]["id"] == "usable"
    assert rows[0]["coverage"]["complete"]
    assert rows[0]["omitted_task_events"] == 0


def test_control_search_does_not_scan_unbounded_history(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "MAX_CONTROL_CANDIDATES", 2, raising=False)
    events = turn("old-usable") + turn("recent-one", clipped=True) + turn("recent-two", clipped=True)
    row = sample(tmp_path, events)[0]
    assert row["events"][0]["id"] == "recent-two"
    assert not row["coverage"]["complete"]


def test_real_source_conflicts_are_not_repaired_by_selecting_another_control(tmp_path):
    row = sample(tmp_path, turn("earlier") + turn("later"), conflicting_records=1)[0]
    assert not row["coverage"]["complete"]
    assert row["coverage"]["conflicting_records"] == 1
