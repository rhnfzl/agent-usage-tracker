"""Provisional estimates are separate from calibrated labels and never export review text."""

import copy
from dataclasses import asdict

import pytest

from agent_usage_tracker.config import AutError
from agent_usage_tracker.label.baseline import events


def bundle():
    return {
        "version": "working-sonnet-baseline-v1",
        "rows": [
            {
                "task": "steer_intent",
                "unit": "steer",
                "unit_id": "unit-1",
                "ts": "2026-11-01T12:00:00Z",
                "label": "review",
                "baseline_label": "review",
                "label_source": "sonnet",
                "model_id": "claude-sonnet-5-5-medium",
                "weight": 2.5,
                "input_hash": "a" * 64,
                "estimate": True,
                "quality": "provisional",
                "human_verified": False,
                "reviewed_signals": {
                    "disputes_agent_work": {
                        "answer": "yes",
                        "source": "agent_review",
                        "human_verified": False,
                        "question_sha256": "b" * 64,
                        "reason": "PRIVATE REVIEW TEXT",
                        "review_file": "/private/path",
                    }
                },
            }
        ],
    }


def test_provisional_namespace_provenance_and_idempotency():
    b = bundle()
    a = events(b, "laptop", "2026-11-06T12:00:00Z")[0]
    later = events(b, "laptop", "2026-11-07T12:00:00Z")[0]
    assert a.event_id == later.event_id
    assert a.attrs["task"] == "working.steer_intent"
    assert a.attrs["p"] is None and a.attrs["provisional"] is True
    assert a.attrs["population_weight"] == 2.5
    assert a.attrs["signals"]["disputes_agent_work"]["answer"] == "yes"
    assert "PRIVATE" not in str(asdict(a)) and "/private" not in str(asdict(a))
    changed = copy.deepcopy(b)
    changed["rows"][0]["label"] = "correction"
    assert events(changed, "laptop", "2026-11-06T12:00:00Z")[0].event_id != a.event_id


def test_unknown_is_kept_and_duplicate_units_are_rejected():
    b = bundle()
    b["rows"][0]["label"] = "unknown"
    b["rows"][0]["label_source"] = "unknown_review"
    assert events(b, "laptop", "2026-11-06T12:00:00Z")[0].attrs["below_tau"] is True
    b["rows"] *= 2
    with pytest.raises(AutError):
        events(b, "laptop", "2026-11-06T12:00:00Z")


@pytest.mark.parametrize(
    "field,value",
    [
        ("weight", 0),
        ("weight", float("nan")),
        ("weight", True),
        ("label", "not-a-label"),
        ("human_verified", True),
        ("quality", "validated"),
        ("unit", "aw_title"),
        ("ts", "not-a-date"),
        ("unit_id", "/private/work"),
    ],
)
def test_invalid_bundle_is_rejected_before_any_write(field, value):
    b = bundle()
    b["rows"][0][field] = value
    with pytest.raises(AutError):
        events(b, "laptop", "2026-11-06T12:00:00Z")


def test_review_signals_must_match_task_and_preserve_unknown():
    b = bundle()
    s = b["rows"][0]["reviewed_signals"]
    s["disputes_agent_work"]["answer"] = "unknown"
    b["rows"][0]["label"] = "unknown"
    b["rows"][0]["label_source"] = "unknown_review"
    assert (
        events(b, "laptop", "2026-11-06T12:00:00Z")[0].attrs["signals"]["disputes_agent_work"][
            "answer"
        ]
        == "unknown"
    )
    s["external_blocker"] = s["disputes_agent_work"]
    with pytest.raises(AutError):
        events(b, "laptop", "2026-11-06T12:00:00Z")


def test_provisional_labels_do_not_enter_confident_daily_notes():
    from datetime import date
    from agent_usage_tracker.label.run import _note_prompt

    row = events(bundle(), "laptop", "2026-11-06T12:00:00Z")[0]
    text = _note_prompt(date(2026, 11, 1), [(row.event_id, row.ts, row.attrs)], [])
    assert "working.steer_intent" not in text
    assert "- none" in text


def test_preview_never_writes_and_apply_validates_the_entire_file(tmp_path, monkeypatch):
    import json
    from agent_usage_tracker.label import baseline
    from agent_usage_tracker.config import Config

    path = tmp_path / "baseline.json"
    cfg = Config(machine="laptop", agents=(), webhook_url=None, webhook_secret=None, pg_dsn="test", home=tmp_path)
    sent = []
    monkeypatch.setattr(baseline, "send_activity_rows", lambda dsn, rows: sent.append(rows))
    path.write_text(json.dumps(bundle()))
    assert baseline.run(cfg, path)["mode"] == "preview" and sent == []
    assert baseline.run(cfg, path, apply=True)["mode"] == "applied" and len(sent) == 1
    broken = bundle()
    second = copy.deepcopy(broken["rows"][0])
    second["unit_id"] = "unit-2"
    second["weight"] = 0
    broken["rows"].append(second)
    path.write_text(json.dumps(broken))
    with pytest.raises(AutError):
        baseline.run(cfg, path, apply=True)
    assert len(sent) == 1


def test_generic_calibrated_selection_excludes_working_namespace():
    from agent_usage_tracker.label.core import current_labels

    row = events(bundle(), "laptop", "2026-11-06T12:00:00Z")[0]
    trusted = {**row.attrs, "task": "steer_intent", "label": "instruction"}
    selected = current_labels([(row.event_id, row.ts, row.attrs), ("trusted", row.ts, trusted)])
    assert len(selected) == 1 and selected[0]["task"] == "steer_intent"


def test_uncertain_review_cannot_fall_back_to_a_known_primary_label():
    b = bundle()
    b["rows"][0]["reviewed_signals"]["disputes_agent_work"]["answer"] = "unknown"
    with pytest.raises(AutError):
        events(b, "laptop", "2026-11-06T12:00:00Z")
