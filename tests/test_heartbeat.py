"""Hermetic tests for the per-collector scan heartbeat (agents.collector_scan).

Run: `python tests/test_heartbeat.py`  (or `pytest`).
Locks the rules that matter for a heartbeat that rides alongside usage delivery:
  - every collector that ran gets one row, 'ok' or 'error', and an error still re-raises;
  - all rows go out in ONE delivery call, and a failing delivery never changes the scan;
  - dry-run sends nothing;
  - error text is short and never carries a DSN or URL with credentials.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import __version__, cli, push  # noqa: E402
from agent_usage_tracker.config import Config  # noqa: E402
from agent_usage_tracker.event import UsageEvent  # noqa: E402


def _cfg(pg_dsn: str | None = None, webhook_url: str | None = None) -> Config:
    return Config(machine="test", agents=(), webhook_url=webhook_url,
                  webhook_secret="s" if webhook_url else None, pg_dsn=pg_dsn,
                  home=Path(tempfile.gettempdir()))


def _ev(eid: str, ts: str | None, kind: str = "agent") -> UsageEvent:
    return UsageEvent(event_id=eid, ts=ts, agent="fake", model="m", provider="p", kind=kind,
                      output_tokens=0 if kind == "human" else 5,
                      total_tokens=0 if kind == "human" else 5)


def _ok_collector(cfg, wm):
    if wm.get("done"):
        return
    # +02:00 is 08:00Z, so a plain string max would wrongly pick it over 09:00Z
    events = [_ev("a1", "2026-07-18T10:00:00+02:00"), _ev("a2", "2026-07-18T09:00:00Z"),
              _ev("h1", "2026-07-18T07:00:00Z", kind="human"), _ev("x1", None),
              # no time zone (valid_ts accepts it): read as UTC, and it must never break the scan
              _ev("a3", "2026-07-18T09:30:00")]
    yield events, lambda: wm.update(done=True)


def _empty_collector(cfg, wm):
    return
    yield  # pragma: no cover


def _bad_collector(cfg, wm):
    raise RuntimeError("boom reading logs")
    yield  # pragma: no cover


@contextlib.contextmanager
def _patched(registry: dict, send_scan_rows):
    """Swap in fake collectors, a recording usage sender and a scan-row sink; isolate state."""
    sent: list[list[str]] = []
    saved = (cli.REGISTRY, cli.make_sender, cli.send_scan_rows, os.environ.get("AUT_STATE"))
    with tempfile.TemporaryDirectory() as d:
        os.environ["AUT_STATE"] = str(Path(d) / "state.json")
        cli.REGISTRY = registry
        cli.make_sender = lambda cfg: (lambda batch: sent.append([e.event_id for e in batch]))
        cli.send_scan_rows = send_scan_rows
        try:
            yield sent, Path(d) / "state.json"
        finally:
            cli.REGISTRY, cli.make_sender, cli.send_scan_rows, prev = saved
            if prev is None:
                os.environ.pop("AUT_STATE", None)
            else:
                os.environ["AUT_STATE"] = prev


def test_scan_rows_ok_and_error() -> None:
    calls: list[list[dict]] = []
    reg = {"good": _ok_collector, "quiet": _empty_collector, "bad": _bad_collector}
    with _patched(reg, lambda cfg, rows: calls.append(list(rows))):
        try:
            cli.run_scan(_cfg(pg_dsn="postgresql://x"), ["good", "quiet", "bad"], dry_run=False)
            raise AssertionError("the collector error must still propagate")
        except RuntimeError as exc:
            assert "boom" in str(exc)
    assert len(calls) == 1, calls                       # ONE delivery, even though a collector raised
    rows = {r["collector"]: r for r in calls[0]}
    assert set(rows) == {"good", "quiet", "bad"}, rows
    good, quiet, bad = rows["good"], rows["quiet"], rows["bad"]
    assert good["status"] == "ok" and good["error"] is None
    assert good["events"] == 3 and good["human"] == 1   # the no-ts record is not counted
    assert good["newest_ts"] == datetime(2026, 7, 18, 9, 30, tzinfo=timezone.utc), good["newest_ts"]
    assert quiet["status"] == "ok" and quiet["events"] == 0 and quiet["newest_ts"] is None
    # only the error TYPE is stored: messages can carry paths or secrets, and stay on local stderr
    assert bad["status"] == "error" and bad["error"] == "RuntimeError", bad
    for r in calls[0]:
        assert r["machine"] == "test" and r["aut_version"] == __version__
        assert r["started_at"].tzinfo is not None and r["started_at"] <= r["finished_at"]
        assert set(r) == set(push.SCAN_COLUMNS), r
    print("ok scan_rows_ok_and_error")


def test_delivery_failure_is_swallowed() -> None:
    def _ok(cfg, rows):
        pass

    def _broken(cfg, rows):
        raise OSError("connection refused")

    def _broken_other(cfg, rows):
        raise ValueError("relation agents.collector_scan does not exist")

    results = []
    for sink in (_ok, _broken, _broken_other):
        err = io.StringIO()
        with _patched({"good": _ok_collector}, sink) as (sent, state_path):
            with contextlib.redirect_stderr(err):
                report = cli.run_scan(_cfg(pg_dsn="postgresql://x"), ["good"], dry_run=False)
            results.append((report, sent, state_path.read_text(), err.getvalue()))
    rep_ok, sent_ok, state_ok, _ = results[0]
    for rep_bad, sent_bad, state_bad, err_bad in results[1:]:
        assert rep_bad == rep_ok and sent_bad == sent_ok and state_bad == state_ok
        assert err_bad.count("\n") == 1 and "heartbeat" in err_bad, err_bad
    assert sent_ok == [["a1", "a2", "h1", "a3"]], sent_ok
    print("ok delivery_failure_is_swallowed")


def test_dry_run_sends_nothing() -> None:
    calls: list = []
    with _patched({"good": _ok_collector}, lambda cfg, rows: calls.append(rows)) as (sent, _):
        cli.make_sender = lambda cfg: (_ for _ in ()).throw(AssertionError("no sender in dry-run"))
        report = cli.run_scan(_cfg(pg_dsn="postgresql://x"), ["good"], dry_run=True)
    assert report["good"]["events"] == 3
    assert calls == [] and sent == [], (calls, sent)
    print("ok dry_run_sends_nothing")


def test_scrub_error() -> None:
    # The stored error is the exception TYPE only. Free text can hold a work repo path or part of
    # a secret, and no redaction pattern catches every form, so none of it leaves the machine.
    for msg in ("could not connect: postgresql://aut:hunter2@db.example/agents",
                "host=db user=aut password='my secret' dbname=agents",
                "No such file: /home/someone/work/client-repo/session.jsonl", "x" * 1000):
        assert push.scrub_error(RuntimeError(msg)) == "RuntimeError", msg
    assert push.scrub_error(KeyError("k")) == "KeyError"
    print("ok scrub_error")


def test_interrupt_is_not_delivered() -> None:
    # Ctrl-C must stop at once: no 5-15 s connect to the sink on the way out.
    def _interrupted(cfg, wm):
        raise KeyboardInterrupt
        yield  # pragma: no cover

    calls: list = []
    with _patched({"stop": _interrupted}, lambda cfg, rows: calls.append(rows)):
        try:
            cli.run_scan(_cfg(pg_dsn="postgresql://x"), ["stop"], dry_run=False)
            raise AssertionError("KeyboardInterrupt must propagate")
        except KeyboardInterrupt:
            pass
    assert calls == [], calls
    print("ok interrupt_is_not_delivered")


def test_send_scan_rows_transport() -> None:
    row = {c: None for c in push.SCAN_COLUMNS} | {"machine": "test", "collector": "good",
                                                  "status": "ok", "events": 1, "human": 0}
    executed: list = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def executemany(self, sql, rows):
            executed.append((sql, rows))

    class _Conn(_Cur):
        def cursor(self):
            return _Cur()

        def commit(self):
            executed.append("commit")

    fake = types.ModuleType("psycopg")
    fake.connect = lambda dsn, connect_timeout: _Conn()
    prev = sys.modules.get("psycopg")
    sys.modules["psycopg"] = fake
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            push.send_scan_rows(_cfg(webhook_url="https://hooks.example/h"), [row])
        assert executed == [] and err.getvalue().count("\n") == 1, (executed, err.getvalue())

        push.send_scan_rows(_cfg(pg_dsn="postgresql://x"), [row])
        sql, rows = executed[0]
        assert "INSERT INTO agents.collector_scan" in sql
        assert "ON CONFLICT (machine, collector, finished_at) DO NOTHING" in sql
        assert rows == [tuple(row[c] for c in push.SCAN_COLUMNS)] and executed[1] == "commit"
    finally:
        if prev is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = prev
    print("ok send_scan_rows_transport")


def main() -> int:
    test_scan_rows_ok_and_error()
    test_delivery_failure_is_swallowed()
    test_dry_run_sends_nothing()
    test_scrub_error()
    test_interrupt_is_not_delivered()
    test_send_scan_rows_transport()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
