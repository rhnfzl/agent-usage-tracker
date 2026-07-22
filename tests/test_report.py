"""Report glue: run_report writes metrics.json from an injected row fetcher (no DB)."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import report  # noqa: E402
from agent_usage_tracker.metrics import Row  # noqa: E402


def test_run_report_writes_json_matching_return() -> None:
    rows = [Row(datetime(2026, 1, 5, 10, tzinfo=timezone.utc), "codex", "laptop", "gpt-5",
                "agent", 100, 50, 0, 0, 10, 160)]
    with tempfile.TemporaryDirectory() as d:
        prices_path = Path(d) / "p.json"
        prices_path.write_text(json.dumps({"gpt-5": {"input_cost": 1e-6, "output_cost": 1e-5}}))
        out = Path(d) / "metrics.json"
        m = report.run_report("postgresql://ignored", prices_path, out,
                              idle_gap_s=1800, now=datetime(2026, 2, 1, tzinfo=timezone.utc),
                              fetch=lambda dsn: rows)
        assert out.exists()
        assert json.loads(out.read_text()) == m
        assert m["volume"]["total_tokens"] == 160
        assert m["volume"]["priced"] is True


def test_run_report_requires_dsn() -> None:
    raised = False
    try:
        report.run_report(None, "x.json", "y.json", fetch=lambda dsn: [])
    except SystemExit:
        raised = True
    assert raised


def test_cli_report_writes_metrics() -> None:
    import os
    from agent_usage_tracker import cli

    rows = [Row(datetime(2026, 1, 1, tzinfo=timezone.utc), "codex", "l", "gpt-5",
                "agent", 10, 5, 0, 0, 0, 15)]
    orig_fetch = report.fetch_rows
    report.fetch_rows = lambda dsn: rows
    orig_cfg_env = os.environ.get("AUT_CONFIG")
    try:
        with tempfile.TemporaryDirectory() as d:
            cfg_toml = Path(d) / "config.toml"
            cfg_toml.write_text('[transport]\npg_dsn = "postgresql://ignored"\n')
            os.environ["AUT_CONFIG"] = str(cfg_toml)
            prices = Path(d) / "p.json"
            prices.write_text('{"gpt-5": {"input_cost": 1e-6, "output_cost": 1e-5}}')
            out = Path(d) / "metrics.json"
            rc = cli.main(["report", "--out", str(out), "--prices", str(prices)])
            assert rc == 0
            assert json.loads(out.read_text())["volume"]["total_tokens"] == 15
    finally:
        report.fetch_rows = orig_fetch
        if orig_cfg_env is None:
            os.environ.pop("AUT_CONFIG", None)
        else:
            os.environ["AUT_CONFIG"] = orig_cfg_env


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all passed")
