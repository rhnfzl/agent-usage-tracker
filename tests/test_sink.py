"""Hermetic tests for the sink boundary: the connect point, the webhook fallback, and
the failures every urllib caller must survive.

Run: `python tests/test_sink.py`  (or `pytest`).
"""

from __future__ import annotations

import contextlib
import http.client
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_usage_tracker import pricing, push, sink  # noqa: E402
from agent_usage_tracker.collectors import cursor_api  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import (ActivityEvent, CollectorUnavailable, EventSession,  # noqa: E402
                                       UsageEvent)
from test_activity import _fake_psycopg  # noqa: E402

WEBHOOK = "https://hooks.example/h"
DSN = "postgresql://localhost/agents"


def _cfg(webhook: str | None = WEBHOOK, dsn: str | None = DSN, **kw) -> Config:
    return Config(machine="test", agents=(), webhook_url=webhook,
                  webhook_secret="s" if webhook else None, pg_dsn=dsn,
                  home=Path(tempfile.gettempdir()), **kw)


def _usage(session: bool = True) -> UsageEvent:
    return UsageEvent(event_id="u1", ts="2026-07-18T09:00:00Z", agent="fake", model="m",
                      provider="p", machine="test",
                      session=EventSession("s1", None, "interactive", None) if session else None)


def _act() -> ActivityEvent:
    return ActivityEvent(event_id="a1", ts="2026-07-18T09:00:00Z", agent="fake", kind="steer",
                         session_id="s1", turn_id=None, attrs={}, machine="test")


def _tables(log: list) -> list[str]:
    return [x if x == "commit" else x[0].split(" (")[0].removeprefix("INSERT INTO ") for x in log]


@contextlib.contextmanager
def _webhook(fn):
    saved = push._post_webhook
    push._post_webhook = fn
    try:
        yield
    finally:
        push._post_webhook = saved


def _raiser(exc):
    def post(url, secret, batch, headers=None):
        raise exc
    return post


def test_connect_warns_only_when_tls_is_not_required() -> None:
    env = {k: os.environ.pop(k) for k in ("PGHOST", "PGSSLMODE") if k in os.environ}
    cases = {
        "postgresql://agents_writer:hunter2@db.example:5432/agents": True,
        "postgresql://db.example/agents?sslmode=prefer": True,
        "host=db.example dbname=agents password=hunter2": True,
        "postgresql://db.example/agents?sslmode=require": False,
        "host=db.example sslmode='verify-full'": False,
        "postgresql://db.example/agents?sslmode=disable": False,    # the stated opt-out
        "postgresql://user:pw@localhost:15432/agents": False,       # an SSH tunnel
        "postgresql://127.0.0.1/agents": False,
        "dbname=agents": False,                                     # the local socket
        "postgresql:///agents?host=/var/run/postgresql": False,
    }
    try:
        for dsn, warns in cases.items():
            sink._warn_if_plaintext.cache_clear()
            with contextlib.redirect_stderr(io.StringIO()) as err:
                sink._warn_if_plaintext(dsn)
                sink._warn_if_plaintext(dsn)                        # once per run
            assert err.getvalue().count("\n") == (1 if warns else 0), (dsn, err.getvalue())
            assert "hunter2" not in err.getvalue(), err.getvalue()
    finally:
        os.environ.update(env)
        sink._warn_if_plaintext.cache_clear()
    print("ok connect_warns_only_when_tls_is_not_required")


def test_a_warned_dsn_still_connects() -> None:
    log: list = []
    with _fake_psycopg(log), contextlib.redirect_stderr(io.StringIO()):
        push.insert_pg("postgresql://db.example/agents", [_act()])
    assert _tables(log) == ["agents.activity_event", "commit"], log
    sink._warn_if_plaintext.cache_clear()
    print("ok a_warned_dsn_still_connects")


def test_webhook_failure_warns_once_and_falls_back() -> None:
    log: list = []
    send = push.make_sender(_cfg())
    # BadStatusLine is no OSError: it must reach the fallback, not escape past it
    with _webhook(_raiser(http.client.BadStatusLine("garbled"))), _fake_psycopg(log), \
            contextlib.redirect_stderr(io.StringIO()) as err:
        send([_usage()])
        send([_usage()])
    assert err.getvalue().count("\n") == 1 and "webhook delivery failed" in err.getvalue()
    assert "BadStatusLine" in err.getvalue(), err.getvalue()
    assert _tables(log).count("agents.usage_event") == 2, log
    print("ok webhook_failure_warns_once_and_falls_back")


def test_double_failure_reports_both_causes() -> None:
    log: list = []
    send = push.make_sender(_cfg())
    with _webhook(_raiser(RuntimeError("webhook HTTP 403"))), \
            _fake_psycopg(log, fail_on="usage_event"), \
            contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            send([_usage()])
            raise AssertionError("a double failure must raise")
        except RuntimeError as exc:
            assert "insert failed" in str(exc)
    assert "webhook HTTP 403" in err.getvalue(), err.getvalue()
    assert "commit" not in log, log
    print("ok double_failure_reports_both_causes")


def test_webhook_success_sends_every_other_row_over_pg() -> None:
    log: list = []
    posted: list = []
    send = push.make_sender(_cfg())
    with _webhook(lambda url, secret, b, h=None: posted.append(b)), _fake_psycopg(log), \
            contextlib.redirect_stderr(io.StringIO()) as err:
        send([_usage(), _act()])
    assert [e.event_id for e in posted[0]] == ["u1"]
    # one transaction, and never the usage rows the webhook already carried
    assert _tables(log) == ["agents.event_session", "agents.activity_event", "commit"], log
    assert err.getvalue() == "", err.getvalue()

    log.clear()
    with _webhook(lambda url, secret, b, h=None: None), _fake_psycopg(log):
        send([_usage(session=False)])
    assert log == [], "nothing but usage: no connection"
    print("ok webhook_success_sends_every_other_row_over_pg")


def test_half_cloudflare_config_warns_and_still_sends() -> None:
    for kw, warns in (({"cf_access_client_id": "id"}, True),
                      ({"cf_access_client_secret": "sec"}, True),
                      ({"cf_access_client_id": "id", "cf_access_client_secret": "sec"}, False),
                      ({}, False)):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert callable(push.make_sender(_cfg(**kw)))
        assert ("Cloudflare" in err.getvalue()) == warns, (kw, err.getvalue())
    print("ok half_cloudflare_config_warns_and_still_sends")


def test_heartbeat_warning_keeps_the_message_without_the_credential() -> None:
    def broken(dsn, rows):
        raise RuntimeError("could not connect:\n  postgresql://aut:hunter2@db.example/agents")
    saved = push.send_scan_rows
    push.send_scan_rows = broken
    try:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            push.deliver_scan_rows(_cfg(), [{}], "label")
    finally:
        push.send_scan_rows = saved
    text = err.getvalue()
    assert text.count("\n") == 1 and "label heartbeat not delivered: RuntimeError" in text, text
    assert "could not connect" in text and "hunter2" not in text, text
    print("ok heartbeat_warning_keeps_the_message_without_the_credential")


def test_require_pg() -> None:
    assert _cfg().require_pg("x") == DSN
    try:
        _cfg(dsn=None).require_pg("time add")
        raise AssertionError("no DSN must be an operator error")
    except SystemExit as exc:
        assert str(exc) == "time add needs transport.pg_dsn"
    print("ok require_pg")


def test_truncated_body_is_a_handled_fetch_failure() -> None:
    def truncated(*a, **k):
        raise http.client.IncompleteRead(b"")

    saved = pricing.urllib.request.urlopen
    pricing.urllib.request.urlopen = truncated
    try:
        assert pricing._fetch_json("https://prices.example/x") is None
    finally:
        pricing.urllib.request.urlopen = saved

    saved_auth, saved_post = cursor_api._auth, cursor_api._post
    cursor_api._auth, cursor_api._post = (lambda cfg: ("cookie", 0)), truncated
    try:
        try:
            list(cursor_api.collect(_cfg(cursor_api=True), {}))
            raise AssertionError("a failed fetch must report the source unavailable")
        except CollectorUnavailable as exc:
            assert "skipping this run" in str(exc), exc
    finally:
        cursor_api._auth, cursor_api._post = saved_auth, saved_post
    print("ok truncated_body_is_a_handled_fetch_failure")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
