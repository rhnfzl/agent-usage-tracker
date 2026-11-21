"""Synthetic normalized records shared by local pricing and the sink integration check."""

from datetime import date, datetime, timezone

from agent_usage_tracker.collectors import Collector, REGISTRY
from agent_usage_tracker.config import Config
from agent_usage_tracker.event import UsageEvent

PRICE_DAY = date(2026, 6, 15)
PRICE_TIMESTAMP = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
PRICE_MODEL = " Fixture@Vendor/MODEL-A@20240229 "

CATALOG = {
    "vendor/model-a": {
        "input_cost_per_token": 0.01, "output_cost_per_token": 0.02,
        "cache_read_input_token_cost": 0.003, "cache_creation_input_token_cost": 0.004,
    },
    "model-b": {"input_cost_per_token": 0.02, "output_cost_per_token": 0.03},
    "model-c": {
        "input_cost_per_token": 0.04, "output_cost_per_token": 0.05,
        "cache_read_input_token_cost": 0.006, "cache_creation_input_token_cost": 0.007,
    },
}


def price_rows():
    ts = PRICE_TIMESTAMP.isoformat()
    return [
        UsageEvent("price-a", ts, "claude_code", PRICE_MODEL, None, input_tokens=10,
                   output_tokens=20, cache_read_tokens=30, cache_write_tokens=5, total_tokens=65,
                   reasoning_tokens=7, est_cost_usd=0.125, machine="pricing-fixture"),
        UsageEvent("price-b", ts, "claude_code", "model-b", None, input_tokens=5,
                   output_tokens=10, cache_read_tokens=7, total_tokens=22,
                   machine="pricing-fixture"),
        UsageEvent("price-unknown", ts, "claude_code", "codex-auto-review", None, input_tokens=8,
                   total_tokens=10, machine="pricing-fixture"),
    ]


def price_config(tmp_path, monkeypatch, rows):
    def collect(cfg, wm):
        yield rows, lambda: None

    monkeypatch.setitem(REGISTRY, "claude_code", Collector(collect))
    return Config("pricing-fixture", ("claude_code",), None, None, None, tmp_path,
                  ledger_timezone="UTC")


LEGACY_RATES = {"vendor/model-q": {"output_cost": 0.5},
                "model-q": {"input_cost": 0.01, "output_cost": 0.02}}


def legacy_price_rows():
    ts = PRICE_TIMESTAMP.isoformat()
    return [
        UsageEvent("legacy-override", ts, "claude_code", "Vendor/Model-Q", None,
                   input_tokens=1, output_tokens=4, total_tokens=5, machine="legacy-pricing"),
        UsageEvent("legacy-negative", ts, "claude_code", "model-q", None,
                   input_tokens=-5, output_tokens=10, total_tokens=5, machine="legacy-pricing"),
        UsageEvent("legacy-no-valid-counts", ts, "claude_code", "model-q", None,
                   input_tokens=-5, total_tokens=-5, machine="legacy-pricing"),
    ]
