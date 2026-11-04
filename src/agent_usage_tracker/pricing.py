"""API-equivalent model price map, synced from LiteLLM (primary) -> models.dev (fallback).

Follows the usual price-registry sync pattern, EXTENDED to also extract the two cache-cost
fields such syncs normally skip (cache_read_input_token_cost /
cache_creation_input_token_cost). Nothing in aut reads the file today (see the README).
Zero runtime deps: the fetch uses stdlib urllib. Prices are NOTIONAL / API-equivalent.

Map shape: {normalized_key: {output_cost, input_cost?, cache_read_cost?, cache_write_cost?}}
in USD per token.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .config import AutError
from .net import HTTP_ERRORS
from .state import atomic_write_text

_MODELS_DEV_PRICE_SCALE = 1_000_000  # models.dev prices are per-1M tokens

# LiteLLM field -> our field
_FIELD_MAP = (
    ("input_cost_per_token", "input_cost"),
    ("cache_read_input_token_cost", "cache_read_cost"),
    ("cache_creation_input_token_cost", "cache_write_cost"),
)


def normalize_model_key(model_id: str) -> str:
    """Lowercase; strip a trailing version suffix (model@20240229), an alias prefix
    (alias@model), then a provider path (provider/model), so a tracker model string and a
    LiteLLM key reduce to the same token."""
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


LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
MODELS_DEV_URL = "https://models.dev/api.json"


def _fetch_json(
    url: str, timeout: int = 30
) -> dict[str, Any] | None:
    """GET a JSON dict via stdlib urllib (no third-party HTTP client).
    None on any failure."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "agent-usage-tracker/price-sync",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (*HTTP_ERRORS, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sync_prices(
    out: str | Path,
    *,
    litellm_url: str = LITELLM_URL,
    models_dev_url: str = MODELS_DEV_URL,
    fetch: Callable[[str], dict[str, Any] | None] | None = None,
) -> int:
    """Fetch -> build -> atomic write. Returns the number of priced models. Raises
    AutError if the primary source is unreachable or the map is empty, so a
    good file is never overwritten with junk. `fetch` defaults to the network
    fetcher; inject a fake in tests."""
    fetch = fetch or _fetch_json
    litellm = fetch(litellm_url)
    if not litellm:
        raise AutError(
            "sync-prices: LiteLLM source unreachable; keeping the existing "
            "price file."
        )
    rates = build_rate_map(litellm, fetch(models_dev_url))
    if not rates:
        raise AutError(
            "sync-prices: built an empty rate map; keeping the existing "
            "price file."
        )
    # Atomic, so a concurrent reader never sees a half-written file. Shared: no secret in it, and
    # --out can point at a path another user or service reads.
    atomic_write_text(Path(out).expanduser(), json.dumps(rates, indent=2, sort_keys=True),
                      shared=True)
    return len(rates)


def load_prices(path: str | Path) -> dict[str, dict[str, float]]:
    """Load a price file; {} if absent."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise AutError(f"price file {p} is unreadable/corrupt; re-run `aut sync-prices`: {exc}")
