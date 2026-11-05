"""API-equivalent model price map, synced from LiteLLM (primary), then models.dev, then
pydantic/genai-prices, each filling only the models the ones before it lack.

Follows the usual price-registry sync pattern, EXTENDED to also extract the two cache-cost
fields such syncs normally skip (cache_read_input_token_cost /
cache_creation_input_token_cost). Nothing in aut reads the file; it is kept for readers outside
aut (see the README).
Zero runtime deps: the fetch uses stdlib urllib. Prices are NOTIONAL / API-equivalent.

Map shape: {normalized_key: {output_cost, input_cost?, cache_read_cost?, cache_write_cost?}}
in USD per token.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date
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
    genai_prices: Any = None,
) -> dict[str, dict[str, float]]:
    """Prune + normalize the sources. An entry survives only with an output price (always
    present and the discriminating cost). LiteLLM is primary; the others fill gaps."""
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
    _merge_genai_prices(rates, genai_prices)
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


# genai-prices field -> our field
_GENAI_FIELD_MAP = (
    ("output_mtok", "output_cost"),
    ("input_mtok", "input_cost"),
    ("cache_read_mtok", "cache_read_cost"),
    ("cache_write_mtok", "cache_write_cost"),
)


def _current_genai_prices(prices: Any, today: str) -> dict[str, Any] | None:
    """A model's price table today. A list holds the base entry, then entries from a start_date in
    date order. Entries for an hour of the day are skipped: this map holds one price per model."""
    if isinstance(prices, dict):
        return prices
    current = None
    for entry in prices if isinstance(prices, list) else ():
        if not isinstance(entry, dict):
            continue
        constraint = entry.get("constraint")
        if constraint is None or (isinstance(constraint, dict)
                                  and str(constraint.get("start_date", "9")) <= today):
            current = entry.get("prices")
    return current if isinstance(current, dict) else None


def _merge_genai_prices(
    rates: dict[str, dict[str, float]],
    genai_prices: Any,
    today: str | None = None,
) -> None:
    """Fill keys still missing from genai-prices (a list of providers, each with models; per-1M ->
    per-token). A tiered price {"base": x, "tiers": [...]} counts as its base: this map holds one
    price per token type, not per prompt size. Output price required. Mutates `rates` in place."""
    if not isinstance(genai_prices, list):
        return
    today = today or date.today().isoformat()
    for provider in genai_prices:
        models = provider.get("models") if isinstance(provider, dict) else None
        for spec in models if isinstance(models, list) else ():
            model_id = spec.get("id") if isinstance(spec, dict) else None
            if not isinstance(model_id, str):
                continue
            key = normalize_model_key(model_id)
            prices = _current_genai_prices(spec.get("prices"), today)
            if key in rates or prices is None:
                continue
            entry: dict[str, float] = {}
            for src, dst in _GENAI_FIELD_MAP:
                raw = prices.get(src)
                val = _coerce_float(raw.get("base") if isinstance(raw, dict) else raw)
                if val is not None:
                    entry[dst] = val / _MODELS_DEV_PRICE_SCALE
            if "output_cost" in entry:
                rates[key] = entry


LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
MODELS_DEV_URL = "https://models.dev/api.json"
GENAI_PRICES_URL = "https://raw.githubusercontent.com/pydantic/genai-prices/main/prices/new_data/v2/data.json"


def _fetch_json(
    url: str, timeout: int = 30
) -> Any:
    """GET a JSON object or list via stdlib urllib (no third-party HTTP client).
    None on any failure. Raises AutError for a URL that is not http(s): urllib would read file:."""
    if not url.lower().startswith(("http://", "https://")):
        raise AutError("sync-prices: a price source URL must be http(s)")
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "agent-usage-tracker/price-sync",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 # http(s) only
            data = json.loads(resp.read().decode("utf-8"))
    except (*HTTP_ERRORS, ValueError):
        return None
    return data if isinstance(data, (dict, list)) else None


def sync_prices(
    out: str | Path,
    *,
    litellm_url: str = LITELLM_URL,
    models_dev_url: str = MODELS_DEV_URL,
    genai_prices_url: str = GENAI_PRICES_URL,
    fetch: Callable[[str], Any] | None = None,
) -> int:
    """Fetch -> build -> atomic write. Returns the number of priced models. Raises
    AutError if the primary source is unreachable or the map is empty, so a
    good file is never overwritten with junk. `fetch` defaults to the network
    fetcher; inject a fake in tests."""
    fetch = fetch or _fetch_json
    litellm = fetch(litellm_url)
    if not litellm or not isinstance(litellm, dict):
        raise AutError(
            "sync-prices: LiteLLM source unreachable; keeping the existing "
            "price file."
        )
    rates = build_rate_map(litellm, fetch(models_dev_url), fetch(genai_prices_url))
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
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise AutError(f"price file {p} is unreadable/corrupt; re-run `aut sync-prices`: {exc}")
    if not isinstance(obj, dict) or not all(isinstance(v, dict) for v in obj.values()):
        raise AutError(f"price file {p} is not a table of models; re-run `aut sync-prices`")
    return obj
