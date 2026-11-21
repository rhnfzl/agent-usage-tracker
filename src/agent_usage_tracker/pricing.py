"""API-equivalent model price map, synced from LiteLLM (primary), then models.dev, then
pydantic/genai-prices, each filling only the models the ones before it lack.

Follows the usual price-registry sync pattern, EXTENDED to also extract the two cache-cost
fields cache_read_input_token_cost and cache_creation_input_token_cost. The local summary
reads the map offline, and an explicit refresh can also load the packaged sink.
Zero runtime deps: the fetch uses stdlib urllib. Prices are NOTIONAL / API-equivalent.

Map shape: {normalized_key: {output_cost, input_cost?, cache_read_cost?, cache_write_cost?}}
in USD per token.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .config import AutError, config_path
from .net import HTTP_ERRORS
from .sink import write_many
from .state import atomic_write_text

COMPONENT_RATES = (
    ("input_tokens", "input_cost"),
    ("output_tokens", "output_cost"),
    ("cache_read_tokens", "cache_read_cost"),
    ("cache_write_tokens", "cache_write_cost"),
)

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
        if head and tail.isascii() and tail.isdigit():
            key = head
    if "@" in key:
        head, _, tail = key.rpartition("@")
        key = tail or head
    if "/" in key:
        key = key.rpartition("/")[2] or key
    return key


def price_key(model: str | None, rates: dict[str, dict[str, float]]) -> str:
    """Use an exact historical rate key before trying the canonical catalog key."""
    exact = (model or "").lower()
    return exact if exact in rates else normalize_model_key(exact)


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):  # bool is an int; a stray `true` must not become 1.0
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


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
        if key := normalize_model_key(model_id):
            rates[key] = entry
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
            if not key or key in rates or not isinstance(spec, dict):
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
            cache_write = _coerce_float(cost.get("cache_write"))
            if cache_write is not None:
                entry["cache_write_cost"] = cache_write / _MODELS_DEV_PRICE_SCALE
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
            if not key or key in rates or prices is None:
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
    pg_dsn: str | None = None,
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
    rates = build_rate_map(litellm)
    if not rates:
        raise AutError(
            "sync-prices: built an empty rate map; keeping the existing "
            "price file."
        )
    _merge_models_dev(rates, fetch(models_dev_url))
    _merge_genai_prices(rates, fetch(genai_prices_url))
    text = json.dumps(rates, indent=2, sort_keys=True)
    if pg_dsn:
        load_sink_prices(pg_dsn, rates, datetime.now(timezone.utc))
    try:
        atomic_write_text(Path(out).expanduser(), text, shared=True)
    except OSError as exc:
        detail = " after the sink applied the rates" if pg_dsn else ""
        raise AutError(f"sync-prices: could not write the local price file{detail}: {exc}") from exc
    return len(rates)


def default_price_path() -> Path:
    return config_path().parent / "model_prices.json"


def read_price_snapshot(path: str | Path) -> tuple[dict[str, dict[str, float]], datetime | None]:
    """Read rates and mtime from the same file. Older plain price maps keep their format."""
    p = Path(path).expanduser()
    try:
        with p.open(encoding="utf-8") as handle:
            obj = json.load(handle)
            synced_at = datetime.fromtimestamp(os.fstat(handle.fileno()).st_mtime, timezone.utc)
    except FileNotFoundError:
        return {}, None
    except (ValueError, OSError) as exc:
        raise AutError(f"price file {p} is unreadable/corrupt; re-run `aut sync-prices`: {exc}")
    if not isinstance(obj, dict) or not all(isinstance(v, dict) for v in obj.values()):
        raise AutError(f"price file {p} is not a table of models; re-run `aut sync-prices`")
    rates = {}
    for key, values in obj.items():
        entry = {}
        for _, field in COMPONENT_RATES:
            if (rate := _coerce_float(values.get(field))) is not None:
                entry[field] = rate
        if key:
            # An exact override may leave one or every component unpriced.
            rates[key] = entry
    return rates, synced_at


def load_prices(path: str | Path) -> dict[str, dict[str, float]]:
    """Load a price file, returning an empty map if the file is absent."""
    return read_price_snapshot(path)[0]


def load_sink_prices(dsn: str, rates: dict[str, dict[str, float]], synced_at: datetime) -> None:
    """Upsert a validated snapshot in one transaction, keeping other existing price keys."""
    rows = [(key, *(Decimal(str(entry[field])) if field in entry else None
                    for _, field in COMPONENT_RATES), synced_at)
            for key, entry in sorted(rates.items())]
    write_many(dsn, """
        INSERT INTO agents.model_price
            (model_key, input_cost, output_cost, cache_read_cost, cache_write_cost, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (model_key) DO UPDATE SET
            input_cost = EXCLUDED.input_cost,
            output_cost = EXCLUDED.output_cost,
            cache_read_cost = EXCLUDED.cache_read_cost,
            cache_write_cost = EXCLUDED.cache_write_cost,
            synced_at = EXCLUDED.synced_at
        WHERE agents.model_price.synced_at < EXCLUDED.synced_at
    """, rows)
