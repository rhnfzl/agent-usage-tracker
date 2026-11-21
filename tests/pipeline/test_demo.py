import argparse
import csv
import io
import json

from agent_usage_tracker import demo_cli
from agent_usage_tracker.demo import fixture
from agent_usage_tracker.scan import run_export


def test_demo_logs_pass_through_the_real_collectors(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unrelated"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "unrelated"))
    home = tmp_path / "fixture"
    config = fixture(home)
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*.jsonl")}
    output = io.StringIO()
    written, skipped, failed = run_export(config, config.agents, output)
    rows = list(csv.DictReader(io.StringIO(output.getvalue())))
    assert written == len(rows) == 3
    assert skipped == 0 and not failed
    assert {row["agent"] for row in rows} == {"claude_code", "codex"}
    assert sum(int(row["total_tokens"]) for row in rows if "2026-06-15" in row["ts"]) == 1140
    assert next(row for row in rows if row["agent"] == "claude_code")["reasoning_tokens"] == ""
    assert {p.relative_to(home): p.read_bytes() for p in home.rglob("*.jsonl")} == before


def test_demo_uses_summary_without_reading_the_caller_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / "unreadable.toml"
    config.write_bytes(b"!unreadable")
    (tmp_path / "model_prices.json").write_text(
        '{"codex-auto-review":{"input_cost":1000,"output_cost":1000,"cache_read_cost":1000}}',
        encoding="utf-8")
    monkeypatch.setenv("AUT_CONFIG", str(config))
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "no-state.json"))
    parser = argparse.ArgumentParser()
    demo_cli.register(parser.add_subparsers(required=True))
    args = parser.parse_args(["demo", "--group-by", "model", "--json"])
    demo_cli.run(args)
    report = json.loads(capsys.readouterr().out)
    assert report["synthetic"] is True and report["group_by"] == "model"
    assert report["window"] == {"since": "2026-06-15", "until": "2026-06-16", "timezone": "UTC"}
    assert report["totals"]["events"] == 2 and report["totals"]["tokens"]["total_tokens"] == 1140
    assert report["totals"]["unknown_events"]["reasoning_tokens"] == 1
    assert {row["key"] for row in report["groups"]} == {"claude-opus-4-8", "codex-auto-review"}
    pricing = report["totals"]["pricing"]
    assert pricing["priced_tokens"] == 100 and pricing["unpriced_tokens"] == 1040
    assert pricing["known_api_cost_usd"] == "0.000930"
    assert pricing["estimated_api_cost_usd"] is None
    assert report["rate_age_seconds"] == 86400
    assert not (tmp_path / "no-state.json").exists()
    assert config.read_bytes() == b"!unreadable"
