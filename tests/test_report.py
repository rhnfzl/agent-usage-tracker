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


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all passed")
