"""Hermetic tests for how push picks its transport and writes one Postgres transaction.

Run: `python tests/test_push.py`  (or `pytest`).
Locks the rule: Postgres delivery puts activity rows in the same transaction as usage + session rows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import push  # noqa: E402
from agent_usage_tracker.config import AutError  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import UsageEvent  # noqa: E402
from fake_psycopg import _fake_psycopg  # noqa: E402
from test_activity import _acts, _cfg, _collect, _run  # noqa: E402


def test_pg_one_transaction() -> None:
    def check(home, repo):
        batch = _collect(home)
        for e in batch:
            e.machine = "test"
        send = push.make_sender(_cfg(home, pg_dsn="postgresql://x"))
        log: list = []
        with _fake_psycopg(log):
            send(batch)
        assert [x if x == "commit" else x[0].split(" (")[0] for x in log] == [
            "INSERT INTO agents.usage_event", "INSERT INTO agents.event_session",
            "INSERT INTO agents.activity_event", "commit"], log
        sql, rows = log[2]
        assert "ON CONFLICT (event_id, ts) DO NOTHING" in sql
        assert len(rows) == len(_acts(batch))
        assert all(json.loads(r[-1]) == e.attrs for r, e in zip(rows, _acts(batch)))
        assert len(log[0][1]) == sum(isinstance(e, UsageEvent) for e in batch)

        log = []
        with _fake_psycopg(log, fail_on="activity_event"):
            try:
                send(batch)
                raise AssertionError("a failed activity insert must raise")
            except RuntimeError as exc:
                assert "insert failed" in str(exc)
        assert "commit" not in log, log
    _run(check)
    print("ok pg_one_transaction")


def test_sender_config() -> None:
    from agent_usage_tracker.push import make_sender
    # nothing configured -> refuse to send
    try:
        make_sender(_cfg(Path("/tmp")))
        raise AssertionError("expected AutError for no transport")
    except AutError:
        pass
    # webhook url set but secret missing -> fail closed (no unauthenticated POST)
    c = Config(machine="t", agents=(), webhook_url="https://n8n.example/webhook/x",
               webhook_secret=None, pg_dsn=None, home=Path("/tmp"))
    try:
        make_sender(c)
        raise AssertionError("expected AutError for missing secret")
    except AutError:
        pass
    # Allowlist, not blocklist: plaintext http:// to a non-local host AND any non-http(s) scheme
    # (ftp/file/no-scheme) must all be refused so the secret can't leak or misfire.
    for url in ("http://evil.example/h", "HTTP://evil.example/h", "Http://evil.example/h",
                "ftp://evil.example/h", "file:///etc/passwd", "//evil.example/h", "evil.example/h"):
        cc = Config(machine="t", agents=(), webhook_url=url, webhook_secret="s",
                    pg_dsn=None, home=Path("/tmp"))
        try:
            make_sender(cc)
            raise AssertionError(f"expected AutError for {url}")
        except AutError:
            pass
    # http://localhost (testing) and any https:// are allowed
    for url in ("http://localhost:5678/h", "HTTPS://ok.example/h"):
        make_sender(Config(machine="t", agents=(), webhook_url=url, webhook_secret="s",
                           pg_dsn=None, home=Path("/tmp")))
    print("ok sender_config")


def test_cf_access_headers() -> None:
    from agent_usage_tracker import push
    from agent_usage_tracker.config import Config
    both = Config(machine="t", agents=(), webhook_url="https://x/h", webhook_secret="s",
                  pg_dsn=None, home=Path("/tmp"),
                  cf_access_client_id="id.abc", cf_access_client_secret="sec.xyz")
    assert both.cf_access_headers == {"CF-Access-Client-Id": "id.abc",
                                      "CF-Access-Client-Secret": "sec.xyz"}
    # only one half set -> no headers (never send a lone token half)
    partial = Config(machine="t", agents=(), webhook_url="https://x/h", webhook_secret="s",
                     pg_dsn=None, home=Path("/tmp"), cf_access_client_id="id.abc")
    assert partial.cf_access_headers == {}
    # _post_webhook merges the extra headers onto the request alongside X-AUT-Secret
    captured = {}

    class _Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=0):
            captured.update(req.headers)
            return _Resp()

    orig = push.urllib.request.build_opener
    push.urllib.request.build_opener = lambda *handlers: _Opener()
    try:
        push._post_webhook("https://x/h", "sec", [], extra_headers=both.cf_access_headers)
    finally:
        push.urllib.request.build_opener = orig
    keys = {k.lower() for k in captured}
    assert {"cf-access-client-id", "cf-access-client-secret", "x-aut-secret"} <= keys, captured
    print("ok cf_access_headers")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
