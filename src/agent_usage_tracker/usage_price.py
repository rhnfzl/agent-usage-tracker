"""Price the measured token components using the same rules as agents.usage_price."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

from .event import UsageEvent
from .pricing import COMPONENT_RATES, price_key


@dataclass(frozen=True)
class PricedUsage:
    known_cost: Decimal | None
    denominator: int
    priced: int
    unattributed: int


def price_counts(counts: list[int], total: int, rate: dict[str, float]) -> PricedUsage:
    if total < 0 or any(n < 0 for n in counts):
        # A bad count makes the row unpriced. Keep the positive evidence in coverage.
        positive = sum(n for n in counts if n > 0)
        return PricedUsage(None, max(positive, total), 0, max(0, total - positive))
    # Reasoning is already included in output. A missing rate only matters for a used component.
    known = [(n, Decimal(str(rate[key]))) for n, (_, key) in zip(counts, COMPONENT_RATES)
             if n > 0 and key in rate]
    priced = sum(n for n, _ in known)
    cost = sum((n * unit_cost for n, unit_cost in known), Decimal(0))
    component_tokens = sum(counts)
    unattributed = max(0, total - component_tokens)
    denominator = component_tokens + unattributed
    return PricedUsage(cost if priced or not denominator else None,
                       denominator, priced, unattributed)


def price_usage(row: UsageEvent, rates: dict[str, dict[str, float]]) -> PricedUsage:
    key = price_key(row.model, rates)
    counts = [getattr(row, field) for field, _ in COMPONENT_RATES]
    return price_counts(counts, row.total_tokens, rates.get(key, {}))


class PriceTotals(TypedDict):
    coverage_tokens: int
    priced_tokens: int
    unpriced_tokens: int
    unattributed_tokens: int
    unpriced_events: int
    known_api_cost_usd: str | None
    estimated_api_cost_usd: str | None


class PriceTally:
    def __init__(self) -> None:
        self.denominator = 0
        self.priced = 0
        self.unattributed = 0
        self.unpriced_events = 0
        self.known_cost: Decimal | None = None

    def add(self, row: PricedUsage) -> None:
        self.denominator += row.denominator
        self.priced += row.priced
        self.unattributed += row.unattributed
        self.unpriced_events += row.known_cost is None or row.priced < row.denominator
        if row.known_cost is not None:
            self.known_cost = (self.known_cost or Decimal(0)) + row.known_cost

    def result(self) -> PriceTotals:
        cost = format(self.known_cost, "f") if self.known_cost is not None else None
        return {
            "coverage_tokens": self.denominator,
            "priced_tokens": self.priced,
            "unpriced_tokens": self.denominator - self.priced,
            "unattributed_tokens": self.unattributed,
            "unpriced_events": self.unpriced_events,
            "known_api_cost_usd": cost,
            "estimated_api_cost_usd": cost if not self.unpriced_events else None,
        }


class Comparison(TypedDict):
    assumption: str
    coverage_tokens: int
    model_cost_usd: dict[str, str] | None
    reason: str | None


def compare_prices(models: tuple[str, str], rates: dict[str, dict[str, float]],
                   tokens: dict[str, int | None], denominator: int) -> Comparison:
    result: Comparison = {
        "assumption": "Apply both rates to the same observed input, output and cache token counts.",
        "coverage_tokens": denominator,
        "model_cost_usd": None,
        "reason": None,
    }
    keys = tuple(price_key(model, rates) for model in models)
    if keys[0] == keys[1] or any(key not in rates for key in keys):
        result["reason"] = "Two distinct models with local rates are required."
        return result
    counts = [tokens[field] or 0 for field, _ in COMPONENT_RATES]
    if not denominator or sum(counts) != denominator:
        result["reason"] = "The window needs observed usage with complete token components."
        return result
    estimates = {key: price_counts(counts, denominator, rates[key]) for key in keys}
    if any(cost.priced != denominator for cost in estimates.values()):
        result["reason"] = "Both models need a rate for each token component used in the window."
        return result
    result["model_cost_usd"] = {key: format(cost.known_cost, "f") for key, cost in estimates.items()
                                if cost.known_cost is not None}
    return result
