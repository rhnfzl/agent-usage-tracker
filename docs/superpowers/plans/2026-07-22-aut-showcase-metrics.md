# AUT Showcase-Metrics Implementation Plan

> **Superseded.** `aut report`, `report.py` and `metrics.py` were built and then retired; `metrics.json` is replaced by the sink's database views. Kept as history, it does not describe the code.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-side capability that turns the tracker's per-turn sink data into a small, honest `metrics.json` for a personal-site showcase, priced by a synced API-equivalent rate map.

**Architecture:** Three new pure/IO-split modules — `pricing.py` (fetch + build + load a LiteLLM-sourced price map, extended for cache-cost fields), `metrics.py` (pure derivation: sessionization, efficiency ratios, fluency trends, human/agent collaboration, volume, priced-coverage), and `report.py` (read the sink, run the math, write JSON) — plus two CLI subcommands (`sync-prices`, `report`). The collector is untouched: sessions are derived at report time.

**Tech Stack:** Python 3.11+, stdlib only for the base path (`urllib.request`, `json`, `tomllib`), `psycopg` (lazy import, `[postgres]` extra) for the sink read. Tests are hermetic and framework-free.

## Global Constraints

- **Python floor:** `requires-python = ">=3.11"`. Use `from __future__ import annotations` at the top of every new module.
- **Zero new runtime dependencies.** Base install stays dependency-free: fetch prices with stdlib `urllib.request` (NOT `httpx`). The only DB access is `import psycopg` done lazily *inside* the function that needs it, so a base install without the `[postgres]` extra still imports and runs everything else.
- **Ruff:** `line-length = 100`, `target-version = "py311"`. Keep lines ≤ 100.
- **Privacy by construction:** the sink `SELECT` MUST NOT include the `project` column. No metric, trend, or output field may carry a project name.
- **Every `$` figure is NOTIONAL / API-equivalent** (subscriptions are flat-rate) — label it so in docstrings and docs.
- **Tests:** hermetic, no network, no DB, no pytest fixtures. Plain `def test_*() -> None:` with `assert`, a `sys.path.insert(0, .../src)` header, and a `__main__` runner that runs each `test_*` and prints `ok <name>`. Runnable via `python tests/<file>.py` and `pytest`.
- **Sink schema** (from `push.py`): table `agents.usage_event`; columns include `ts, agent, machine, model, kind, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, total_tokens` (and `project`, `est_cost_usd`, `event_id`, `provider` — not read here). `kind` is `'agent'` or `'human'`.

---

### Task 1: Pricing — pure rate-map builder

**Files:**
- Create: `src/agent_usage_tracker/pricing.py`
- Test: `tests/test_pricing.py`

**Interfaces:**
- Produces: `normalize_model_key(model_id: str) -> str`; `build_rate_map(litellm: dict, models_dev: dict | None = None) -> dict[str, dict[str, float]]` where each value is `{"output_cost": float, "input_cost"?: float, "cache_read_cost"?: float, "cache_write_cost"?: float}` in USD per token.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pricing.py
"""Hermetic pricing tests: no network. Locks key normalization, cache-field extraction,
models.dev per-1M scaling, and the write/load roundtrip."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker import pricing  # noqa: E402


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


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all passed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo-root> && python -m pytest tests/test_pricing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_usage_tracker.pricing'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent_usage_tracker/pricing.py
"""API-equivalent model price map, synced from LiteLLM (primary) -> models.dev (fallback).

Follows the usual price-registry sync pattern, EXTENDED to also extract the
two cache-cost fields such syncs normally skip (cache_read_input_token_cost /
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
    if isinstance(value, bool):  # bool is an int; a stray `true` must not become 1.0 $/token
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_rate_map(litellm: dict[str, Any],
                   models_dev: dict[str, Any] | None = None) -> dict[str, dict[str, float]]:
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


def _merge_models_dev(rates: dict[str, dict[str, float]],
                      models_dev: dict[str, Any] | None) -> None:
    """Fill keys LiteLLM lacked from models.dev (nested provider -> models -> cost; per-1M ->
    per-token). Output price required. Mutates `rates` in place."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pricing.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agent_usage_tracker/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): pure LiteLLM/models.dev rate-map builder with cache-cost fields"
```

---

### Task 2: Pricing — fetch, atomic write, load

**Files:**
- Modify: `src/agent_usage_tracker/pricing.py`
- Test: `tests/test_pricing.py`

**Interfaces:**
- Consumes: `build_rate_map` (Task 1).
- Produces: `LITELLM_URL`, `MODELS_DEV_URL` (str constants); `sync_prices(out, *, litellm_url=LITELLM_URL, models_dev_url=MODELS_DEV_URL, fetch=None) -> int` (writes the file atomically, returns priced-model count, raises `SystemExit` on an unreachable primary source or empty map); `load_prices(path) -> dict[str, dict[str, float]]` (returns `{}` if the file is absent). `fetch` resolves to the internal `_fetch_json` when `None`, so it is monkeypatchable.

- [ ] **Step 1: Write the failing test** (append to `tests/test_pricing.py`, above the `__main__` block)

```python
def test_sync_and_load_roundtrip() -> None:
    sample = {"claude-opus-4": {"litellm_provider": "anthropic",
                                "input_cost_per_token": 1.5e-05,
                                "output_cost_per_token": 7.5e-05}}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "sub" / "model_prices.json"
        n = pricing.sync_prices(out, fetch=lambda url: sample)  # same sample for both URLs
        assert n == 1
        loaded = pricing.load_prices(out)
        assert loaded["claude-opus-4"]["output_cost"] == 7.5e-05
        assert pricing.load_prices(Path(d) / "gone.json") == {}


def test_sync_prices_refuses_empty() -> None:
    raised = False
    try:
        pricing.sync_prices("/tmp/should-not-write.json", fetch=lambda url: None)
    except SystemExit:
        raised = True
    assert raised
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pricing.py -q`
Expected: FAIL — `AttributeError: module 'agent_usage_tracker.pricing' has no attribute 'sync_prices'`.

- [ ] **Step 3: Write minimal implementation** (add imports at the top of `pricing.py` and append the functions)

Add to the import block at the top of `pricing.py`:

```python
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Callable
```

Append to `pricing.py`:

```python
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
MODELS_DEV_URL = "https://models.dev/api.json"


def _fetch_json(url: str, timeout: int = 30) -> dict[str, Any] | None:
    """GET a JSON dict via stdlib urllib (no third-party HTTP client). None on any failure."""
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "agent-usage-tracker/price-sync"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # fixed https sources
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sync_prices(out: str | Path, *, litellm_url: str = LITELLM_URL,
                models_dev_url: str = MODELS_DEV_URL,
                fetch: Callable[[str], dict[str, Any] | None] | None = None) -> int:
    """Fetch -> build -> atomic write. Returns the number of priced models. Raises SystemExit if
    the primary source is unreachable or the map is empty, so a good file is never overwritten
    with junk. `fetch` defaults to the network fetcher; inject a fake in tests."""
    fetch = fetch or _fetch_json
    litellm = fetch(litellm_url)
    if not litellm:
        raise SystemExit("sync-prices: LiteLLM source unreachable; keeping the existing price file.")
    rates = build_rate_map(litellm, fetch(models_dev_url))
    if not rates:
        raise SystemExit("sync-prices: built an empty rate map; keeping the existing price file.")
    path = Path(out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic publish: write a sibling temp then os.replace (same-dir rename is atomic), so a
    # concurrent reader never sees a half-written file. Mirrors the the registry sync script.
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(rates, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return len(rates)


def load_prices(path: str | Path) -> dict[str, dict[str, float]]:
    """Load a price file; {} if absent (the report then degrades to token-only metrics)."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pricing.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agent_usage_tracker/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): urllib fetch + atomic write + load for the price map"
```

---

### Task 3: CLI — `aut sync-prices`

**Files:**
- Modify: `src/agent_usage_tracker/cli.py`
- Test: `tests/test_pricing.py`

**Interfaces:**
- Consumes: `pricing.sync_prices`, `pricing.LITELLM_URL`, `pricing.MODELS_DEV_URL` (Task 2); `config._config_path` (existing).
- Produces: `aut sync-prices [--out PATH] [--litellm-url URL] [--models-dev-url URL]`. Default `--out` = `<config-dir>/model_prices.json`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_pricing.py`, above `__main__`)

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pricing.py::test_cli_sync_prices -q`
Expected: FAIL — argparse rejects `sync-prices` (`invalid choice`), raising `SystemExit`.

- [ ] **Step 3: Write minimal implementation**

In `src/agent_usage_tracker/cli.py`, add to the imports near the top (with the other `from .` imports):

```python
from . import pricing
from .config import Config, load_config, _config_path
```

(The existing line is `from .config import Config, load_config` — replace it with the line above.)

In `main()`, register the subparser next to the others (after the `export` subparser is added):

```python
    p_sync = sub.add_parser("sync-prices", help="refresh the API-equivalent model price map")
    p_sync.add_argument("--out", help="output JSON (default: <config-dir>/model_prices.json)")
    p_sync.add_argument("--litellm-url", default=pricing.LITELLM_URL)
    p_sync.add_argument("--models-dev-url", default=pricing.MODELS_DEV_URL)
```

In `main()`, handle it BEFORE `cfg = load_config()` (it needs no config), right after `args = parser.parse_args(argv)`:

```python
    if args.cmd == "sync-prices":
        out = args.out or str(_config_path().parent / "model_prices.json")
        n = pricing.sync_prices(out, litellm_url=args.litellm_url,
                                models_dev_url=args.models_dev_url)
        print(f"synced {n} model prices -> {out}", file=sys.stderr)
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pricing.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agent_usage_tracker/cli.py tests/test_pricing.py
git commit -m "feat(cli): aut sync-prices subcommand"
```

---

### Task 4: Metrics — the `Row` type and sessionization

**Files:**
- Create: `src/agent_usage_tracker/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `pricing.normalize_model_key` (Task 1).
- Produces: `Row` (a `NamedTuple` with fields `ts, agent, machine, model, kind, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, total_tokens`); `iter_sessions(rows, idle_gap_s: int)` yielding `(agent, machine, first_ts)` per session (a run of one agent+machine's events with < `idle_gap_s` between consecutive events).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_metrics.py
"""Hermetic metrics tests: fixed rows -> asserted derived metrics. No DB, no network."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_usage_tracker.metrics import Row, iter_sessions  # noqa: E402


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _row(ts: str, agent: str = "claude_code", model: str | None = "claude-opus-4",
         kind: str = "agent", inp: int = 0, out: int = 0, cr: int = 0, cw: int = 0,
         reason: int = 0, total: int | None = None) -> Row:
    total = (inp + out + cr + cw + reason) if total is None else total
    return Row(_dt(ts), agent, "laptop", model, kind, inp, out, cr, cw, reason, total)


def test_sessions_split_on_idle_gap() -> None:
    rows = [_row("2026-01-01T10:00:00"), _row("2026-01-01T10:20:00"),  # <30m -> same session
            _row("2026-01-01T12:00:00")]                                # >30m -> new session
    assert len(list(iter_sessions(rows, idle_gap_s=1800))) == 2


def test_sessions_do_not_merge_across_agents() -> None:
    rows = [_row("2026-01-01T10:00:00", agent="claude_code"),
            _row("2026-01-01T10:05:00", agent="codex")]
    assert len(list(iter_sessions(rows, idle_gap_s=1800))) == 2


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all passed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_usage_tracker.metrics'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent_usage_tracker/metrics.py
"""Pure derivation of showcase metrics from sink rows + a price map. No I/O here: the caller
supplies rows (report.fetch_rows) and prices (pricing.load_prices), so this module is
deterministic and unit-tested without a database or network.

`input` throughout means FRESH (non-cached) prompt tokens. Every $ figure is NOTIONAL /
API-equivalent (subscriptions are flat-rate).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, NamedTuple

from .pricing import normalize_model_key


class Row(NamedTuple):
    ts: datetime
    agent: str
    machine: str | None
    model: str | None
    kind: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int | None
    total_tokens: int


def iter_sessions(rows: Iterable[Row], idle_gap_s: int):
    """Yield (agent, machine, first_ts) per session. A session is a run of one agent+machine's
    events with <= idle_gap_s between consecutive events (ordered by ts)."""
    buckets: dict[tuple, list[datetime]] = defaultdict(list)
    for r in rows:
        buckets[(r.agent, r.machine)].append(r.ts)
    for (agent, machine), times in buckets.items():
        times.sort()
        session_start: datetime | None = None
        prev: datetime | None = None
        for t in times:
            if prev is None or (t - prev).total_seconds() > idle_gap_s:
                if session_start is not None:
                    yield (agent, machine, session_start)
                session_start = t
            prev = t
        if session_start is not None:
            yield (agent, machine, session_start)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agent_usage_tracker/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): Row type and gap-based sessionization"
```

---

### Task 5: Metrics — per-row cost functions

**Files:**
- Modify: `src/agent_usage_tracker/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `Row` (Task 4); `normalize_model_key` (Task 1).
- Produces: `cost_of(row, prices) -> float | None` (API-equivalent cost of one row; `None` if the model is absent from `prices`); `naive_cost_of(row, prices) -> float | None` (same but cache-read tokens repriced at the full input rate — its ratio to `cost_of` is the cache-savings multiple). Reasoning tokens are billed at the output rate.

- [ ] **Step 1: Write the failing test** (append to `tests/test_metrics.py`, above `__main__`; also update the import line)

Change the import line at the top of `tests/test_metrics.py` to:

```python
from agent_usage_tracker.metrics import Row, iter_sessions, cost_of, naive_cost_of  # noqa: E402
```

Append:

```python
PRICES = {"claude-opus-4": {"input_cost": 1.5e-05, "output_cost": 7.5e-05,
                            "cache_read_cost": 1.5e-06, "cache_write_cost": 1.875e-05}}


def test_cost_and_naive() -> None:
    r = _row("2026-01-01T10:00:00", inp=100, out=10, cr=1000, cw=50)
    # actual: 100*1.5e-5 + 10*7.5e-5 + 1000*1.5e-6 + 50*1.875e-5
    assert round(cost_of(r, PRICES), 10) == round(0.0015 + 0.00075 + 0.0015 + 0.0009375, 10)
    # naive reprices cache_read at the input rate: 1000*1.5e-5 (not 1000*1.5e-6)
    assert round(naive_cost_of(r, PRICES), 10) == round(0.0015 + 0.00075 + 0.015 + 0.0009375, 10)


def test_cost_none_for_unpriced_model() -> None:
    assert cost_of(_row("2026-01-01T10:00:00", model="unknown-x"), PRICES) is None
    assert naive_cost_of(_row("2026-01-01T10:00:00", model=None), PRICES) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: FAIL — `ImportError: cannot import name 'cost_of'`.

- [ ] **Step 3: Write minimal implementation** (append to `metrics.py`)

```python
def cost_of(row: Row, prices: dict[str, dict[str, float]]) -> float | None:
    """API-equivalent cost of one row, or None if the model is not priced. cache_read/write use
    their own rate when present, else fall back to the input rate. Reasoning bills as output."""
    p = prices.get(normalize_model_key(row.model or ""))
    if not p:
        return None
    in_rate = p.get("input_cost", 0.0)
    out_rate = p.get("output_cost", 0.0)
    return (row.input_tokens * in_rate
            + row.output_tokens * out_rate
            + row.cache_read_tokens * p.get("cache_read_cost", in_rate)
            + row.cache_write_tokens * p.get("cache_write_cost", in_rate)
            + (row.reasoning_tokens or 0) * out_rate)


def naive_cost_of(row: Row, prices: dict[str, dict[str, float]]) -> float | None:
    """What the row WOULD cost with no cache discount: cache_read repriced at the full input
    rate. sum(naive) / sum(actual) over priced rows is the cache-savings multiple."""
    p = prices.get(normalize_model_key(row.model or ""))
    if not p:
        return None
    in_rate = p.get("input_cost", 0.0)
    out_rate = p.get("output_cost", 0.0)
    return (row.input_tokens * in_rate
            + row.output_tokens * out_rate
            + row.cache_read_tokens * in_rate
            + row.cache_write_tokens * p.get("cache_write_cost", in_rate)
            + (row.reasoning_tokens or 0) * out_rate)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agent_usage_tracker/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): API-equivalent cost + naive (no-cache) cost per row"
```

---

### Task 6: Metrics — `compute_metrics` assembling all families

**Files:**
- Modify: `src/agent_usage_tracker/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `iter_sessions`, `cost_of`, `naive_cost_of`, `Row` (Tasks 4–5).
- Produces: `compute_metrics(rows, prices, *, idle_gap_s: int, now: datetime) -> dict` with top-level keys `as_of, window, volume, efficiency, fluency, collaboration, trends, priced_coverage, instrument`. Full shape is defined by the implementation below.

- [ ] **Step 1: Write the failing test** (update the import line and append; above `__main__`)

Update the import line at the top of `tests/test_metrics.py` to add `compute_metrics`:

```python
from agent_usage_tracker.metrics import (  # noqa: E402
    Row, iter_sessions, cost_of, naive_cost_of, compute_metrics,
)
```

Append:

```python
def test_compute_metrics_end_to_end() -> None:
    rows = [
        _row("2026-01-05T10:00:00", inp=100, out=50, cr=900, cw=0, kind="agent"),
        _row("2026-01-05T10:05:00", agent="claude_code", kind="human", model=None),
        _row("2026-02-10T09:00:00", agent="codex", model="unknown-x",
             inp=200, out=100, reason=30, total=330),
    ]
    m = compute_metrics(rows, PRICES, idle_gap_s=1800, now=_dt("2026-03-01T00:00:00"))

    assert m["as_of"] == "2026-03-01"
    assert m["collaboration"]["human_turns"] == 1
    assert m["collaboration"]["agent_turns"] == 2
    assert m["collaboration"]["agent_turns_per_human_turn"] == 2.0
    # cache_hit_rate over agent rows = cr / (cr + input) = 900 / (900 + 300)
    assert m["efficiency"]["cache_hit_rate"] == round(900 / 1200, 4)
    assert m["fluency"]["active_days"] == 2              # 2026-01-05 and 2026-02-10
    assert m["fluency"]["distinct_agents"] == 2
    assert m["priced_coverage"]["models_priced"] == 1    # only the claude row is priced
    assert "unknown-x" in m["priced_coverage"]["models_unpriced"]
    assert m["volume"]["priced"] is True
    assert len(m["trends"]["by_month"]) == 2             # 2026-01 and 2026-02
    assert m["instrument"]["tracks_human_and_agent_turns"] is True


def test_compute_metrics_unpriced_only_degrades_cleanly() -> None:
    rows = [_row("2026-01-05T10:00:00", model="unknown-x", inp=10, out=5, total=15)]
    m = compute_metrics(rows, PRICES, idle_gap_s=1800, now=_dt("2026-02-01T00:00:00"))
    assert m["volume"]["priced"] is False
    assert m["volume"]["est_cost_usd"] is None
    assert m["efficiency"]["cache_savings_multiple"] is None
    assert m["efficiency"]["output_leverage"] == round(5 / 10, 4)  # token ratios still work
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: FAIL — `ImportError: cannot import name 'compute_metrics'`.

- [ ] **Step 3: Write minimal implementation** (append to `metrics.py`; the `_INSTRUMENT` constant can sit just under the imports)

```python
_INSTRUMENT = {
    "agents_normalized": ["claude_code", "codex", "cursor", "cursor_sdk", "cursor_api", "pi"],
    "token_classes": ["input", "output", "cache_read", "cache_write", "reasoning"],
    "passive": True,
    "subscription_safe": True,
    "deterministic_idempotency": True,
    "tracks_human_and_agent_turns": True,
    "note": ("Summing only input_tokens undercounts real usage ~460x; the cache token classes "
             "dominate and are captured explicitly."),
}


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def compute_metrics(rows, prices, *, idle_gap_s: int, now: datetime) -> dict:
    """Derive the full showcase metric set. `rows` is any iterable of Row; `prices` may be {}
    (then $ metrics degrade to None while token-ratio metrics still compute)."""
    rows = list(rows)
    agent_rows = [r for r in rows if r.kind != "human"]
    human_rows = [r for r in rows if r.kind == "human"]

    tot_in = sum(r.input_tokens for r in agent_rows)
    tot_out = sum(r.output_tokens for r in agent_rows)
    tot_cr = sum(r.cache_read_tokens for r in agent_rows)
    tot_reason = sum(r.reasoning_tokens or 0 for r in agent_rows)
    tot_tokens = sum(r.total_tokens for r in agent_rows)

    # Cost + savings over the priced subset only; a model absent from `prices` is counted as
    # unpriced (never guessed) and surfaced in priced_coverage.
    actual = naive = 0.0
    priced_tokens = unpriced_tokens = 0
    priced_models: set[str] = set()
    unpriced_models: set[str] = set()
    for r in agent_rows:
        c = cost_of(r, prices)
        if c is None:
            unpriced_tokens += r.total_tokens
            if r.model:
                unpriced_models.add(r.model)
            continue
        actual += c
        naive += naive_cost_of(r, prices) or 0.0
        priced_tokens += r.total_tokens
        if r.model:
            priced_models.add(r.model)

    active_days = {r.ts.date() for r in rows}
    sessions = list(iter_sessions(rows, idle_gap_s))
    sessions_by_month: dict[str, int] = defaultdict(int)
    for _a, _m, first_ts in sessions:
        sessions_by_month[first_ts.strftime("%Y-%m")] += 1

    # Monthly trends (all rows; human turns counted separately, never in the token rollup).
    by_month: dict[str, dict] = {}
    for r in rows:
        mb = by_month.setdefault(r.ts.strftime("%Y-%m"),
                                 {"tokens": 0, "in": 0, "cr": 0, "human": 0, "agent": 0,
                                  "days": set(), "model_tokens": defaultdict(int)})
        mb["days"].add(r.ts.date())
        if r.kind == "human":
            mb["human"] += 1
            continue
        mb["agent"] += 1
        mb["tokens"] += r.total_tokens
        mb["in"] += r.input_tokens
        mb["cr"] += r.cache_read_tokens
        mb["model_tokens"][r.model or "unknown"] += r.total_tokens

    trends = []
    for month in sorted(by_month):
        mb = by_month[month]
        denom = mb["tokens"] or 1
        trends.append({
            "month": month,
            "tokens": mb["tokens"],
            "cache_hit_rate": round(_safe_div(mb["cr"], mb["cr"] + mb["in"]), 4),
            "active_days": len(mb["days"]),
            "sessions": sessions_by_month.get(month, 0),
            "human_turns": mb["human"],
            "agent_turns": mb["agent"],
            "model_mix": {mdl: round(tok / denom, 4) for mdl, tok in
                          sorted(mb["model_tokens"].items(), key=lambda kv: -kv[1])},
        })

    # Per-agent adoption span.
    spans: dict[str, dict] = {}
    for r in rows:
        s = spans.setdefault(r.agent, {"first": r.ts, "last": r.ts})
        s["first"] = min(s["first"], r.ts)
        s["last"] = max(s["last"], r.ts)
    fluency_agents = {
        a: {"first_seen": v["first"].date().isoformat(),
            "last_seen": v["last"].date().isoformat(),
            "months_active": (v["last"].year - v["first"].year) * 12
                             + (v["last"].month - v["first"].month) + 1}
        for a, v in spans.items()
    }

    human_n, agent_n = len(human_rows), len(agent_rows)
    return {
        "as_of": now.date().isoformat(),
        "window": {
            "first_ts": min((r.ts for r in rows), default=now).isoformat(),
            "last_ts": max((r.ts for r in rows), default=now).isoformat(),
        },
        "volume": {
            "total_tokens": tot_tokens,
            "est_cost_usd": round(actual, 2) if priced_tokens else None,
            "priced": priced_tokens > 0,
        },
        "efficiency": {
            "cache_hit_rate": round(_safe_div(tot_cr, tot_cr + tot_in), 4),
            "cache_savings_multiple": round(_safe_div(naive, actual), 2) if actual else None,
            "output_leverage": round(_safe_div(tot_out, tot_in), 4),
            "reasoning_share": round(_safe_div(tot_reason, tot_tokens), 4),
            "cost_per_active_day": round(actual / len(active_days), 2)
                                   if (actual and active_days) else None,
        },
        "fluency": {
            "agents": fluency_agents,
            "active_days": len(active_days),
            "sessions": len(sessions),
            "distinct_models": len({r.model for r in agent_rows if r.model}),
            "distinct_agents": len({r.agent for r in rows}),
        },
        "collaboration": {
            "human_turns": human_n,
            "agent_turns": agent_n,
            "agent_turns_per_human_turn": round(_safe_div(agent_n, human_n), 2) if human_n else None,
            "human_turn_share": round(_safe_div(human_n, human_n + agent_n), 4),
        },
        "trends": {"by_month": trends},
        "priced_coverage": {
            "models_priced": len(priced_models),
            "models_unpriced": sorted(unpriced_models),
            "unpriced_token_share": round(
                _safe_div(unpriced_tokens, priced_tokens + unpriced_tokens), 4),
        },
        "instrument": _INSTRUMENT,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_metrics.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agent_usage_tracker/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): compute_metrics assembling efficiency/fluency/collaboration/volume/trends"
```

---

### Task 7: Report — sink read + JSON writer

**Files:**
- Create: `src/agent_usage_tracker/report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `metrics.Row`, `metrics.compute_metrics` (Tasks 4–6); `pricing.load_prices` (Task 2).
- Produces: `fetch_rows(dsn: str) -> list[Row]` (reads `agents.usage_event`, `project` NOT selected; lazy `import psycopg`); `run_report(dsn, prices_path, out, *, idle_gap_s=1800, now=None, fetch=None) -> dict` (fetch rows -> compute -> atomic-write JSON; returns the metrics dict; raises `SystemExit` if `dsn` is falsy). `fetch` resolves to `fetch_rows` when `None`, so it is monkeypatchable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_usage_tracker.report'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent_usage_tracker/report.py
"""Read side: pull safe aggregate rows from the sink and write metrics.json.

I/O only — the math lives in metrics.compute_metrics. The SELECT deliberately omits `project`,
so a project name can never reach the output (privacy by construction).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .metrics import Row, compute_metrics
from .pricing import load_prices

_SELECT = """
    SELECT ts, agent, machine, model, kind,
           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
           reasoning_tokens, total_tokens
    FROM agents.usage_event
    ORDER BY agent, machine, ts
"""


def fetch_rows(dsn: str) -> list[Row]:
    """Read all usage rows from the sink. `project` is never selected.

    ponytail: pulls the whole table into memory. Fine for a personal dataset; if it ever
    outgrows RAM, push the monthly bucketing into SQL. Not needed now.
    """
    try:
        import psycopg  # optional dependency; install with the [postgres] extra
    except ImportError as exc:
        raise SystemExit("aut report needs the [postgres] extra: "
                         "uv tool install 'agent-usage-tracker[postgres]'") from exc
    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT)
            return [Row(*r) for r in cur.fetchall()]


def run_report(dsn: str | None, prices_path: str | Path, out: str | Path, *,
               idle_gap_s: int = 1800, now: datetime | None = None,
               fetch: Callable[[str], list[Row]] | None = None) -> dict:
    """Fetch rows -> compute -> atomic-write metrics.json. Returns the metrics dict."""
    if not dsn:
        raise SystemExit("aut report needs a read DSN: set [transport].pg_dsn in your config "
                         "(it doubles as the report's read source).")
    fetch = fetch or fetch_rows
    rows = fetch(dsn)
    metrics = compute_metrics(rows, load_prices(prices_path), idle_gap_s=idle_gap_s,
                              now=now or datetime.now(timezone.utc))
    path = Path(out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return metrics
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agent_usage_tracker/report.py tests/test_report.py
git commit -m "feat(report): read the sink (project omitted) and write metrics.json"
```

---

### Task 8: CLI — `aut report`

**Files:**
- Modify: `src/agent_usage_tracker/cli.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `report.run_report`, `report.fetch_rows` (Task 7); `cfg.pg_dsn` (existing `Config`); `config._config_path` (imported in Task 3).
- Produces: `aut report [--out PATH] [--prices PATH] [--idle-gap SECONDS]`. Reads the DSN from `cfg.pg_dsn`. Default `--out` = `<config-dir>/metrics.json`; default `--prices` = `<config-dir>/model_prices.json`; default `--idle-gap` = `1800`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_report.py`, above `__main__`)

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report.py::test_cli_report_writes_metrics -q`
Expected: FAIL — argparse rejects `report` (`invalid choice`), raising `SystemExit`.

- [ ] **Step 3: Write minimal implementation**

In `main()` of `cli.py`, register the subparser (next to `sync-prices`):

```python
    p_report = sub.add_parser("report",
                              help="compute showcase metrics from the sink -> metrics.json")
    p_report.add_argument("--out", help="output JSON (default: <config-dir>/metrics.json)")
    p_report.add_argument("--prices", help="price map (default: <config-dir>/model_prices.json)")
    p_report.add_argument("--idle-gap", type=int, default=1800,
                          help="session idle-gap seconds (default 1800)")
```

In `main()`, handle it AFTER `cfg = load_config()` (it needs `cfg.pg_dsn`). Place this block right after the `cfg = load_config()` / `agents = cfg.agents` setup and before the `status` handler:

```python
    if args.cmd == "report":
        from . import report as report_mod
        out = args.out or str(_config_path().parent / "metrics.json")
        prices = args.prices or str(_config_path().parent / "model_prices.json")
        metrics = report_mod.run_report(cfg.pg_dsn, prices, out, idle_gap_s=args.idle_gap)
        cov = metrics["priced_coverage"]
        print(f"wrote metrics -> {out}  (priced {cov['models_priced']} models, "
              f"{cov['unpriced_token_share']:.0%} of tokens unpriced)", file=sys.stderr)
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_report.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the whole suite and commit**

Run: `python -m pytest -q`
Expected: PASS (all existing collector tests plus the new pricing/metrics/report tests).

```bash
git add src/agent_usage_tracker/cli.py tests/test_report.py
git commit -m "feat(cli): aut report subcommand (reads pg_dsn, writes metrics.json)"
```

---

### Task 9: Docs, config example, and the nightly publish job

**Files:**
- Modify: `README.md`
- Modify: `CONTRACT.md`
- Modify: `config.example.toml`
- Create: `packaging/nightly-metrics.md` (a short how-to for the homelab cron)

**Interfaces:** none (documentation).

- [ ] **Step 1: Update `README.md`**

In the `## Use` code block, add the two new commands under the existing ones:

```bash
aut sync-prices        # refresh the API-equivalent model price map (LiteLLM -> models.dev)
aut report             # compute showcase metrics from the sink -> metrics.json (needs [postgres])
```

Add a new section after `## How it stays correct`:

```markdown
## Showcase metrics (derived, opt-in)

`aut report` reads the **unified sink** (via `transport.pg_dsn`, which doubles as the report's
read source) and writes a small `metrics.json` for a personal-site showcase. It is a pure
derivation over data you already have — the collector is unchanged, and **`project` is never
read**, so no project name can leak into the output (aggregate-by-construction).

- **Framing:** it reports *ratios and trends* (cache-hit %, output leverage, adoption timeline,
  agent-turns-per-human-prompt), not a token scoreboard. Volume appears once, framed as
  API-equivalent work done on a flat-rate subscription.
- **Pricing:** `aut sync-prices` maintains `model_prices.json` from LiteLLM (primary) and
  models.dev (fallback), both open source, extended to
  carry the cache-read/write rates this tool needs. Every `$` figure is **notional /
  API-equivalent**; a model missing from the map is reported under `priced_coverage`, never guessed.
- **Sessions** are derived at report time by gap (default 30 min via `--idle-gap`), so the
  count covers all history without any collector change.
```

- [ ] **Step 2: Update `CONTRACT.md`**

Append a section:

```markdown
## Derived artifact: metrics.json (not part of the event contract)

`aut report` produces `metrics.json` — an aggregate of the events above, never a passthrough.
It is read from the sink with the `project` column deliberately excluded, so the published
artifact aggregates to `agent × model × month × token-class (× kind)` and cannot carry a
project name. `$` figures are notional / API-equivalent, computed from the synced price map
(`aut sync-prices`), not from `est_cost_usd`.
```

- [ ] **Step 3: Update `config.example.toml`**

Under the `[transport]` section (near `pg_dsn`), add a comment noting the dual use:

```toml
# pg_dsn is the optional direct-Postgres transport AND the read source for `aut report`
# (the showcase-metrics command). Point it at the sink to enable `aut report`.
```

- [ ] **Step 4: Create `packaging/nightly-metrics.md`**

```markdown
# Nightly showcase-metrics refresh (homelab)

Run this on the box that sits next to the sink Postgres (so the read is local). It refreshes
the price map, then regenerates `metrics.json` for the website to pick up. Not live — a nightly
cron is deliberate.

Install with the postgres extra:

    uv tool install "git+https://github.com/rhnfzl/agent-usage-tracker[postgres]"

Crontab (03:15 nightly), writing where the site build reads it:

    15 3 * * *  aut sync-prices && aut report --out /srv/www/data/metrics.json

`aut report` reads the DSN from `transport.pg_dsn` in the aut config on that box. Failures are
safe: `sync-prices` keeps the prior price file if the source is unreachable, and `report`
writes atomically (temp + rename), so the site never sees a half-written file.

(Alternatively, mirror the launchd/systemd units in this directory for `aut scan` and change
the command to the two above.)
```

- [ ] **Step 5: Commit**

```bash
git add README.md CONTRACT.md config.example.toml packaging/nightly-metrics.md
git commit -m "docs: showcase-metrics usage, contract note, config hint, nightly cron"
```

---

## Self-Review

**Spec coverage:**
- §4.A Efficiency ratios → Tasks 5–6 (`cost_of`/`naive_cost_of`, `cache_hit_rate`, `output_leverage`, `reasoning_share`, `cost_per_active_day`). ✔
- §4.B Fluency trends → Task 6 (`fluency` block, `trends.by_month`) + Task 4 (sessions). ✔
- §4.C Collaboration (human/agent) → Task 6 (`collaboration` block). ✔
- §4.D Instrument facts → Task 6 (`_INSTRUMENT`). ✔
- §4.E Contextual volume → Task 6 (`volume` block). ✔
- §5 Sessions derived, no collector change → Task 4. ✔
- §6 Price map (LiteLLM + models.dev, cache fields, atomic write, graceful degradation) → Tasks 1–3; `priced_coverage` → Task 6. ✔
- §7 Output + publish (`aut report`, pure core, nightly cron, deferred summary) → Tasks 7–9. ✔
- §8 Privacy (project never selected) → Task 7 `_SELECT` + Global Constraints. ✔
- §9 Testing (golden fixture, sessionization, pricing math, degradation) → Tasks 1,2,4,5,6,7. ✔
- §10 Read DSN reuse (`pg_dsn`) → Tasks 7–8. ✔

**Placeholder scan:** no TBD/TODO; every code step carries complete, runnable code. The nightly-cron GitHub URL uses the real `rhnfzl/agent-usage-tracker` remote.

**Type consistency:** `Row` field order is identical in Task 4 (definition), the test fixtures, and `report.fetch_rows` (`Row(*r)` over the matching `_SELECT` column order). `sync_prices(..., fetch=None)` and `run_report(..., fetch=None)` both resolve their default fetcher at call time, so the CLI tests' monkeypatching works. `compute_metrics(rows, prices, *, idle_gap_s, now)` signature matches every caller (test + `run_report`). `load_prices` returns `{}` for a missing file, which `compute_metrics` handles (all-unpriced → `$` metrics `None`).
