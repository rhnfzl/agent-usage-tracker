"""Hermetic pricing tests: no network. Locks key normalization, cache-field extraction,
models.dev per-1M scaling, and the write/load roundtrip."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import pricing  # noqa: E402
from agent_usage_tracker.config import AutError  # noqa: E402


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
    print("ok sync_prices_writes_the_mode_a_plain_write_would")


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


def test_cli_sync_prices() -> None:
    from agent_usage_tracker import cli
    orig = pricing._fetch_json
    pricing._fetch_json = lambda url, timeout=30: {
        "claude-opus-4": {"output_cost_per_token": 7.5e-05}
    }
    try:
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "prices.json")
            rc = cli.main(["sync-prices", "--out", out])
            assert rc == 0
            assert pricing.load_prices(out)["claude-opus-4"]["output_cost"] == 7.5e-05
    finally:
        pricing._fetch_json = orig


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
