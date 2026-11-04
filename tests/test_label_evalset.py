"""Hermetic tests for label/evalset.py: the local label files and the shared unit order.

Run: `python tests/test_label_evalset.py`  (or `pytest`).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.config import AutError  # noqa: E402
from agent_usage_tracker.label import evalset  # noqa: E402
from agent_usage_tracker.label import tasks  # noqa: E402
from label_helpers import BASE, TASK  # noqa: E402
from agent_usage_tracker.label import core  # noqa: E402
from label_helpers import FakeSource, _cfg, _env  # noqa: E402


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


def _rows(path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_eval_path_names_each_kind(tmp_path):
    with _env(tmp_path):
        base = tmp_path / "data" / "aut" / "eval"
        assert evalset.eval_path("t", "gold") == base / "t.jsonl"
        assert evalset.eval_path("t", "silver") == base / "t.silver.jsonl"
        assert evalset.eval_path("t", "eval") == base / "t.eval.jsonl"


def test_append_and_write_keep_one_sorted_row_per_line(tmp_path):
    p = tmp_path / "new" / "dir" / "x.jsonl"
    evalset.append_jsonl(p, {"b": 1, "a": 2})
    evalset.append_jsonl(p, {"c": 3})
    assert _rows(p) == ['{"a": 2, "b": 1}', '{"c": 3}']
    assert evalset.read_jsonl(p) == [{"a": 2, "b": 1}, {"c": 3}]
    evalset.write_jsonl(p, [{"z": 1}])
    assert _rows(p) == ['{"z": 1}']


def test_read_jsonl_drops_a_cut_line_and_refuses_bad_bytes(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n[2]\n{"cut')
    assert evalset.read_jsonl(p) == [{"a": 1}]
    p.write_bytes(b'{"a": "\xff"}\n')
    with pytest.raises(AutError, match="cannot read"):
        evalset.read_jsonl(p)


def test_gold_latest_takes_the_last_answer_of_this_version(tmp_path):
    ver = tasks.TASKS[TASK].ver
    with _env(tmp_path):
        p = evalset.eval_path(TASK, "gold")
        for row in ({"unit_id": "u1", "gold": "stop", "task_ver": ver},
                    {"unit_id": "u1", "gold": "framing", "task_ver": ver},
                    {"unit_id": "u2", "gold": "stop", "task_ver": ver + 1},
                    {"unit_id": "u3", "gold": "not-a-label", "task_ver": ver}):
            evalset.append_jsonl(p, row)
        assert evalset.gold_latest(TASK) == {"u1": "framing"}


def test_silver_by_unit_groups_teachers_of_this_version(tmp_path):
    ver = tasks.TASKS[TASK].ver
    with _env(tmp_path):
        p = evalset.eval_path(TASK, "silver")
        for row in ({"unit_id": "u", "teacher": "x", "task_ver": ver, "dist": {"stop": 1.0}},
                    {"unit_id": "u", "teacher": "y", "task_ver": ver, "dist": {"framing": 1.0}},
                    {"unit_id": "v", "teacher": "x", "task_ver": ver + 1, "dist": {"stop": 1.0}},
                    {"unit_id": "w", "teacher": "x", "task_ver": ver, "dist": "junk"}):
            evalset.append_jsonl(p, row)
        assert evalset.silver_by_unit(TASK) == {"u": {"x": {"stop": 1.0}, "y": {"framing": 1.0}}}


def test_stratum_ignores_a_teacher_without_an_answer():
    assert evalset.stratum({}) == "unlabelled" and evalset.stratum({"x": {}}) == "unlabelled"
    assert evalset.stratum({"x": {}, "y": {"a": 1.0}}) == "a"


def test_unit_day_is_the_local_day_of_the_unit():
    r = tasks.TASKS[TASK].request("steer", "u", BASE, {})
    assert evalset.unit_day(r) == BASE.date()


def test_encoder_split_follows_the_unit_alone():
    reqs = [tasks.TASKS[TASK].request("steer", f"u{i}", BASE, {}) for i in range(40)]
    sides = [evalset.in_encoder_train_split(r) for r in reqs]
    assert set(sides) == {True, False}
    later = [tasks.TASKS[TASK].request("steer", f"u{i}", BASE - timedelta(days=9), {})
             for i in range(40)]
    assert [evalset.in_encoder_train_split(r) for r in later] == sides


def test_window_units_needs_the_sink_unless_given_a_source(tmp_path):
    with pytest.raises(AutError, match="needs transport.pg_dsn"):
        evalset.window_units(_cfg(tmp_path, pg_dsn=None), TASK)


def test_window_units_reads_whole_local_days_up_to_today(tmp_path, monkeypatch):
    seen = {}
    req = tasks.TASKS[TASK].request("steer", "u", BASE, {})

    def units(task, src, since, until):
        seen.update(task=task, src=src, since=since, until=until)
        return [req]
    monkeypatch.setattr(evalset, "units", units)
    src = FakeSource(_cfg(tmp_path))
    assert evalset.window_units(_cfg(tmp_path), TASK, 7, src, date(2026, 6, 15)) == {"u": req}
    assert seen["task"] == TASK and seen["src"] is src
    assert seen["since"] == core.local_midnight(date(2026, 6, 8))
    assert seen["until"] == core.local_midnight(date(2026, 6, 15))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
