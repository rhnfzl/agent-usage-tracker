"""Hermetic pricing tests: no network. Locks key normalization, cache-field extraction,
models.dev per-1M scaling, and the write/load roundtrip."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from agent_usage_tracker import pricing
from agent_usage_tracker.config import AutError


def test_normalize_model_key() -> None:
    assert pricing.normalize_model_key("claude-3-opus@20240229") == "claude-3-opus"
    assert pricing.normalize_model_key("vendor-ai@azure/gpt-4o") == "gpt-4o"
    assert pricing.normalize_model_key("anthropic/claude-3-5-sonnet") == "claude-3-5-sonnet"
    assert pricing.normalize_model_key("GPT-5") == "gpt-5"


def test_build_rate_map_extracts_cache_fields() -> None:
    litellm = {
        "claude-opus-4": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 1.5e-05, "output_cost_per_token": 7.5e-05,
            "cache_read_input_token_cost": 1.5e-06, "cache_creation_input_token_cost": 1.875e-05,
        },
        "no-output-price": {"input_cost_per_token": 1e-06},  # dropped: no output price
        "sample_spec": "not-a-dict",
    }
    rates = pricing.build_rate_map(litellm)
    assert "no-output-price" not in rates
    assert rates["claude-opus-4"] == {
        "output_cost": 7.5e-05, "input_cost": 1.5e-05,
        "cache_read_cost": 1.5e-06, "cache_write_cost": 1.875e-05,
    }


def test_models_dev_fallback_scales_per_million() -> None:
    litellm = {"a": {"output_cost_per_token": 1e-06}}
    models_dev = {"openai": {"models": {"b": {"cost": {"input": 2.0, "output": 8.0}}}}}
    rates = pricing.build_rate_map(litellm, models_dev)
    assert rates["b"] == {"output_cost": 8.0 / 1_000_000, "input_cost": 2.0 / 1_000_000}


def test_sync_and_load_roundtrip() -> None:
    sample = {"claude-opus-4": {"litellm_provider": "anthropic",
                                "input_cost_per_token": 1.5e-05,
                                "output_cost_per_token": 7.5e-05}}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "sub" / "model_prices.json"
        n = pricing.sync_prices(out, fetch=lambda url: sample)
        assert n == 1
        loaded = pricing.load_prices(out)
        assert loaded["claude-opus-4"]["output_cost"] == 7.5e-05
        assert pricing.load_prices(Path(d) / "gone.json") == {}


def test_sync_prices_writes_the_mode_a_plain_write_would() -> None:
    if os.name == "nt":          # no POSIX modes
        return
    sample = {"claude-opus-4": {"litellm_provider": "anthropic", "input_cost_per_token": 1.5e-05,
                                "output_cost_per_token": 7.5e-05}}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "prices.json"
        umask = os.umask(0o027)
        try:
            pricing.sync_prices(out, fetch=lambda url: sample)
        finally:
            os.umask(umask)
        assert stat.S_IMODE(out.stat().st_mode) == 0o640, "a new file follows the umask"
        out.chmod(0o644)
        pricing.sync_prices(out, fetch=lambda url: sample)
        assert stat.S_IMODE(out.stat().st_mode) == 0o644, "an existing file keeps its mode"


def test_fetch_refuses_a_url_that_is_not_http() -> None:
    raised = False
    try:
        pricing._fetch_json("file:///etc/passwd")
    except AutError:
        raised = True
    assert raised


def test_sync_prices_refuses_empty() -> None:
    raised = False
    try:
        pricing.sync_prices("/tmp/should-not-write.json", fetch=lambda url: None)
    except AutError:
        raised = True
    assert raised


def test_sync_prices_refuses_empty_rate_map() -> None:
    bad = {"m": {"input_cost_per_token": 1e-06}}  # no output price -> pruned -> empty map
    raised = False
    try:
        with tempfile.TemporaryDirectory() as d:
            pricing.sync_prices(Path(d) / "x.json", fetch=lambda url: bad)
    except AutError:
        raised = True
    assert raised


@pytest.mark.parametrize("body", ["[1]", '{"m": 1}'])
def test_load_prices_refuses_a_file_of_another_shape(tmp_path, body) -> None:
    bad = tmp_path / "prices.json"
    bad.write_text(body, encoding="utf-8")
    with pytest.raises(AutError, match="not a table of models"):
        pricing.load_prices(bad)


def test_load_prices_raises_on_corrupt_file() -> None:
    raised = False
    try:
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "prices.json"
            bad.write_text("{not json", encoding="utf-8")
            pricing.load_prices(bad)
    except AutError:
        raised = True
    assert raised


def test_cli_sync_prices(monkeypatch) -> None:
    from agent_usage_tracker import cli
    monkeypatch.setattr(pricing, "_fetch_json", lambda url, timeout=30: {
        "claude-opus-4": {"output_cost_per_token": 7.5e-05}
    })
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "prices.json")
        rc = cli.main(["sync-prices", "--out", out])
        assert rc == 0
        assert pricing.load_prices(out)["claude-opus-4"]["output_cost"] == 7.5e-05


def test_genai_prices_fills_only_gaps_at_todays_base_price() -> None:
    data = [{"id": "p", "models": [
        {"id": "a", "prices": {"output_mtok": 99}},                           # LiteLLM has it
        {"id": "flat", "prices": {"input_mtok": 2, "output_mtok": 8, "cache_read_mtok": 1,
                                    "cache_write_mtok": 3, "web_searches_kcount": 10}},
        {"id": "tiered", "prices": {"input_mtok": {"base": 5, "tiers": [{"start": 9, "price": 10}]},
                                      "output_mtok": {"base": 25, "tiers": []}}},
        {"id": "dated", "prices": [
            {"prices": {"output_mtok": 1}},
            {"constraint": {"start_date": "2026-03-01"}, "prices": {"output_mtok": 2}},
            {"constraint": {"start_date": "2999-01-01"}, "prices": {"output_mtok": 3}},
        ]},
        {"id": "hourly", "prices": [
            {"prices": {"output_mtok": 4}},
            {"constraint": {"start_time": "16:30:00Z", "end_time": "00:30:00Z"},
             "prices": {"output_mtok": 1}},
        ]},
        {"id": "no-output", "prices": {"input_mtok": 1}},
        {"prices": {"output_mtok": 1}}, "not-a-model",
    ]}, "not-a-provider"]
    rates = pricing.build_rate_map({"a": {"output_cost_per_token": 1e-06}}, None, data)
    m = 1_000_000
    assert rates["a"] == {"output_cost": 1e-06}
    assert rates["flat"] == {"output_cost": 8 / m, "input_cost": 2 / m, "cache_read_cost": 1 / m,
                             "cache_write_cost": 3 / m}
    assert rates["tiered"] == {"output_cost": 25 / m, "input_cost": 5 / m}
    assert rates["dated"] == {"output_cost": 2 / m}
    assert rates["hourly"] == {"output_cost": 4 / m}
    assert "no-output" not in rates


def test_sync_survives_a_missing_genai_prices_list() -> None:
    sample = {"m": {"output_cost_per_token": 1e-06}}
    with tempfile.TemporaryDirectory() as d:
        n = pricing.sync_prices(Path(d) / "p.json",
                                fetch=lambda url: sample if url == pricing.LITELLM_URL else None)
    assert n == 1
