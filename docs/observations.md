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

## Import and inspect observations

These commands read explicit local inputs and keep a separate private SQLite database.
They do not load sink credentials or change normal scans.

```bash
aut observe --db ~/.local/share/aut/observations.sqlite3 import ~/private/usage.jsonl \
  --format jsonl --source my-application --stream requests --role primary
aut observe --db ~/.local/share/aut/observations.sqlite3 report --format html \
  --out ~/.local/share/aut/observations.html
aut observe --db ~/.local/share/aut/observations.sqlite3 report --format json
```

Use `--format otlp` for a supported OTLP JSON file.
Use `--role crosscheck` for an alternative view of an already counted source.
A source has one primary stream. Tool and usage identities must remain stable across retries.
Changed copies produce conflicts. Valid records in a conflicting batch may already be durable.
Inspect the returned receipt and retry identical records after resolving the conflict.

Usage reports show complete component totals, known lower bounds and missing counts separately.
Quota reports show latest remaining/limit values, units, reset times and freshness.
A missing expiry stays unknown. A quota value never becomes consumed token usage.
Tools retain separate source, stream and role groups.
Their summed durations describe operation time, including nested operations, not elapsed task time.
No tool count establishes human activity or business completion.

Add `--improve-root /private/improvement-store` to `report` for immutable study and desktop receipts.
The selected metric, scoped decision, unknowns and recorded costs remain separate from general productivity.
Reports written with `--out` must be outside Git.

## Native desktop evidence

Native command evidence can explain recovery that a parent coding session only records as one tool call.
It needs explicit parent association and trace coverage.

```bash
aut improve --root ~/.local/share/aut/desktop-evidence desktop \
  --trace ~/private/desktop-segments --responses ~/private/desktop-responses.jsonl \
  --mapping ~/private/desktop-mapping.json
```

The mapping contains exactly these nonempty identifiers:

```json
{"task_id":"task-42","session_id":"aut-session-7","parent_call_id":"call-3",
 "desktop_session_id":"native-session-2"}
```

The adapter accepts raw Agent Desktop JSONL segments, a segment directory or a trace-show JSON envelope.
Viewer exports with truncated or filtered events remain incomplete.
Trace identities use writer PID, process start and sequence, with the explicit desktop session.
A matching response companion has this shape:

```json
{"writer_pid":123,"writer_proc_start_ms":1794580000000,"command_seq":1,
 "response":{"ok":false,"command":"click",
 "error":{"code":"STALE_REF","disposition":{"delivery":"not_delivered","retry":"safe"}}}}
```

Use the actual command-start sequence and writer identity. Do not generate replacements for missing identity.
A preflight failure can instead use `{"response": <actual envelope>}`.
It remains sanitized orphan evidence, with unknown association, delivery and retry.
UI text, arguments, titles, paths, screenshots and response bodies are not retained in normalized records.

Unpaired commands stay unknown in private receipts. They create no token usage or completed tool point.
A later complete import can add the paired tool observation once.
Repeated complete imports deduplicate across segments and sessions.
Contradictory stored observations produce a disputed receipt and an error.
That error states when some valid observations may already be stored.

Desktop observations use `observations.sqlite3` inside the selected improvement root.
Pass that file to `aut observe --db` and the same root to `report --improve-root` to inspect both views.
Command success never establishes task acceptance. Native operations remain separate from parent model tool calls.
The adapter does not launch apps, request permissions, take screenshots or advise unsafe retries.

## Optional receiver and hosted packaging

Start the receiver only after supplying `AUT_OBSERVE_TOKEN` through the environment.
Use a generated token of at least 16 non-whitespace characters.

```bash
aut observe --db ~/.local/share/aut/observations.sqlite3 serve
```

It defaults to `127.0.0.1:8766`. `--token-env NAME` selects another explicit environment variable.
`GET /health` exposes health only. `GET /v1/report` requires bearer authentication.
`POST /v1/ingest` requires the same authentication and exact `Content-Type: application/json`.
A normalized request wraps records as follows:

```json
{"format":"jsonl","source":"my-application","stream":"requests","role":"primary",
 "records":[{"version":1,"kind":"tool","id":"attempt-42",
             "timestamp":"2026-11-13T12:00:00Z","status":"error"}]}
```

For OTLP JSON, use `format: "otlp"` and `payload` instead of `records`.
Unknown wrapper fields, transfer encoding and ambiguous body lengths fail.
Bodies are limited to 8 MiB. Sockets time out after five seconds.
Success acknowledges durable storage. Conflicts return HTTP 409 with a partial-store receipt.
An exact retry preserves identities and does not add usage twice.

The receiver processes requests sequentially. Keep it internal.
A nonloopback bind needs an authenticated TLS proxy and a private network boundary.
[Inactive container and systemd examples](../packaging/observations/README.md) provide private persistent storage.
No command above deploys a container, installs a service or changes an existing timer.

## Local public draft

```bash
aut observe --db ~/.local/share/aut/observations.sqlite3 export-public \
  --out ~/.local/share/aut/public-observations-draft.json
```

This writes a local draft and never publishes it.
Its fixed fields contain timestamps, aggregate operation counts, study decisions and unknown coverage.
Desktop and other operation counts remain separate. There is no combined parent/child total.
Source identifiers, accounts, projects, session IDs, text, tokens, prices, costs and quotas are excluded.
Selected scoped study results do not establish general productivity or representative error rates.
Review the draft and its evidence before choosing public metrics.
