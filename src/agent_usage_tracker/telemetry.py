"""Supported OTLP JSON token sums/histograms and GenAI spans, with content dropped."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .observations import MAX_BATCH_RECORDS, stable_id, validate_record

TOKEN_KEYS = {
    "input": "input_tokens_total",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
}
SPAN_KEYS = {
    "gen_ai.usage.input_tokens": "input_tokens_total",
    "gen_ai.usage.output_tokens": "output_tokens",
    "gen_ai.usage.reasoning.output_tokens": "reasoning_tokens",
    "gen_ai.usage.cache_read.input_tokens": "cache_read_tokens",
    "gen_ai.usage.cache_creation.input_tokens": "cache_write_tokens",
}


def _identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _identity(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [_identity(item) for item in value]
        if all(isinstance(item, dict) and "key" in item for item in items):
            return sorted(items, key=stable_id)
        return items
    return value


def _integer(value: Any) -> int:
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("invalid OTLP int64")
    return value


def _time(value: Any) -> str:
    ns = _integer(value)
    return (
        datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ns // 1000)
    ).isoformat()


def _attrs(items: Any) -> dict[str, Any]:
    allowed = {"gen_ai.token.type", "gen_ai.operation.name", *SPAN_KEYS}
    result = {}
    for item in items:
        key = item.get("key")
        if key in allowed:
            value = item.get("value", {})
            result[key] = value.get("stringValue", value.get("intValue"))
    return result


def normalize_otlp(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """Return validated content-free observations plus unsupported instrument count.

    Resource and scope metadata, arbitrary attributes, names, events and bodies are dropped.
    Cumulative counters remain nonadditive snapshots in the observation store.
    """
    if not isinstance(payload, dict):
        raise ValueError("OTLP payload must be an object")
    result: list[dict[str, Any]] = []
    unsupported = 0
    for resource in payload.get("resourceMetrics", []):
        for scope in resource.get("scopeMetrics", []):
            for metric in scope.get("metrics", []):
                instrument = metric.get("sum", metric.get("histogram"))
                if metric.get("name") != "gen_ai.client.token.usage" or not instrument:
                    unsupported += 1
                    continue
                temporality = instrument.get("aggregationTemporality")
                if type(temporality) is not int or temporality not in (1, 2):
                    unsupported += 1
                    continue
                if "sum" in metric and instrument.get("isMonotonic") is not True:
                    unsupported += 1
                    continue
                for point in instrument.get("dataPoints", []):
                    attrs = _attrs(point.get("attributes", []))
                    key = TOKEN_KEYS.get(attrs.get("gen_ai.token.type", ""))
                    value = (
                        point.get("sum")
                        if "histogram" in metric
                        else point.get("asInt", point.get("asDouble"))
                    )
                    if key is None or value is None or point.get("flags", 0) & 1:
                        unsupported += 1
                        continue
                    r: dict[str, Any] = {
                        "version": 1,
                        "kind": "usage",
                        "timestamp": _time(point["timeUnixNano"]),
                        "temporality": "delta" if temporality == 1 else "cumulative",
                        "counter": stable_id(
                            [
                                metric["name"],
                                key,
                                _identity(resource.get("resource", {})),
                                _identity(scope.get("scope", {})),
                                _identity(point.get("attributes", [])),
                            ]
                        ),
                        key: _integer(value),
                    }
                    if _integer(point.get("startTimeUnixNano", 0)):
                        r["start"] = _time(point["startTimeUnixNano"])
                    r["id"] = stable_id(["metric", r["counter"], _integer(point["timeUnixNano"])])
                    result.append(validate_record(r))
    for resource in payload.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                attrs = _attrs(span.get("attributes", []))
                counts = {
                    target: _integer(attrs[key])
                    for key, target in SPAN_KEYS.items()
                    if key in attrs
                }
                common = {"version": 1, "timestamp": _time(span["endTimeUnixNano"])}
                if counts:
                    r = dict(common, kind="usage", **counts)
                    total, read = counts.get("input_tokens_total"), counts.get("cache_read_tokens")
                    # Cache creation inclusion varies by provider, so fresh stays unknown.
                    if (
                        total is not None
                        and read is not None
                        and ("cache_write_tokens" in counts and counts["cache_write_tokens"] == 0)
                    ):
                        if read > total:
                            raise ValueError("cache read exceeds inclusive input")
                        r["input_tokens"] = total - read
                    r["id"] = stable_id(["span", span["traceId"], span["spanId"]])
                    result.append(validate_record(r))
                elif attrs.get("gen_ai.operation.name") == "execute_tool":
                    start = _integer(span["startTimeUnixNano"])
                    end = _integer(span["endTimeUnixNano"])
                    if end < start:
                        raise ValueError("invalid span duration")
                    code = span.get("status", {}).get("code", 0)
                    r = dict(
                        common,
                        kind="tool",
                        status={1: "ok", 2: "error"}.get(code, "unknown"),
                        duration_ms=(end - start) // 1000000,
                    )
                    r["id"] = stable_id(["span", span["traceId"], span["spanId"]])
                    result.append(validate_record(r))
                else:
                    unsupported += 1
    if any("counter" in r for r in result) and any(
        r["kind"] == "usage" and "counter" not in r for r in result
    ):
        raise ValueError("metric and span usage overlap; import separate crosscheck streams")
    if len(result) > MAX_BATCH_RECORDS:
        raise ValueError("too many OTLP records")
    return result, unsupported
