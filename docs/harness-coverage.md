# Know which coding work AUT can inspect

AUT reports readable sources, missing evidence and collection gaps across coding tools.
The report separates a successful collector run from proof that all work was captured.

Run `aut doctor --json` for the private report, or `aut doctor` for a short issue list.
Doctor reads bounded local metadata. It runs no models, commands, collectors or network requests.
It does not create a salt, change watermarks, open the delivery queue for writing or save its report.

Normal scans save two private files beside the watermark file:

- `scan-observations.json`: the latest collector receipts, merged by collector.
- `coverage.json`: the current source assessment and a signature of its issue set.

Changed nonempty issue sets produce one local stderr notice. Reports stay local.
They are not added to public ledger payloads or improvement patch proposals.
These notices use the existing scheduled-job logs; they do not send desktop, email or chat notifications.

## What the report proves

Source availability, parser support, observed records and completeness are separate fields.
Normalized row counts describe local collection, not end-to-end delivery acknowledgements.
Human counts describe existing collector classifications, not independent human-origin certification.
Commit and pull-request links do not establish an accepted task outcome.

Missing, stale, malformed, future-dated and failed receipts remain distinct from a quiet successful pass.
Parser losses remain visible even when useful usage rows were delivered.
Pending delivery is reported separately. An existing SQLite WAL makes the queue probe unknown,
because opening it could create sidecar files.

Watermark reads stop at 64 MiB. Other report JSON reads stop at 8 MiB or a smaller purpose-specific limit.
A read limit is distinct from invalid state.
Metadata traversal stops after 4,096 entries or eight directory levels.
Uninspected roots stay unknown. Skipped symlinks and traversal limits remain explicit.
Receipt freshness uses a 12-hour threshold, exposed in the report.

The catalog describes documented routes for 25 tools. It does not enable collectors.
Every entry remains unqualified by that catalog.
On macOS, bounded app-folder name matches appear as candidates with unknown activity.
Doctor does not read app contents or retain unmatched app names.
Other platforms retain native-source and explicit-registry checks; app inventory remains unknown.

## Register a profile or another harness

The registry is `sources.json` beside the normal AUT configuration.
`AUT_SOURCES` can select another absolute path for the live user's registry.
Alternate-home inspection uses that home's `.config/agent-usage-tracker/sources.json`.

For example:

```json
{
  "version": 1,
  "sources": [
    {
      "id": "my-ade-codex",
      "harness": "my-ade",
      "engine": "codex",
      "location": "local",
      "transport": "native_files",
      "home": "/absolute/path/to/codex-home"
    },
    {
      "id": "my-remote-ade",
      "harness": "my-ade",
      "engine": "custom-agent",
      "location": "remote",
      "transport": "http_events"
    }
  ],
  "links": [
    {
      "source_id": "my-ade-codex",
      "native_session_id": "native-session",
      "harness_thread_id": "app-thread",
      "request_id": "app-request"
    }
  ]
}
```

Native file homes currently extend `claude_code`, `codex` and `pi`.
The default home stays included. Live `CLAUDE_CONFIG_DIR` and `CODEX_HOME` values are included.
Explicit T3 Claude profiles remain included. Resolved aliases are read once.
Collection, local labels and private improvement inspection use the same discovery helpers.

A home means the engine's state directory, such as the directory containing Codex `sessions`.
It does not mean a repository checkout.
Native file collection accepts registered local `native_files` or `structured_cli` routes.
The other declared routes are `app_server`, `acp`, `http_events`, `otlp`, `pty` and `unknown`.
An unfamiliar engine or remote route is a valid declaration, but does not create a reader.

Links preserve explicit source/session/thread/request associations.
They do not change native event IDs, redistribute counts, certify human input or prove duplicate equivalence.
Doctor exposes at most 512 links and reports omitted links.
Existing source-specific duplicate handling, including T3 and SDK overlap rejection, remains unchanged.

The registry accepts at most 1 MiB, 128 sources, 10,000 links and 32 publications.
Identifiers and fields are validated. Relative paths, unsupported versions, unknown fields and conflicting source IDs are rejected.
Invalid registrations report a gap; they cannot silently enable a new source or widen its evidence claims.

## Publication receipts

A registered publication can point to a local receipt:

```json
{
  "version": 1,
  "publications": [
    {
      "id": "public-ledger",
      "path": "/absolute/path/to/publication-receipt.json",
      "timezone": "Europe/Amsterdam"
    }
  ]
}
```

Its receipt contains `completed_through`, timezone-aware `published_at` and `verified_at`,
and matching `published_revision` and `live_revision`.
The completed day must be yesterday in the registered timezone.
Verification must be fresh; impossible times, missing fields and mismatched revisions cannot pass.
A publisher adapter must produce and refresh that receipt.
Doctor does not fetch the website, manufacture verification or infer a publication from a collector heartbeat.

## Adding an integration

Identify the actual host, provider mode, engine version and evidence interface.
Reuse a native reader when it matches the source format.
Use a source adapter for structured CLI/SDK, app-server, ACP or server-event APIs.
A terminal status event alone cannot provide requests, full tool results or verified outcomes.

Qualify adapters with content-free fixtures for identity, origin, retries, rotation, malformed records,
profile changes, missing results and unknown versions.
Telemetry prompt redaction does not remove every content channel.
Keep a field allowlist and explicit ownership when combining native, SDK and hook observations.
