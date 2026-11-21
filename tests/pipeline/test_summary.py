"""Offline report on two native formats, using the same records as the export."""

import argparse
import csv
import io
import json
import os
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.request import OpenerDirector

import pytest

from agent_usage_tracker import pricing, scan, summary, summary_cli
from price_fixture import (CATALOG, PRICE_DAY, PRICE_MODEL, PRICE_TIMESTAMP, price_config,
                           price_rows)
from agent_usage_tracker.config import AutError, Config

SINCE = date(2026, 6, 15)
UNTIL = date(2026, 6, 16)


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))


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


def test_missing_or_bad_price_file_keeps_offline_usage(tmp_path):
    cfg = write_summary_logs(tmp_path)
    path = tmp_path / "model_prices.json"
    for status in ("missing", "invalid"):
        if status == "invalid":
            path.write_text("bad json")
        report = summary.build_summary(cfg, since=SINCE, until=UNTIL, prices_path=path)
        assert report["rate_status"] == status
        assert report["rate_age_seconds"] is None
        assert report["totals"]["tokens"]["total_tokens"] == 20
        assert report["totals"]["pricing"] == {
            "coverage_tokens": 20, "priced_tokens": 0, "unpriced_tokens": 20,
            "unattributed_tokens": 0, "unpriced_events": 2,
            "known_api_cost_usd": None, "estimated_api_cost_usd": None,
        }


def test_measured_price_coverage_and_logged_cost(tmp_path, monkeypatch):
    path = tmp_path / "rates.json"
    pricing.sync_prices(path, fetch=lambda url: CATALOG)
    os.utime(path, (PRICE_TIMESTAMP.timestamp(), PRICE_TIMESTAMP.timestamp()))
    cfg = price_config(tmp_path, monkeypatch, price_rows())
    report = summary.build_summary(cfg, since=PRICE_DAY, until=PRICE_DAY + timedelta(days=1),
                                   prices_path=path, now=PRICE_TIMESTAMP + timedelta(seconds=60))
    assert report["rate_age_seconds"] == 60
    assert report["rate_synced_at"] == PRICE_TIMESTAMP.isoformat()
    totals = report["totals"]
    rates = totals["pricing"]
    assert totals["logged_cost_usd"] == 0.125
    assert Decimal(rates["known_api_cost_usd"]) == Decimal("1.01")
    assert rates["estimated_api_cost_usd"] is None
    assert (rates["coverage_tokens"], rates["priced_tokens"], rates["unpriced_tokens"],
            rates["unattributed_tokens"], rates["unpriced_events"]) == (97, 80, 17, 2, 2)
    models = {model["model"]: model for model in report["prices_by_model"]}
    a, b, missing = (models[key] for key in (PRICE_MODEL, "model-b", "codex-auto-review"))
    assert Decimal(a["estimated_api_cost_usd"]) == Decimal("0.61")
    assert b["unpriced_tokens"] == 7 and Decimal(b["known_api_cost_usd"]) == Decimal("0.4")
    assert b["estimated_api_cost_usd"] is None
    assert missing["model"] == "codex-auto-review" and missing["known_api_cost_usd"] is None
    assert report["price_comparison"] is None
    assert "at least two complete ledger days" in report["price_forecast"]["reason"].lower()


def test_fixed_usage_comparison_requires_both_component_rates(tmp_path, monkeypatch):
    path = tmp_path / "rates.json"
    pricing.sync_prices(path, fetch=lambda url: CATALOG)
    cfg = price_config(tmp_path, monkeypatch, price_rows()[:2])
    settings = dict(since=PRICE_DAY, until=PRICE_DAY + timedelta(days=1), prices_path=path)
    report = summary.build_summary(cfg, compare=("model-a", "model-c"), **settings)
    comparison = report["price_comparison"]
    assert comparison["reason"] is None and comparison["coverage_tokens"] == 87
    assert {key: Decimal(value) for key, value in comparison["model_cost_usd"].items()} == {
        "model-a": Decimal("0.881"), "model-c": Decimal("2.357"),
    }
    report = summary.build_summary(cfg, compare=("model-a", "model-b"), **settings)
    assert report["price_comparison"]["model_cost_usd"] is None
    assert "each token component" in report["price_comparison"]["reason"]


def test_forecast_uses_completed_covered_days_and_explicit_assumptions(tmp_path, monkeypatch):
    path = tmp_path / "rates.json"
    pricing.sync_prices(path, fetch=lambda url: CATALOG)
    row = price_rows()[0]
    first = datetime.combine(PRICE_DAY, datetime.min.time(), timezone.utc)
    until = PRICE_DAY + timedelta(days=5)
    now = first + timedelta(days=4, hours=3)
    rows = [replace(row, event_id=f"day-{day}", ts=(first + timedelta(days=day)).isoformat())
            for day in (0, 2, 4)]
    cfg = price_config(tmp_path, monkeypatch, rows)
    settings = dict(since=PRICE_DAY, until=until, now=now, prices_path=path)
    report = summary.build_summary(cfg, **settings)
    forecast = report["price_forecast"]
    assert forecast["reason"] is None
    assert forecast["until"] == now.date().isoformat()
    assert (forecast["active_days"], forecast["calendar_days"]) == (2, 4)
    assert Decimal(forecast["api_cost_for_30_calendar_days_usd"]) == Decimal("9.15")
    assert Decimal(forecast["api_cost_for_30_active_days_usd"]) == Decimal("18.30")
    assert report["totals"]["events"] == 3
    rows.pop()
    forecast = summary.build_summary(cfg, **settings)["price_forecast"]
    assert "does not cover" in forecast["reason"]
    assert forecast["api_cost_for_30_calendar_days_usd"] is None


def test_malformed_source_record_marks_source_partial(tmp_path):
    cfg = write_summary_logs(tmp_path)
    with (tmp_path / ".claude/projects/orchard/main.jsonl").open("a") as log:
        log.write("bad json\n")
    report = summary.build_summary(cfg, since=SINCE, until=UNTIL)
    assert report["sources"][0]["status"] == "partial"
    assert report["sources"][0]["skipped_records"] == {"malformed_records": 1}
    assert report["totals"]["events"] == 2
