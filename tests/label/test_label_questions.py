"""label/questions.py: task version 3 has one complete yes/no question per label, and version 2
stays as it was."""

from __future__ import annotations

import json
import re

from agent_usage_tracker.label import questions as qs
from agent_usage_tracker.label.tasks import TASKS


def _complete(q: qs.Question) -> None:
    assert q.task.endswith("?"), q.label
    assert q.definition and q.not_definition, q.label
    assert q.evidence and all(e.strip() for e in q.evidence), q.label
    assert q.do_not_count and all(d.strip() for d in q.do_not_count), q.label


def test_every_label_has_definition_evidence_and_do_not_count():
    for task, questions in qs.QUESTIONS.items():
        assert [q.label for q in questions] == [i for i, _ in TASKS[task].candidates], task
        assert TASKS[task].ver == 2                 # the live multi-class task is untouched
        names = {q.label for q in questions} | set(qs.GROUPS[task])
        for q in (*questions, *qs.GROUP_QUESTIONS[task]):
            _complete(q)
            named = set(re.findall(r"\(([a-z_]+)\)", " ".join(q.do_not_count)))
            assert named & names - {q.label}, f"{task}.{q.label} names no neighbouring label"


def test_groups_partition_the_leaves_and_multi_leaf_groups_have_a_question():
    for task, groups in qs.GROUPS.items():
        leaves = [leaf for g in groups.values() for leaf in g]
        assert sorted(leaves) == sorted(i for i, _ in TASKS[task].candidates), task
        asked = {q.label for q in qs.GROUP_QUESTIONS[task]}
        assert asked <= set(groups)
        rest = {"closed"} if task == "soft_outcome" else set()
        assert {g for g, ls in groups.items() if len(ls) > 1} - rest == asked, task


def test_noul_has_the_structured_shape_and_is_json():
    q = qs.STEER[3]
    body = json.loads(json.dumps(qs.noul("steer_intent", q)))
    assert body["type"] == "noul" and body["question_id"] == "is_correction"
    assert set(body["instructions"]) == {"task", "context", "evaluation_rules"}
    assert body["criteria"]["true"]["examples_of_qualifying_evidence"] == list(q.evidence)
    assert body["criteria"]["false"]["do_not_count"] == list(q.do_not_count)
    assert body["version"].endswith(f"-v{qs.VER}")
