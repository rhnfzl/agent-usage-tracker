import argparse
from datetime import date
import json
from pathlib import Path

import pytest

from agent_usage_tracker import overview_cli
from agent_usage_tracker.demo import fixture
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.overview import render
from agent_usage_tracker.summary import build_summary


def test_overview_reads_real_summary_and_private_evidence(tmp_path, monkeypatch, capsys):
    home = tmp_path / "usage"
    config = fixture(home)
    store_path = tmp_path / "private"
    Store(store_path)
    monkeypatch.setenv("AUT_IMPROVE_ROOT", str(store_path))
    monkeypatch.setenv("AUT_STATE", str(tmp_path / "state" / "state.json"))
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config" / "config.toml"))
    parser = argparse.ArgumentParser()
    overview_cli.register(parser.add_subparsers(required=True))
    args = parser.parse_args(["overview", "--since", "2026-06-15", "--until", "2026-06-16",
                              "--no-browser"])

    def snapshot():
        return {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    before = snapshot()

    def local_page(token, on_get, on_post, notice, *, open_browser, on_ready):
        assert token and not open_browser
        on_ready(f"http://127.0.0.1:12345/{token}")
        page = on_get("").decode()
        assert "1,140" in page and "2026-06-15" in page and "UTC" in page
        assert "Source health" in page and "Pricing" in page and "unknown" in page.lower()
        assert f'href="/{token}/improvement"' in page
        assert "Private improvement report card" in on_get("/improvement").decode()
        switched = on_get("?period=custom&since=2026-06-16&until=2026-06-17&group=model").decode()
        assert "codex-auto-review" in switched and "1,040" in switched and "1,140" not in switched
        assert "Choose two dates" in on_get("?period=custom&since=2026-06-16").decode()
        assert on_get("/extra") is None
        assert on_post({"action": ["unexpected"]}) is None
        assert str(tmp_path) not in page

    monkeypatch.setattr(overview_cli.local_page, "serve", local_page)
    overview_cli.run(config, args)
    assert "http://127.0.0.1:" in capsys.readouterr().out
    assert snapshot() == before


def test_overview_escapes_local_project_names(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    cfg = fixture(tmp_path)
    transcript = next(Path(tmp_path).glob(".claude/projects/*/*.jsonl"))
    transcript.write_text(transcript.read_text().replace("sample-project", "<script>alert(1)<&"))
    report = build_summary(cfg, since=date(2026, 6, 15), until=date(2026, 6, 16),
                           group_by="project")
    page = render(report, {"collectors": [], "issues": []}, "/tok", "custom", False)
    assert b"<script>alert(1)" not in page
    assert b"&lt;script&gt;alert(1)" in page


def test_overview_distinguishes_priced_and_unpriced_usage(tmp_path):
    config = fixture(tmp_path / "home")
    rates = tmp_path / "sample-prices.json"
    rates.write_text(json.dumps({"claude-opus-4-8": {
        "input_cost": 0.00001, "output_cost": 0.00002, "cache_read_cost": 0.000001,
        "cache_write_cost": 0.00001,
    }}), encoding="utf-8")
    report = build_summary(config, since=date(2026, 6, 15), until=date(2026, 6, 16),
                           prices_path=rates)
    page = render(report, {"collectors": [], "issues": []}, "/tok", "custom", False)
    assert b"Rate file available" in page and b"age" in page
    assert b"Unpriced tokens" in page and b"Logged cost (USD)" in page
    assert b"<td>All models</td><td>100</td><td>1,040</td><td>1,140</td>" in page
    assert b"<td>0.000930</td><td>unknown</td>" in page


def test_overview_rejects_bad_cli_dates_before_serving(tmp_path, monkeypatch):
    monkeypatch.setenv("AUT_CONFIG", str(tmp_path / "config.toml"))
    parser = argparse.ArgumentParser()
    overview_cli.register(parser.add_subparsers(required=True))
    args = parser.parse_args(["overview", "--until", "2026-06-16"])
    monkeypatch.setattr(overview_cli.local_page, "serve", lambda *a, **kw: pytest.fail("server"))
    with pytest.raises(overview_cli.AutError, match="--since and --until"):
        overview_cli.run(fixture(tmp_path), args)
