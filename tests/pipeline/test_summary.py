"""Offline report on two native formats, using the same records as the export."""

import argparse
import csv
import io
import json
from datetime import date, datetime, timezone
from urllib.request import OpenerDirector

import pytest

from agent_usage_tracker import scan, summary, summary_cli
from agent_usage_tracker.config import AutError, Config

SINCE = date(2026, 6, 15)
UNTIL = date(2026, 6, 16)


def write_summary_logs(home):
    def write(path, *rows):
        path = home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    claude = {"type": "assistant", "uuid": "block", "requestId": "request",
              "timestamp": "2026-06-14T22:00:00Z", "cwd": "/example/orchard",
              "sessionId": "session", "message": {
                  "id": "message", "model": "model-a",
                  "usage": {"input_tokens": 10, "output_tokens": 5}}}
    write(".claude/projects/orchard/main.jsonl", claude,
          dict(claude, requestId="before", timestamp="2026-06-14T21:59:59Z"),
          dict(claude, requestId="after", timestamp="2026-06-15T22:00:00Z"))
    write(".claude/projects/orchard/copy.jsonl", claude)
    write(".pi/agent/sessions/orchard/main.jsonl", {
        "type": "message", "id": "synthetic-message", "timestamp": "2026-06-15T10:00:00Z",
        "message": {"model": "model-b", "usage": {"input": 3, "output": 2, "reasoning": 0,
                                                  "cost": {"total": 0.02}}},
    })
    return Config("private-machine", ("claude_code", "pi"), None, None, None, home)


@pytest.mark.parametrize(("group_by", "keys"), [
    ("agent", {"claude_code": 15, "pi": 5}),
    ("model", {"model-a": 15, "model-b": 5}),
    ("project", {"orchard": 15, None: 5}),
])
def test_grouped_summary_reconciles_to_export(tmp_path, group_by, keys):
    cfg = write_summary_logs(tmp_path)
    report = summary.build_summary(cfg, since=SINCE, until=UNTIL, group_by=group_by)
    assert report["window"] == {"since": str(SINCE), "until": str(UNTIL),
                                "timezone": "Europe/Amsterdam"}
    assert {g["key"]: g["tokens"]["total_tokens"] for g in report["groups"]} == keys
    out = io.StringIO()
    assert scan.run_export(cfg, cfg.agents, out) == (4, 0, set())
    rows = [r for r in csv.DictReader(io.StringIO(out.getvalue()))
            if "2026-06-14T22:00:00Z" <= r["ts"] < "2026-06-15T22:00:00Z"]
    assert len(rows) == report["totals"]["events"] == 2
    for key in summary.COUNTS:
        measured = [int(r[key]) for r in rows if r[key]]
        assert report["totals"]["tokens"][key] == sum(measured)
        assert report["totals"]["unknown_events"][key] == len(rows) - len(measured)
    assert report["totals"]["tokens"]["reasoning_tokens"] == 0
    assert report["totals"]["logged_cost_usd"] == 0.02
    assert report["totals"]["unknown_events"]["logged_cost_usd"] == 1
    assert [source["events"] for source in report["sources"]] == [1, 1]
    assert "private-machine" not in json.dumps(report)
    assert "/example/" not in json.dumps(report)


def test_registered_summary_is_offline_and_read_only(tmp_path, monkeypatch, capsys):
    cfg = write_summary_logs(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")}
    state = tmp_path / "state.json"
    state.write_text('{"some": "watermark"}')
    monkeypatch.setenv("AUT_STATE", str(state))

    def fail(*args, **kwargs):
        pytest.fail("offline report opened a connection")

    monkeypatch.setattr(OpenerDirector, "open", fail)
    monkeypatch.setattr(scan, "make_sender", fail)
    parser = argparse.ArgumentParser()
    summary_cli.register(parser.add_subparsers())
    args = parser.parse_args(["summary", "--json", "--since", str(SINCE), "--until", str(UNTIL)])
    from dataclasses import replace
    summary_cli.run(replace(cfg, pg_dsn="postgresql://example.invalid/db", cursor_api=True),
                    (*cfg.agents, "cursor_api", "activitywatch"), args)
    output = json.loads(capsys.readouterr().out)
    assert [s["status"] for s in output["sources"]] == [
        "observed", "observed", "offline", "not_usage_source"]
    assert state.read_text() == '{"some": "watermark"}'
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")} == before
    assert not (tmp_path / "repo_key.salt").exists()
    text = summary.render(output)
    assert "Source coverage" in text and "Events with unknown values" in text


def test_calendar_periods_and_explicit_bounds(tmp_path):
    cfg = write_summary_logs(tmp_path)
    now = datetime(2026, 6, 16, 22, tzinfo=timezone.utc)
    assert [summary.build_summary(cfg, period=p, now=now)["window"]["since"]
            for p in ("today", "week", "month")] == ["2026-06-17", "2026-06-15", "2026-06-01"]
    assert summary.build_summary(cfg, period="week", now=now)["window"]["until"] == "2026-06-18"
    with pytest.raises(AutError, match="--since and --until"):
        summary.build_summary(cfg, since=SINCE)
