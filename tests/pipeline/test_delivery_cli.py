"""Queued rows remain visible and can recover without new collector output."""

from dataclasses import replace

from agent_usage_tracker import cli, push, scan
from fake_psycopg import _fake_psycopg
from test_sink import _act, _cfg


def queued_config(monkeypatch):
    cfg = _cfg(dsn=None)
    monkeypatch.setattr(push, "post_webhook", lambda *args: None)
    push.make_sender(cfg)([_act()])
    return replace(cfg, pg_dsn="postgresql://x")


def test_status_reports_rows_then_explicit_delivery_retry_drains_them(monkeypatch, capsys):
    cfg = queued_config(monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["status"]) == 0
    assert "delivery queue: 1 pending rows" in capsys.readouterr().out
    with _fake_psycopg([]):
        assert cli.main(["retry-delivery"]) == 0
    assert "sent 1 rows" in capsys.readouterr().out
    assert push.outbox_status() == {}


def test_empty_scan_still_recovers_deferred_rows(monkeypatch):
    cfg = queued_config(monkeypatch)
    with _fake_psycopg([]):
        assert scan.run_scan(cfg, [], dry_run=False) == {}
    assert push.outbox_status() == {}
