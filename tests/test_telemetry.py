import json

import pytest

from agent_usage_tracker.observations import import_file, ingest, records
from agent_usage_tracker.telemetry import normalize_otlp


def point(value=10, start="1000000000", end="2000000000", temporality=2):
    return {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "gen_ai.client.token.usage",
                                "histogram": {
                                    "aggregationTemporality": temporality,
                                    "dataPoints": [
                                        {
                                            "startTimeUnixNano": start,
                                            "timeUnixNano": end,
                                            "sum": value,
                                            "attributes": [
                                                {
                                                    "key": "gen_ai.token.type",
                                                    "value": {"stringValue": "input"},
                                                },
                                                {
                                                    "key": "prompt",
                                                    "value": {"stringValue": "SECRET"},
                                                },
                                            ],
                                        }
                                    ],
                                },
                            },
                            {"name": "unsupported.private.name", "gauge": {"dataPoints": []}},
                        ]
                    }
                ]
            }
        ]
    }


def test_histogram_privacy_retry_reset(tmp_path):
    db = tmp_path / "usage.db"

    def add(payload):
        values, unsupported = normalize_otlp(payload)
        assert unsupported == 1
        assert "SECRET" not in json.dumps(values)
        return ingest(db, values, source="otel", stream="metric", role="primary")

    assert add(point())["accepted"] == 1
    assert add(point())["duplicates"] == 1
    add(point(15, end="3000000000"))
    add(point(2, start="3000000000", end="4000000000"))
    values = records(db)
    assert [r["delta"]["input_tokens_total"] for r in values] == [10, 5, 2]
    assert all(r["input_tokens"] is None for r in values)
    assert all(r["additive"] for r in values)
    with pytest.raises(ValueError, match="out-of-order"):
        add(point(16, end="2500000000"))
    assert len(records(db)) == 3


def test_unknown_start_decrease_crosscheck(tmp_path):
    db = tmp_path / "usage.db"
    for payload in (
        point(start="0"),
        point(20, start="0", end="3000000000"),
        point(2, start="3000000000", end="4000000000"),
        point(1, start="3000000000", end="5000000000"),
    ):
        values, _ = normalize_otlp(payload)
        ingest(db, values, source="otel", stream="metric", role="crosscheck")
    values = records(db)
    assert not any(r["additive"] for r in values)
    assert values[0]["coverage"] == "unknown_first_point"
    assert values[1]["coverage"] == "unknown_start"
    assert values[-1]["coverage"] == "counter_decreased"


def test_span_tool_and_cache(tmp_path):
    span = {
        "traceId": "1",
        "spanId": "2",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "name": "SECRET",
        "attributes": [
            {"key": k, "value": {"intValue": str(v)}}
            for k, v in (
                ("gen_ai.usage.input_tokens", 10),
                ("gen_ai.usage.cache_read.input_tokens", 3),
                ("gen_ai.usage.cache_creation.input_tokens", 0),
            )
        ],
    }
    tool = dict(
        span,
        spanId="3",
        attributes=[{"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}}],
    )
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [span, tool]}]}]}
    path = tmp_path / "otlp.json"
    path.write_text(json.dumps(payload))
    db = tmp_path / "db"
    assert (
        import_file(db, path, format="otlp", source="otel", stream="spans", role="primary")[
            "accepted"
        ]
        == 2
    )
    result = records(db)
    assert "SECRET" not in json.dumps(result)
    assert next(r for r in result if r["kind"] == "usage")["input_tokens"] == 7
    assert next(r for r in result if r["kind"] == "tool")["duration_ms"] == 1000


def test_delta_overlap(tmp_path):
    db = tmp_path / "db"
    for payload in (
        point(3, temporality=1),
        point(4, start="1500000000", end="3000000000", temporality=1),
    ):
        values, _ = normalize_otlp(payload)
        ingest(db, values, source="otel", stream="metrics", role="primary")
    assert not any(r["additive"] for r in records(db))


def test_protocol_identity_conflicts(tmp_path):
    db = tmp_path / "db"
    for value in (10, 11):
        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "trace",
                                    "spanId": "span",
                                    "endTimeUnixNano": "2000000000",
                                    "attributes": [
                                        {
                                            "key": "gen_ai.usage.input_tokens",
                                            "value": {"intValue": str(value)},
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        rows, _ = normalize_otlp(payload)
        outcome = ingest(db, rows, source="otel", stream="one", role="primary")
    assert outcome["conflicts"] == 1
    assert len(records(db)) == 1
    db = tmp_path / "metrics"
    for value in (10, 11):
        rows, _ = normalize_otlp(point(value))
        outcome = ingest(db, rows, source="otel", stream="one", role="primary")
    assert outcome["conflicts"] == 1


def test_persistent_representation_and_role_ownership(tmp_path):
    db = tmp_path / "db"
    event = dict(
        version=1, kind="usage", id="span", timestamp="1970-01-01T00:00:02Z", input_tokens_total=10
    )
    ingest(db, [event], source="otel", stream="one", role="primary")
    rows, _ = normalize_otlp(point(20, end="3000000000"))
    with pytest.raises(ValueError, match="representation"):
        ingest(db, rows, source="otel", stream="one", role="primary")
    assert len(records(db)) == 1
    other = tmp_path / "roles"
    rows, _ = normalize_otlp(point())
    ingest(other, rows, source="otel", stream="one", role="crosscheck")
    rows, _ = normalize_otlp(point(20, end="3000000000"))
    with pytest.raises(ValueError, match="role"):
        ingest(other, rows, source="otel", stream="one", role="primary")
    assert len(records(other)) == 1


def test_reasoning_span_subset():
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "trace",
                                "spanId": "span",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [
                                    {"key": key, "value": {"intValue": str(value)}}
                                    for key, value in (
                                        ("gen_ai.usage.output_tokens", 10),
                                        ("gen_ai.usage.reasoning.output_tokens", 4),
                                    )
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    rows, _ = normalize_otlp(payload)
    assert rows[0]["reasoning_tokens"] == 4
    assert rows[0]["output_tokens"] == 10
