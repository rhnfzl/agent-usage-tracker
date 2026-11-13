# Import external observations privately

External observations let AUT inspect usage, quota and tool reliability from additional producers.
They remain separate from the existing SQL sink, so a quota snapshot cannot become token usage.

The library accepts local normalized JSONL and a supported OTLP JSON subset.
No provider endpoint, credential file or hosted service is accessed during import.

## Local library API

```python
from pathlib import Path
from agent_usage_tracker.observations import import_file, records

database = Path.home() / ".local/share/aut/observations.sqlite3"
receipt = import_file(
    database, Path("usage.jsonl"), format="jsonl",
    source="my-sdk", stream="usage-events", role="primary",
)
observations = records(database)
```

The first argument is a SQLite file path, not the private improvement `Store` object.
`ingest(database, records, *, source, stream, role)` accepts normalized records directly.
`validate_record(record)` validates one record without storing it.
Import receipts report accepted, duplicate, conflicting and crosscheck-excluded observations.
OTLP imports also report unsupported instruments or points.

SQLite files use mode 0600. Newly created parent directories use mode 0700.
Choose a private location; AUT does not change permissions on existing parent directories.

## Normalized schema, version 1

Every record requires integer `version: 1`, `kind`, `id` and a timezone-aware ISO `timestamp`.
Identifiers use at most 128 letters, digits or the punctuation `_.:/-`, starting with a letter or digit.
Unknown fields are rejected. Text such as prompts, titles, arguments and results is not a record field.

Counts are nonnegative int64 values or explicit null. Booleans and fractional values are not counts.
Missing usage counts become null, never zero. At least one usage count must be known.

### Usage

```json
{"version":1,"kind":"usage","id":"run-001","timestamp":"2026-11-13T10:00:00Z","input_tokens":20,"input_tokens_total":35,"output_tokens":12,"cache_read_tokens":10,"cache_write_tokens":5,"reasoning_tokens":4}
```

`input_tokens` means known fresh input. `input_tokens_total` is the reported inclusive input.
Reasoning is a subset of output. Never add reasoning to output or an inclusive input total to its components.
When fresh input is known, fresh, cache-read and cache-write counts are disjoint under AUT's contract.
Known disjoint components cannot exceed the inclusive input total.
When fresh input is unknown, the relationship between cache creation and the reported total can remain unknown.

Optional `counter`, `temporality` and `start` describe a counter series.
Temporality is `delta` or `cumulative`. A start or temporality requires a counter identifier.
A non-null start must precede the observation timestamp.

### Quota

```json
{"version":1,"kind":"quota","id":"quota-001","timestamp":"2026-11-13T10:00:00Z","remaining":1200,"limit":2000,"unit":"tokens","period":"day","reset":"2026-11-14T00:00:00Z","fresh_until":"2026-11-13T10:05:00Z","scope":"account-opaque-id"}
```

Every quota field shown is required. Numeric values and reset/freshness times can be null.
Units are `tokens`, `requests`, `percent`, `credits` or `unknown`.
Periods are `minute`, `hour`, `day`, `week`, `month`, `session` or `unknown`.
Quota is a latest-value gauge, never additive usage. Consumers must compare freshness with their evaluation clock.
The importer does not infer account limits, reset times or remaining quota.

### Tools

```json
{"version":1,"kind":"tool","id":"operation-001","timestamp":"2026-11-13T10:00:00Z","status":"error","duration_ms":150}
```

Status is `ok`, `error` or `unknown`. Duration is optional, nonnegative integer milliseconds or null.
Tool names, arguments, descriptions and output bodies are excluded from this schema.
Tool success alone does not establish task completion or human effort.

## Prevent overlapping totals

Source and stream identify the producer and representation. Role is `primary` or `crosscheck`.
Each source has one primary stream; alternate representations use a separate crosscheck stream.
Use a consistent source identifier for overlapping representations. Different source names do not prove independence.

Stream role cannot change. Event usage and counter usage cannot mix in one stream.
These rules apply across imports. A rejected conflicting copy cannot change ownership or counter state.
Exact repeated identities are duplicates. Changed copies are conflicts and preserve the original evidence.

Only records marked `additive: true` may contribute to usage totals.
Crosschecks, quotas and tool observations never contribute additive tokens.

## Read cumulative counters safely

`records()` retains raw cumulative snapshots and adds `delta` for eligible increments.
Use the derived delta for cumulative totals, not the raw snapshot count.
For ordinary event or delta records, use their count fields only when `additive` is true.

Ordered points with the same known start can yield differences.
A known, nonoverlapping new start can identify a reset. Unknown starts, decreases, overlapping windows
and invalid derived components remain visible through coverage fields and nonadditive records.
Missing component values remain unknown after subtraction.

Counter ordering and temporality checks occur in the ingestion transaction.
Exact retries are checked before ordering, so a lost acknowledgment does not make a valid retry out of order.
Role changes and incompatible representations reject the transaction.

## Supported OTLP JSON

Use `format="otlp"` for JSON metric or trace envelopes.
This is a supported subset, not binary Protobuf, OTLP logs or a complete exporter implementation.

The adapter recognizes `gen_ai.client.token.usage` sum or histogram values with token-type dimensions.
It supports explicit delta and cumulative temporality, including decimal-string integer timestamps.
GenAI spans can provide input, output, cache-read, cache-creation and reasoning counts.
Input stays inclusive when a fresh split cannot be established.
`execute_tool` spans can provide tool status and duration.

Protocol identities do not include mutable counts or status. Changed copies therefore conflict rather than add tokens.
Attribute order does not change the series identity. Arbitrary attribute bodies are not stored as text.
Unsupported instruments remain counted. Mixed metric/span usage is not silently combined into primary totals.

The implementation uses the [OTLP JSON specification](https://opentelemetry.io/docs/specs/otlp#json-protobuf-encoding),
[metrics data model](https://opentelemetry.io/docs/specs/otel/metrics/data-model) and supported
[GenAI attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai).
The adapter's supported keys define its compatibility boundary.

## Bounds

Files are limited to 8 MiB, normalized records to 4096 bytes, batches to 10,000 records,
and a store to 1,000,000 observations. Exceeding a bound fails instead of truncating evidence.
The current reader returns the bounded store as a list; report memory grows with stored observations.
No automatic retention deletion or publication occurs.

## Opt-in SDK collection

Applications can append normalized usage to a private local file without a vendor dependency.
The producer must supply a stable event ID and a timezone-aware timestamp.

```python
from agent_usage_tracker.sdk import emit_usage

emit_usage(
    "/private/path/sdk.jsonl",
    {"version": 1, "kind": "usage", "id": "request-42",
     "timestamp": "2026-11-13T12:00:00Z",
     "input_tokens": 20, "output_tokens": 10,
     "cache_read_tokens": 0, "cache_write_tokens": 0},
    producer="my-application", model="declared-model", provider="declared-provider",
)
```

Enable it explicitly in the normal AUT configuration:

```toml
agents = ["generic_sdk"]
generic_sdk_paths = "~/private/sdk/*.jsonl"
```

The SDK envelope accepts only version, producer, usage, model, provider and native_source.
Use native_source for an event already represented by a native AUT collector.
Known overlap with configured or selected collectors fails. Unknown overlap cannot establish independence.
Do not mirror native logs into this collector without explicit source separation.

Fresh input, cache-read, cache-write and output must all be known before a SQL usage row can be emitted.
Reasoning remains a subset of output and is never added twice.
Missing components and counter records produce visible unavailable or unsupported counts, without SQL usage.
The observation importer can retain partial or counter evidence separately.

Complete records use producer and event ID for identity, independent of file paths.
Changed copies fail before delivery. Identical copies and delivery retries retain the same identity.
Each bounded batch advances its watermark only after acknowledgment.
The collector drains retained files for export and repeated scans. A partial final line remains pending.

Use one writer per file and rotate before the 8 MiB file limit.
The emitter refuses to append after an incomplete record.
The collector retains up to one million identity digests and validates conflicts before delivery.
Its scan memory grows with retained history. Removing unseen source data prevents later recovery.
No credentials, network calls or new default collectors are added.
