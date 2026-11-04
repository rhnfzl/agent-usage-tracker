"""Hermetic tests for label/evalset.py: the local label files and the shared unit order.

Run: `python tests/test_label_evalset.py`  (or `pytest`).
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.config import AutError  # noqa: E402
from agent_usage_tracker.label import evalset  # noqa: E402
from agent_usage_tracker.label import tasks  # noqa: E402
from label_helpers import BASE, TASK  # noqa: E402


def test_stratified_and_split():
    items = ["a"] * 20 + ["b"] * 2 + ["c"]
    order = evalset.stratified(items, lambda x: x, "seed")
    assert set(order[:3]) == {"a", "b", "c"} and sorted(order) == sorted(items)
    assert order == evalset.stratified(items, lambda x: x, "seed")
    r = tasks.TASKS[TASK].request("steer", "u", BASE, {})
    later = tasks.TASKS[TASK].request("steer", "v", BASE + timedelta(minutes=1), {})
    assert evalset.held_out(r) == evalset.held_out(later)        # same day, same side
    sides = {evalset.held_out(tasks.TASKS[TASK].request("steer", "u", BASE - timedelta(days=d),
                                                         {})) for d in range(20)}
    assert sides == {True, False}
    # a title's ts moves with the window read: its side follows the unit_id, not the day
    t = tasks.TASKS["aw_title_relevance"]
    assert {evalset.held_out(t.request("aw_title", "k1", BASE - timedelta(days=d), {}))
            for d in range(20)} == {evalset.held_out(t.request("aw_title", "k1", BASE, {}))}
    assert evalset.stratum(None) == "unlabelled"
    assert evalset.stratum({"x": {"a": 0.6, "b": 0.4}, "y": {"a": 0.9, "b": 0.1}}) == "a"
    assert evalset.stratum({"x": {"a": 0.6, "b": 0.4}, "y": {"a": 0.1, "b": 0.9}}) == "disagree"


def test_unreadable_jsonl_is_not_an_empty_one(tmp_path):
    assert evalset.read_jsonl(tmp_path / "gone.jsonl") == []
    with pytest.raises(AutError, match="cannot read"):
        evalset.read_jsonl(tmp_path)        # a directory: OSError that is not "missing"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
