"""Shared for every test folder."""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture(autouse=True)
def _sink_has_no_repo_keys(monkeypatch):
    """A scan with no salt file asks the sink whether this machine already has repo keys
    (repo_keys.require_readable_salt). The tests' DSNs reach no sink: answer "none". A test of that
    check sets repo_keys.connect itself."""
    from agent_usage_tracker import repo_keys

    class _NoKeys:
        def execute(self, sql, params):
            return self

        def fetchone(self):
            return None

    monkeypatch.setattr(repo_keys, "connect", lambda dsn, timeout=15: contextlib.nullcontext(_NoKeys()))
