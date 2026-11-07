"""Shared for every test folder."""

from __future__ import annotations

import contextlib
import threading

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


@pytest.fixture(autouse=True)
def local_page_servers(monkeypatch):
    """Close idle local review servers when an assertion prevents their submit request."""
    from agent_usage_tracker import local_page

    servers = []

    class Server(local_page.HTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append((self, threading.current_thread()))

    def close_all():
        for server, thread in servers:
            if thread is not threading.current_thread() and thread.is_alive():
                server.shutdown()
                thread.join(10)
                assert not thread.is_alive(), "A local review server did not stop"
            server.server_close()

    monkeypatch.setattr(local_page, "HTTPServer", Server)
    yield close_all
    close_all()
