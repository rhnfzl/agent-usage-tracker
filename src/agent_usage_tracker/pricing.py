"""API-equivalent model price map, synced from LiteLLM (primary) -> models.dev (fallback).

Mirrors the usual price-registry sync pattern, EXTENDED to also extract
the two cache-cost fields that script skips (cache_read_input_token_cost /
cache_creation_input_token_cost), which the tracker needs for its cache-savings metric.
Zero runtime deps: the fetch uses stdlib urllib. Prices are NOTIONAL / API-equivalent.

Map shape: {normalized_key: {output_cost, input_cost?, cache_read_cost?, cache_write_cost?}}
in USD per token.
"""

from __future__ import annotations

from typing import Any

_MODELS_DEV_PRICE_SCALE = 1_000_000  # models.dev prices are per-1M tokens

# LiteLLM field -> our field
_FIELD_MAP = (
    ("input_cost_per_token", "input_cost"),
    ("cache_read_input_token_cost", "cache_read_cost"),
    ("cache_creation_input_token_cost", "cache_write_cost"),
)


def normalize_model_key(model_id: str) -> str:
    """Lowercase; strip a trailing version suffix (model@20240229), an alias prefix
    (alias@model), then a provider path (provider/model). Mirrors the shared normalizer so a
    tracker model string and a LiteLLM key reduce to the same token."""
    key = model_id.strip().lower()
    if "@" in key:
        head, _, tail = key.rpartition("@")
        if head and tail.isdigit():
            key = head
    if "@" in key:
        head, _, tail = key.rpartition("@")
        key = tail or head
    if "/" in key:
        key = key.rpartition("/")[2] or key
    return key


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):  # bool is an int; a stray `true` must not become 1.0
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_rate_map(
    litellm: dict[str, Any],
    models_dev: dict[str, Any] | None = None,
) -> dict[str, dict[str, float]]:
    """Prune + normalize both sources. An entry survives only with an output price (always
    present and the discriminating cost). LiteLLM is primary; models.dev fills gaps."""
    rates: dict[str, dict[str, float]] = {}
    for model_id, spec in litellm.items():
        if not isinstance(spec, dict):
            continue
        out = _coerce_float(spec.get("output_cost_per_token"))
        if out is None:
            continue
        entry: dict[str, float] = {"output_cost": out}
        for src, dst in _FIELD_MAP:
            val = _coerce_float(spec.get(src))
            if val is not None:
                entry[dst] = val
        rates[normalize_model_key(model_id)] = entry
    _merge_models_dev(rates, models_dev)
    return rates


def _merge_models_dev(
    rates: dict[str, dict[str, float]],
    models_dev: dict[str, Any] | None,
) -> None:
    """Fill keys LiteLLM lacked from models.dev (nested provider -> models -> cost;
    per-1M -> per-token). Output price required. Mutates `rates` in place."""
    if not isinstance(models_dev, dict):
        return
    for _provider, block in models_dev.items():
        models = block.get("models") if isinstance(block, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id, spec in models.items():
            key = normalize_model_key(model_id)
            if key in rates or not isinstance(spec, dict):
                continue
            cost = spec.get("cost")
            if not isinstance(cost, dict):
                continue
            out = _coerce_float(cost.get("output"))
            if out is None:
                continue
            entry: dict[str, float] = {"output_cost": out / _MODELS_DEV_PRICE_SCALE}
            inp = _coerce_float(cost.get("input"))
            if inp is not None:
                entry["input_cost"] = inp / _MODELS_DEV_PRICE_SCALE
            cache_read = _coerce_float(cost.get("cache_read"))
            if cache_read is not None:
                entry["cache_read_cost"] = cache_read / _MODELS_DEV_PRICE_SCALE
            rates[key] = entry
