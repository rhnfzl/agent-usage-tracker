"""Shared for the improve tests."""

import pytest

from agent_usage_tracker.improve import judge, rubric


@pytest.fixture(autouse=True)
def core_rubrics(monkeypatch):
    """Judge tests run the two core rubrics from the package files, never a user's rubric folder.
    Tests of the newer rubrics set judge._rubrics themselves."""
    core = [item for item in rubric.load() if item.blocking]
    assert [item.id for item in core] == ["avoidable_recovery", "completion_supported"]
    monkeypatch.setattr(judge, "_rubrics", lambda: core)
    return core
